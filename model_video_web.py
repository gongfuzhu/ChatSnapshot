#!/usr/bin/env python3
"""模特视频浏览 Web 页面（Python 标准库零新依赖）。

读取 MongoDB clusterchat.model_videos（fetch_model_videos.py 写入的数据），
提供一个内网可访问的浏览/播放页面：
  GET /            页面
  GET /api/videos  视频 JSON（支持 ?q=关键词 &user_id= &page=&page_size=）
  GET /api/models  模特列表（筛选用）

free 的 video_url 多为 HLS(.m3u8)，前端用 hls.js 播放；trailer 为 mp4 直接播放。

用法：
  python model_video_web.py                 # 监听 0.0.0.0:8088
  python model_video_web.py --port 8090
  python model_video_web.py --host 127.0.0.1   # 仅本机访问
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from pymongo import MongoClient
from pymongo.errors import OperationFailure

from model_db_config import MONGO_URI, MONGO_DB, MONGO_COLLECTION

DEFAULT_PORT = 8088
ALBUM_COLLECTION = "model_albums"
# 发布审核三态
PUBLISH_STATUSES = ("pending", "approved", "rejected")
PUBLISH_LABELS = {"pending": "待审核", "approved": "可发布", "rejected": "不发布"}


_shared_client = None


def get_collection():
    """返回全局共享的 MongoClient 与视频集合（连接池单例）。

    复用单一长连接客户端，避免每个 HTTP 请求都新建 client、重新做副本集拓扑
    发现——在到 Atlas 网络抖动时，反复重连极易触发 ServerSelectionTimeout。
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    return _shared_client, _shared_client[MONGO_DB][MONGO_COLLECTION]


def get_album_collection():
    """复用同一连接池客户端，返回 free 相册集合 model_albums。"""
    client, _ = get_collection()
    return client, client[MONGO_DB][ALBUM_COLLECTION]


def build_query(params):
    """根据查询参数构造 Mongo filter。"""
    q = {}
    user_id = params.get("user_id", [None])[0]
    if user_id:
        try:
            q["user_id"] = int(user_id)
        except ValueError:
            pass
    keyword = (params.get("q", [""])[0] or "").strip()
    if keyword:
        rx = re.escape(keyword)
        q["$or"] = [
            {"title": {"$regex": rx, "$options": "i"}},
            {"description": {"$regex": rx, "$options": "i"}},
            {"model.username": {"$regex": rx, "$options": "i"}},
        ]
    pub = params.get("publish_status", [None])[0]
    if pub in PUBLISH_STATUSES:
        q["publish_status"] = pub
    access = params.get("access", [None])[0]
    if access == "free":
        q["access_mode"] = "free"
    elif access == "paid":
        # 非 free（付费/粉丝俱乐部等，存的是预告片链接）
        q["access_mode"] = {"$ne": "free"}
    return q


