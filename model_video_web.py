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


def get_collection():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    return client, client[MONGO_DB][MONGO_COLLECTION]


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
    pub = params.get("publishable", [None])[0]
    if pub in ("true", "false"):
        q["publishable"] = (pub == "true")
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
       background:rgba(0,0,0,.72);border-radius:20px;padding:4px 9px;font-size:11px;
       cursor:pointer;user-select:none;border:1px solid var(--line);color:var(--mut)}
  .pub.on{background:rgba(31,157,85,.92);color:#fff;border-color:var(--free)}
  .pub .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
  .card.marked{border-color:var(--free)}
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
  <input id="search" placeholder="搜索标题 / 模特名…" oninput="onSearch()">
  <select id="modelFilter" onchange="load(1)"><option value="">全部模特</option></select>
  <select id="pubFilter" onchange="load(1)">
    <option value="">全部标注</option>
    <option value="true">仅可发布</option>
    <option value="false">仅未标注</option>
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
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtDur = s => s==null?"":(Math.floor(s/60)+":"+String(s%60).padStart(2,"0"));

async function load(p){
  page = p;
  const q = document.getElementById("search").value.trim();
  const uid = document.getElementById("modelFilter").value;
  const pub = document.getElementById("pubFilter").value;
  const params = new URLSearchParams({page:String(p), page_size:String(PAGE_SIZE)});
  if(q) params.set("q", q);
  if(uid) params.set("user_id", uid);
  if(pub) params.set("publishable", pub);
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
    const marked = v.publishable === true;
    return `<div class="card ${marked?"marked":""}" id="card-${v.video_id}" onclick='play(${JSON.stringify(v).replace(/'/g,"&#39;")})'>
      <div class="thumb" style="background-image:url('${esc(v.cover_url)}')">
        <span class="badge ${free?"free":"paid"}">${free?"FREE完整":"预告片"}</span>
        <span class="pub ${marked?"on":""}" data-id="${v.video_id}"
              onclick="togglePublish(event, ${v.video_id})"
              title="切换是否可发布（用于后续自动发布）">
          <span class="dot"></span><span class="pubtxt">${marked?"可发布":"待审核"}</span>
        </span>
        ${v.duration?`<span class="dur">${fmtDur(v.duration)}</span>`:""}
      </div>
      <div class="meta">
        <p class="title">${esc(v.title)||"(无标题)"}</p>
        <div class="sub"><span>${esc(v.model&&v.model.username)||""}</span>
        <span>♥ ${v.likes||0}</span></div>
      </div></div>`;
  }).join("");
}

function onSearch(){ clearTimeout(searchTimer); searchTimer=setTimeout(()=>load(1),300); }

async function togglePublish(ev, videoId){
  ev.stopPropagation();  // 不触发卡片播放
  const el = ev.currentTarget;
  const willOn = !el.classList.contains("on");
  // 乐观更新 UI
  el.classList.toggle("on", willOn);
  el.querySelector(".pubtxt").textContent = willOn ? "可发布" : "待审核";
  document.getElementById("card-"+videoId)?.classList.toggle("marked", willOn);
  try{
    const r = await fetch("/api/publishable", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({video_id: videoId, publishable: willOn})
    });
    if(!r.ok) throw new Error("HTTP "+r.status);
  }catch(err){
    // 失败回滚
    el.classList.toggle("on", !willOn);
    el.querySelector(".pubtxt").textContent = willOn ? "待审核" : "可发布";
    document.getElementById("card-"+videoId)?.classList.toggle("marked", !willOn);
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                return self._send(200, PAGE_HTML, "text/html; charset=utf-8")
            if parsed.path == "/api/videos":
                return self._videos(params)
            if parsed.path == "/api/models":
                return self._models()
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
            if parsed.path == "/api/publishable":
                return self._set_publishable(payload)
            self._send(404, json.dumps({"error": "not found"}))
        except json.JSONDecodeError:
            self._send(400, json.dumps({"error": "invalid JSON"}, ensure_ascii=False))
        except OperationFailure as e:
            self._send(500, json.dumps({"error": f"DB error: {e}"}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def _set_publishable(self, payload):
        """更新单条视频的 publishable 布尔标注。"""
        video_id = payload.get("video_id")
        value = payload.get("publishable")
        if not isinstance(video_id, int) or not isinstance(value, bool):
            return self._send(400, json.dumps(
                {"error": "需要 int video_id 与 bool publishable"}, ensure_ascii=False))
        client, col = get_collection()
        try:
            res = col.update_one({"video_id": video_id},
                                 {"$set": {"publishable": value}})
            if res.matched_count == 0:
                return self._send(404, json.dumps({"error": "video not found"}))
            self._send(200, json.dumps({"ok": True, "video_id": video_id,
                                        "publishable": value}))
        finally:
            client.close()

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
                      .sort([("created_at", -1), ("video_id", -1)])
                      .skip((page - 1) * page_size).limit(page_size))
            items = list(cursor)
            self._send(200, json.dumps(
                {"total": total, "page": page, "page_size": page_size, "items": items},
                ensure_ascii=False, default=str))
        finally:
            client.close()

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
            client.close()

    def log_message(self, fmt, *args):  # 精简日志
        print(f"[web] {self.address_string()} {fmt % args}")


def main():
    ap = argparse.ArgumentParser(description="模特视频浏览 Web 服务")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认全网卡，内网可访问）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    # 启动前探测一次数据库
    client, col = get_collection()
    try:
        client.admin.command("ping")
        print(f"[web] 已连接 {MONGO_DB}.{MONGO_COLLECTION}，现有 {col.count_documents({})} 条视频")
    finally:
        client.close()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[web] 视频浏览服务启动: http://{args.host}:{args.port}/")
    print(f"[web] 本机访问 http://127.0.0.1:{args.port}/ ，局域网用 http://<本机IP>:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已停止")


if __name__ == "__main__":
    main()