def build_album_query(params):
    """相册查询：按 user_id 精确过滤 + 关键词（模特名/相册名）。集合本身只含 free。"""
    q = {}
    user_id = params.get("user_id", [None])[0]
    if user_id:
        try:
            q["user_id"] = int(user_id)
        except ValueError:
            pass
    keyword = (params.get("q", [""])[0] or "").strip()
    if keyword:
        rx = re.escape(keyword)
        q["$or"] = [
            {"name": {"$regex": rx, "$options": "i"}},
            {"model.username": {"$regex": rx, "$options": "i"}},
        ]
    return q


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>模特视频库</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--line:#2a2e38;--fg:#e7e9ee;--mut:#8b90a0;
        --accent:#4f8cff;--free:#1f9d55;--paid:#c9741f;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;z-index:10;background:rgba(15,17,21,.92);
         backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
         padding:14px 20px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  header h1{font-size:18px;margin:0;white-space:nowrap}
  header h1 small{color:var(--mut);font-weight:400;font-size:12px;margin-left:8px}
  .navlink{display:inline-flex;align-items:center;background:var(--card);border:1px solid var(--line);
       color:var(--fg);text-decoration:none;border-radius:8px;padding:8px 14px;font-size:13px;white-space:nowrap}
  .navlink:hover{border-color:var(--accent);color:var(--accent)}
  input,select{background:var(--card);border:1px solid var(--line);color:var(--fg);
       border-radius:8px;padding:9px 12px;font-size:14px;outline:none}
  input{flex:1;min-width:200px}
  input:focus,select:focus{border-color:var(--accent)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
       gap:16px;padding:20px;max-width:1400px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
       overflow:hidden;cursor:pointer;transition:transform .12s,border-color .12s}
  .card:hover{transform:translateY(-3px);border-color:var(--accent)}
  .thumb{position:relative;aspect-ratio:16/9;background:#000;
       background-size:cover;background-position:center}
  .badge{position:absolute;top:8px;left:8px;font-size:11px;padding:3px 8px;
       border-radius:20px;color:#fff;font-weight:600}
  .badge.free{background:var(--free)} .badge.paid{background:var(--paid)}
  .dur{position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,.75);
       font-size:11px;padding:2px 7px;border-radius:6px}
  .pub{position:absolute;top:8px;right:8px;display:flex;align-items:center;gap:5px;
       background:rgba(0,0,0,.72);border-radius:20px;padding:4px 10px;font-size:11px;
       cursor:pointer;user-select:none;border:1px solid var(--line);color:var(--mut)}
  .pub:hover{filter:brightness(1.25)}
  .pub.st-approved{background:rgba(31,157,85,.92);color:#fff;border-color:var(--free)}
  .pub.st-rejected{background:rgba(200,50,60,.92);color:#fff;border-color:#c8323c}
  .pub .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
  .card.st-approved{border-color:var(--free)}
  .card.st-rejected{border-color:#c8323c;opacity:.85}
  .pubrow{display:flex;gap:6px;margin-top:9px}
  .pubbtn{flex:1;padding:7px 0;font-size:12px;border-radius:8px;cursor:pointer;
       border:1px solid var(--line);background:#161922;color:var(--mut);
       transition:all .12s;font-family:inherit}
  .pubbtn:hover{filter:brightness(1.3)}
  .pubbtn.st-pending.active{background:#3a4150;color:#fff;border-color:#5a6373}
  .pubbtn.st-approved.active{background:rgba(31,157,85,.95);color:#fff;border-color:var(--free)}
  .pubbtn.st-rejected.active{background:rgba(200,50,60,.95);color:#fff;border-color:#c8323c}
  .meta{padding:10px 12px}
  .title{font-size:14px;line-height:1.4;margin:0 0 6px;
       display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:39px}
  .sub{font-size:12px;color:var(--mut);display:flex;justify-content:space-between;gap:8px}
  .empty{grid-column:1/-1;text-align:center;color:var(--mut);padding:60px 0}
  /* 模态播放 */
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;
       align-items:center;justify-content:center;z-index:50;padding:24px}
  .modal.open{display:flex}
  .player-box{width:min(960px,100%);background:var(--card);border-radius:14px;
       border:1px solid var(--line);overflow:hidden}
  video{width:100%;max-height:70vh;background:#000;display:block}
  .pmeta{padding:14px 18px}
  .pmeta h3{margin:0 0 8px;font-size:16px}
  .pmeta .row{color:var(--mut);font-size:13px;display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
  .pmeta a{color:var(--accent);word-break:break-all;font-size:12px}
  .close{float:right;cursor:pointer;color:var(--mut);font-size:22px;line-height:1}
  .pager{display:flex;gap:10px;justify-content:center;align-items:center;
       padding:8px 20px 40px;color:var(--mut)}
  .pager button{background:var(--card);border:1px solid var(--line);color:var(--fg);
       border-radius:8px;padding:8px 16px;cursor:pointer}
  .pager button:disabled{opacity:.4;cursor:default}
</style>
</head>
<body>
<header>
  <h1>模特视频库<small id="count"></small></h1>
  <a class="navlink" href="/albums">📷 图片库</a>
  <input id="search" placeholder="搜索标题 / 模特名…" oninput="onSearch()">
  <select id="modelFilter" onchange="load(1)"><option value="">全部模特</option></select>
  <select id="pubFilter" onchange="load(1)">
    <option value="">全部标注</option>
    <option value="pending">待审核</option>
    <option value="approved">可发布</option>
    <option value="rejected">不发布</option>
  </select>
  <select id="accessFilter" onchange="load(1)">
    <option value="">全部类型</option>
    <option value="free">仅 FREE 完整</option>
    <option value="paid">仅预告片</option>
  </select>
</header>
<div class="grid" id="grid"></div>
<div class="pager">
  <button id="prev" onclick="page--;load(page)">上一页</button>
  <span id="pageInfo"></span>
  <button id="next" onclick="page++;load(page)">下一页</button>
</div>

<div class="modal" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="player-box">
    <video id="player" controls playsinline></video>
    <div class="pmeta">
      <span class="close" onclick="closeModal()">&times;</span>
      <h3 id="pTitle"></h3>
      <div class="row">
        <span id="pModel"></span><span id="pMode"></span><span id="pDur"></span>
        <span id="pLikes"></span><span id="pDate"></span>
      </div>
      <a id="pLink" target="_blank" rel="noopener"></a>
    </div>
  </div>
</div>

<script>
let page = 1, totalPages = 1, searchTimer = null, hls = null;
const PAGE_SIZE = 24;
const PUB_ORDER = ["pending","approved","rejected"];
const PUB_LABELS = {pending:"待审核", approved:"可发布", rejected:"不发布"};
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtDur = s => s==null?"":(Math.floor(s/60)+":"+String(s%60).padStart(2,"0"));

async function load(p){
  page = p;
  const q = document.getElementById("search").value.trim();
  const uid = document.getElementById("modelFilter").value;
  const pub = document.getElementById("pubFilter").value;
  const access = document.getElementById("accessFilter").value;
  const params = new URLSearchParams({page:String(p), page_size:String(PAGE_SIZE)});
  if(q) params.set("q", q);
  if(uid) params.set("user_id", uid);
  if(pub) params.set("publish_status", pub);
  if(access) params.set("access", access);
  const r = await fetch("/api/videos?"+params);
  const data = await r.json();
  totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  document.getElementById("count").textContent = "共 "+data.total+" 个视频";
  document.getElementById("pageInfo").textContent = page+" / "+totalPages;
  document.getElementById("prev").disabled = page<=1;
  document.getElementById("next").disabled = page>=totalPages;
  const grid = document.getElementById("grid");
  if(!data.items.length){ grid.innerHTML='<div class="empty">没有匹配的视频</div>'; return; }
  grid.innerHTML = data.items.map(v=>{
    const free = v.access_mode==="free";
    const st = v.publish_status || "pending";
    return `<div class="card st-${st}" id="card-${v.video_id}" onclick='play(${JSON.stringify(v).replace(/'/g,"&#39;")})'>
      <div class="thumb" style="background-image:url('${esc(v.cover_url)}')">
        <span class="badge ${free?"free":"paid"}">${free?"FREE完整":"预告片"}</span>
        <span class="pub st-${st}"><span class="dot"></span><span class="pubtxt">${PUB_LABELS[st]||"待审核"}</span></span>
        ${v.duration?`<span class="dur">${fmtDur(v.duration)}</span>`:""}
      </div>
      <div class="meta">
        <p class="title">${esc(v.title)||"(无标题)"}</p>
        <div class="sub"><span>${esc(v.model&&v.model.username)||""}</span>
        <span>♥ ${v.likes||0}</span></div>
        <div class="pubrow" onclick="event.stopPropagation()">
          ${PUB_ORDER.map(s=>`<button type="button" class="pubbtn st-${s} ${st===s?"active":""}"
                  data-id="${v.video_id}" data-status="${s}"
                  onclick="setStatus(event, ${v.video_id}, '${s}')">${PUB_LABELS[s]}</button>`).join("")}
        </div>
      </div></div>`;
  }).join("");
}

function onSearch(){ clearTimeout(searchTimer); searchTimer=setTimeout(()=>load(1),300); }

async function setStatus(ev, videoId, status){
  ev.stopPropagation();  // 不触发卡片播放
  const card = document.getElementById("card-"+videoId);
  const oldStatus = card ? (card.dataset.status || (card.className.match(/st-(pending|approved|rejected)/)||[])[1] || "pending") : "pending";
  // 乐观更新：卡片状态类 + 三个按钮高亮
  if(card){
    card.classList.remove("st-pending","st-approved","st-rejected");
    card.classList.add("st-"+status);
    card.dataset.status = status;
    card.querySelectorAll(".pubbtn").forEach(b=>{
      b.classList.toggle("active", b.dataset.status === status);
    });
    const tag = card.querySelector(".pub");
    if(tag){ tag.className = "pub st-"+status; tag.querySelector(".pubtxt").textContent = PUB_LABELS[status]; }
  }
  try{
    const r = await fetch("/api/publish-status", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({video_id: videoId, publish_status: status})
    });
    if(!r.ok) throw new Error("HTTP "+r.status);
  }catch(err){
    // 失败回滚
    if(card){
      card.classList.remove("st-"+status); card.classList.add("st-"+oldStatus);
      card.dataset.status = oldStatus;
      card.querySelectorAll(".pubbtn").forEach(b=>{
        b.classList.toggle("active", b.dataset.status === oldStatus);
      });
      const tag = card.querySelector(".pub");
      if(tag){ tag.className = "pub st-"+oldStatus; tag.querySelector(".pubtxt").textContent = PUB_LABELS[oldStatus]; }
    }
    alert("标注保存失败："+err.message);
  }
}

async function loadModels(){
  const r = await fetch("/api/models"); const data = await r.json();
  const sel = document.getElementById("modelFilter");
  data.models.forEach(m=>{
    const o=document.createElement("option"); o.value=m.user_id;
    o.textContent=`${m.username||m.user_id} (${m.n})`; sel.appendChild(o);
  });
}

function play(v){
  const modal=document.getElementById("modal"), video=document.getElementById("player");
  if(hls){ hls.destroy(); hls=null; }
  document.getElementById("pTitle").textContent=v.title||"(无标题)";
  document.getElementById("pModel").textContent="模特: "+(v.model&&v.model.username||v.user_id);
  document.getElementById("pMode").textContent="类型: "+(v.access_mode||"-")+"（"+(v.url_field||"")+"）";
  document.getElementById("pDur").textContent="时长: "+(v.duration!=null?fmtDur(v.duration):"-");
  document.getElementById("pLikes").textContent="赞: "+(v.likes||0);
  document.getElementById("pDate").textContent="发布: "+(v.created_at||"").slice(0,10);
  const link=document.getElementById("pLink"); link.href=v.video_url; link.textContent=v.video_url;
  modal.classList.add("open");
  const url=v.video_url||"";
  if(url.includes(".m3u8") && window.Hls && Hls.isSupported()){
    hls=new Hls(); hls.loadSource(url); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));
  }else{ video.src=url; video.play().catch(()=>{}); }
}
function closeModal(){
  const video=document.getElementById("player"); video.pause(); video.removeAttribute("src"); video.load();
  if(hls){hls.destroy();hls=null;}
  document.getElementById("modal").classList.remove("open");
}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});
loadModels(); load(1);
</script>
</body>
</html>"""


ALBUMS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>模特图片库 · FREE 相册</title>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--line:#2a2e38;--fg:#e7e9ee;--mut:#8b90a0;
        --accent:#4f8cff;--free:#1f9d55;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;z-index:10;background:rgba(15,17,21,.92);
         backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
         padding:14px 20px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  header h1{font-size:18px;margin:0;white-space:nowrap}
  header h1 small{color:var(--mut);font-weight:400;font-size:12px;margin-left:8px}
  .navlink{display:inline-flex;align-items:center;background:var(--card);border:1px solid var(--line);
       color:var(--fg);text-decoration:none;border-radius:8px;padding:8px 14px;font-size:13px;white-space:nowrap}
  .navlink:hover{border-color:var(--accent);color:var(--accent)}
  input,select{background:var(--card);border:1px solid var(--line);color:var(--fg);
       border-radius:8px;padding:9px 12px;font-size:14px;outline:none}
  input{flex:1;min-width:200px}
  input:focus,select:focus{border-color:var(--accent)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
       gap:16px;padding:20px;max-width:1400px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
       overflow:hidden;cursor:pointer;transition:transform .12s,border-color .12s}
  .card:hover{transform:translateY(-3px);border-color:var(--accent)}
  .cover{position:relative;aspect-ratio:3/4;background:#000;
       background-size:cover;background-position:center}
  .badge{position:absolute;top:8px;left:8px;font-size:11px;padding:3px 8px;
       border-radius:20px;color:#fff;font-weight:600;background:var(--free)}
  .cnt{position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,.75);
       font-size:11px;padding:2px 8px;border-radius:6px}
  .meta{padding:10px 12px}
  .title{font-size:14px;line-height:1.4;margin:0 0 6px;font-weight:600;
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sub{font-size:12px;color:var(--mut);display:flex;justify-content:space-between;gap:8px}
  .empty{grid-column:1/-1;text-align:center;color:var(--mut);padding:60px 0}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;
       align-items:center;justify-content:center;z-index:50;padding:24px}
  .modal.open{display:flex}
  .album-box{width:min(1080px,100%);max-height:92vh;overflow:auto;background:var(--card);
       border-radius:14px;border:1px solid var(--line);padding:18px 20px}
  .album-box h3{margin:0 0 4px;font-size:16px}
  .album-box .arow{color:var(--mut);font-size:13px;margin-bottom:14px;display:flex;gap:16px;flex-wrap:wrap}
  .close{float:right;cursor:pointer;color:var(--mut);font-size:24px;line-height:1}
  .photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .photos img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:8px;
       background:#000;cursor:zoom-in;border:1px solid var(--line)}
  /* 大图 lightbox */
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.96);display:none;
       align-items:center;justify-content:center;z-index:60;padding:20px}
  .lightbox.open{display:flex}
  .lightbox img{max-width:100%;max-height:96vh;border-radius:8px}
  .pager{display:flex;gap:10px;justify-content:center;align-items:center;
       padding:8px 20px 40px;color:var(--mut)}
  .pager button{background:var(--card);border:1px solid var(--line);color:var(--fg);
       border-radius:8px;padding:8px 16px;cursor:pointer}
  .pager button:disabled{opacity:.4;cursor:default}
</style>
</head>
<body>
<header>
  <h1>模特图片库<small id="count"></small></h1>
  <a class="navlink" href="/">🎬 视频库</a>
  <input id="search" placeholder="搜索模特名 / 相册名…" oninput="onSearch()">
  <select id="modelFilter" onchange="load(1)"><option value="">全部模特</option></select>
</header>
<div class="grid" id="grid"></div>
<div class="pager">
  <button id="prev" onclick="page--;load(page)">上一页</button>
  <span id="pageInfo"></span>
  <button id="next" onclick="page++;load(page)">下一页</button>
</div>

<div class="modal" id="modal" onclick="if(event.target===this)closeAlbum()">
  <div class="album-box">
    <span class="close" onclick="closeAlbum()">&times;</span>
    <h3 id="aTitle"></h3>
    <div class="arow"><span id="aModel"></span><span id="aCount"></span><span id="aDate"></span></div>
    <div class="photos" id="photos"></div>
  </div>
</div>
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <img id="bigImg" alt="大图">
</div>

<script>
let page = 1, totalPages = 1, searchTimer = null;
const PAGE_SIZE = 24;
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function load(p){
  page = p;
  const q = document.getElementById("search").value.trim();
  const uid = document.getElementById("modelFilter").value;
  const params = new URLSearchParams({page:String(p), page_size:String(PAGE_SIZE)});
  if(q) params.set("q", q);
  if(uid) params.set("user_id", uid);
  const r = await fetch("/api/albums?"+params);
  const data = await r.json();
  totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  document.getElementById("count").textContent = "共 "+data.total+" 个相册";
  document.getElementById("pageInfo").textContent = page+" / "+totalPages;
  document.getElementById("prev").disabled = page<=1;
  document.getElementById("next").disabled = page>=totalPages;
  const grid = document.getElementById("grid");
  if(!data.items.length){ grid.innerHTML='<div class="empty">没有匹配的相册</div>'; return; }
  grid.innerHTML = data.items.map(a=>{
    const uname = esc(a.model&&a.model.username)||a.user_id;
    return `<div class="card" onclick='openAlbum(${JSON.stringify(a).replace(/'/g,"&#39;")})'>
      <div class="cover" style="background-image:url('${esc(a.preview)}')">
        <span class="badge">FREE</span>
        <span class="cnt">${a.photos_count||(a.photos?a.photos.length:0)} 图</span>
      </div>
      <div class="meta">
        <p class="title">${esc(a.name)||"(相册)"}</p>
        <div class="sub"><span>${uname}</span><span>♥ ${a.likes||0}</span></div>
      </div></div>`;
  }).join("");
}

function onSearch(){ clearTimeout(searchTimer); searchTimer=setTimeout(()=>load(1),300); }

async function loadModels(){
  const r = await fetch("/api/album-models"); const data = await r.json();
  const sel = document.getElementById("modelFilter");
  data.models.forEach(m=>{
    const o=document.createElement("option"); o.value=m.user_id;
    o.textContent=`${m.username||m.user_id} (${m.n})`; sel.appendChild(o);
  });
}

function openAlbum(a){
  const uname = (a.model&&a.model.username)||a.user_id;
  document.getElementById("aTitle").textContent = a.name||"(相册)";
  document.getElementById("aModel").textContent = "模特: "+uname;
  document.getElementById("aCount").textContent = "图片: "+(a.photos?a.photos.length:0);
  document.getElementById("aDate").textContent = "创建: "+(a.created_at||"").slice(0,10);
  const photos = a.photos||[];
  document.getElementById("photos").innerHTML = photos.map(ph=>
    `<img loading="lazy" src="${esc(ph.url_thumb||ph.url)}" data-full="${esc(ph.url)}"
         onclick="openLightbox('${esc(ph.url).replace(/'/g,"\\'")}')" alt="图">`
  ).join("") || '<div class="empty">该相册暂无内嵌图片</div>';
  document.getElementById("modal").classList.add("open");
}
function closeAlbum(){ document.getElementById("modal").classList.remove("open"); }
function openLightbox(url){ const b=document.getElementById("bigImg"); b.src=url;
  document.getElementById("lightbox").classList.add("open"); }
function closeLightbox(){ const b=document.getElementById("bigImg"); b.src="";
  document.getElementById("lightbox").classList.remove("open"); }
document.addEventListener("keydown",e=>{ if(e.key==="Escape"){ closeLightbox(); closeAlbum(); } });
loadModels(); load(1);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 禁用缓存，确保每次拿到最新前端代码（修过的按钮/字段立即生效）
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                return self._send(200, PAGE_HTML, "text/html; charset=utf-8")
            if parsed.path == "/albums":
                return self._send(200, ALBUMS_HTML, "text/html; charset=utf-8")
            if parsed.path == "/api/videos":
                return self._videos(params)
            if parsed.path == "/api/models":
                return self._models()
            if parsed.path == "/api/albums":
                return self._albums(params)
            if parsed.path == "/api/album-models":
                return self._album_models()
            self._send(404, json.dumps({"error": "not found"}))
        except OperationFailure as e:
            self._send(500, json.dumps({"error": f"DB error: {e}"}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            if parsed.path == "/api/publish-status":
                return self._set_publish_status(payload)
            self._send(404, json.dumps({"error": "not found"}))
        except json.JSONDecodeError:
            self._send(400, json.dumps({"error": "invalid JSON"}, ensure_ascii=False))
        except OperationFailure as e:
            self._send(500, json.dumps({"error": f"DB error: {e}"}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def _set_publish_status(self, payload):
        """更新单条视频的发布审核状态（pending/approved/rejected）。"""
        video_id = payload.get("video_id")
        status = payload.get("publish_status")
        if not isinstance(video_id, int) or status not in PUBLISH_STATUSES:
            return self._send(400, json.dumps(
                {"error": f"需要 int video_id 与 publish_status∈{list(PUBLISH_STATUSES)}"},
                ensure_ascii=False))
        client, col = get_collection()
        try:
            res = col.update_one({"video_id": video_id},
                                 {"$set": {"publish_status": status}})
            if res.matched_count == 0:
                return self._send(404, json.dumps({"error": "video not found"}))
            self._send(200, json.dumps({"ok": True, "video_id": video_id,
                                        "publish_status": status}))
        finally:
            pass  # 共享连接池，不关闭 client

    def _videos(self, params):
        client, col = get_collection()
        try:
            q = build_query(params)
            try:
                page_size = min(int(params.get("page_size", [24])[0]), 100)
            except ValueError:
                page_size = 24
            try:
                page = max(int(params.get("page", [1])[0]), 1)
            except ValueError:
                page = 1
            total = col.count_documents(q)
            cursor = (col.find(q, {"_id": 0})
                      # 按入库时间 first_seen 倒序（最新进入数据库的在前），
                      # video_id 倒序作为同一入库批次内的稳定兜底次序。
                      .sort([("first_seen", -1), ("video_id", -1)])
                      .skip((page - 1) * page_size).limit(page_size))
            items = list(cursor)
            self._send(200, json.dumps(
                {"total": total, "page": page, "page_size": page_size, "items": items},
                ensure_ascii=False, default=str))
        finally:
            pass  # 共享连接池，不关闭 client

    def _models(self):
        client, col = get_collection()
        try:
            pipeline = [
                {"$group": {"_id": "$user_id",
                            "username": {"$first": "$model.username"},
                            "n": {"$sum": 1}}},
                {"$project": {"user_id": "$_id", "username": 1, "n": 1, "_id": 0}},
                {"$sort": {"n": -1}},
            ]
            models = list(col.aggregate(pipeline))
            self._send(200, json.dumps({"models": models}, ensure_ascii=False, default=str))
        finally:
            pass  # 共享连接池，不关闭 client

    def _albums(self, params):
        client, col = get_album_collection()
        try:
            q = build_album_query(params)
            try:
                page_size = min(int(params.get("page_size", [24])[0]), 100)
            except ValueError:
                page_size = 24
            try:
                page = max(int(params.get("page", [1])[0]), 1)
            except ValueError:
                page = 1
            total = col.count_documents(q)
            # 按入库时间倒序，album_id 兜底稳定次序；内嵌 photos 全量返回供前端展开
            cursor = (col.find(q, {"_id": 0})
                      .sort([("first_seen", -1), ("album_id", -1)])
                      .skip((page - 1) * page_size).limit(page_size))
            items = list(cursor)
            self._send(200, json.dumps(
                {"total": total, "page": page, "page_size": page_size, "items": items},
                ensure_ascii=False, default=str))
        finally:
            pass

    def _album_models(self):
        """相册集合中的模特分组（供图片库筛选下拉）。"""
        client, col = get_album_collection()
        try:
            pipeline = [
                {"$group": {"_id": "$user_id",
                            "username": {"$first": "$model.username"},
                            "n": {"$sum": 1}}},
                {"$project": {"user_id": "$_id", "username": 1, "n": 1, "_id": 0}},
                {"$sort": {"n": -1}},
            ]
            models = list(col.aggregate(pipeline))
            self._send(200, json.dumps({"models": models}, ensure_ascii=False, default=str))
        finally:
            pass

    def log_message(self, fmt, *args):  # 精简日志
        print(f"[web] {self.address_string()} {fmt % args}")


def main():
    ap = argparse.ArgumentParser(description="模特视频浏览 Web 服务")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认全网卡，内网可访问）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    # 启动前探测一次数据库（共享连接池，不关闭）
    client, col = get_collection()
    client.admin.command("ping")
    _, album_col = get_album_collection()
    print(f"[web] 已连接 {MONGO_DB}.{MONGO_COLLECTION}，现有 {col.count_documents({})} 条视频")
    print(f"[web] 图片库 {MONGO_DB}.{ALBUM_COLLECTION}，现有 {album_col.count_documents({})} 个 free 相册")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[web] 视频浏览服务启动: http://{args.host}:{args.port}/")
    print(f"[web] 本机访问 http://127.0.0.1:{args.port}/ ，局域网用 http://<本机IP>:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已停止")


if __name__ == "__main__":
    main()
