# 设计文档：推荐直播间录制视频并发布到 X

日期：2026-09-07
状态：已确认（方案 A）

## 背景与目标

项目现有业务为「抓取直播封面 → 生成九宫格图 → 浏览器自动化发帖到 X」。
本次新增一条**独立业务**，与九宫格流程不发生数据关联：

> 从推荐直播间接口取直播间 → 录制 480p 直播流 15 秒为 mp4 视频 → 上传视频到 X → 文案「直播间：{username}」

原有九宫格图片流程**原样保留，行为不变**。

## 需求决策记录

| 决策点 | 结论 |
|--------|------|
| 接口选流策略 | 过滤 `status == "public"` 后取 `viewersCount` 最大者；无 public 主播时兜底取 `viewersCount` 最大者 |
| 录制工具 | 系统 ffmpeg（`shutil.which` 探测，缺失则报错提示安装） |
| 上传路径 | 浏览器自动化，复用 `upload_grid_to_x.py` 的编排 |
| 代码组织 | 方案 A：新建 `record_stream_to_x.py` + 最小化泛化 `upload_grid_to_x.py` 的上传函数 |
| 文案 | `"直播间：{username}"`，`{username}` 替换为被录制主播的 `username` |
| 临时文件 | 录制的 mp4 发帖后在 `finally` 中删除 |

## 接口

```
GET https://go.whitetrafsa.com/api/models?landing=Player&sortBy=paidUsers&modelsList=princiana
    &stripcashR=0&forceClient=1&modelPromotion=0&acclan=0
    &abTest=player_PlayerD20260826&abTestVariant=player_PlayerD20260826_PlayerGA_9
    &seenAbTest=1&seenDomain=1&seenLanding=1&limit=20
```

- 请求头按浏览器同款：`accept`、`user-agent`、`origin: https://creative.whitetrafsa.com`、`referer: https://creative.whitetrafsa.com/`
- 响应：`models[]` 扁平列表，每个元素含 `id`、`username`、`snapshotUrl`、`status`、`viewersCount`、`stream.urls["480p"]`（HLS m3u8 地址）等
- `API_URL` 按上述 curl 原样保留（含 `modelsList=princiana`）；后续如需覆盖全站推荐，只改这一处配置

## 组件设计

### 1. `record_stream_to_x.py`（新建）

**配置区**（文件顶部，同项目现有风格）：
- `API_URL`：见上
- `RECORD_SECONDS = 15`
- `PROXY = "http://127.0.0.1:7890"`（`""` 关闭）
- `POST_TEXT = "直播间：{username}"`

**函数**：
- `fetch_recommended()`：GET 接口（`PROXY` 非空时 requests 走该代理），`raise_for_status`，失败报错退出
- `pick_top_streamer(models)`：先过滤 `status == "public"`，取 `viewersCount` 最大；过滤后为空则兜底取全量 `viewersCount` 最大
- `record_stream(stream_url, out_path, seconds)`：调用 ffmpeg 录制
- `main()`：取数据 → 选流 → 录制 → 渲染文案 → 发帖 → 清理

**ffmpeg 录制命令**：
```
ffmpeg -y \
  -headers "Referer: https://creative.whitetrafsa.com/" \
  -i <480p m3u8 url> \
  -t 15 \
  -c:v libx264 -c:a aac \
  -movflags +faststart \
  out.mp4
```
采用**重编码**而非 `-c copy`，保证输出一定是 H.264/AAC mp4（X 硬性要求）；15 秒 480p 重编码耗时仅数秒。
`PROXY` 非空时追加 `-proxy <PROXY>` 参数，使拉流走本地代理。

**主流程**：
1. `fetch_recommended()` 取数据
2. `pick_top_streamer()` 选出目标直播间，取 `username` 与 `stream.urls["480p"]`
3. `record_stream()` 录制为 `stream_%Y%m%d_%H%M%S.mp4`
4. `render_post_text()` 渲染文案（复用 `upload_grid_to_x.py` 的同名函数）
5. `ensure_browser()` + Playwright CDP 连接 → `upload_media_to_x()` 发帖
6. `finally`：删除 mp4 临时文件；自己启动的 Chrome 发帖后关闭（与现有一致）

**错误处理**：
- 接口请求失败 / `models` 为空 → 报错退出（exit 1）
- ffmpeg 不存在 → 报错并提示安装
- ffmpeg 录制失败（非零返回码）→ 报错退出，`finally` 清理临时文件
- 浏览器/发帖异常 → 沿用 `upload_grid_to_x.py` 现有的提示与退出逻辑

### 2. `upload_grid_to_x.py`（最小化改动）

- `upload_image_to_x(page, image_path, post_text)` 改名为 **`upload_media_to_x(page, media_path, post_text)`**，内部逻辑不变，仅：
  - 预览检测选择器扩展为 `[data-testid="attachments"] img, [data-testid="attachments"] video, [data-testid="filePreview"]`
  - 预览等待超时 30s → 90s；发布按钮等待超时 60s → 120s（X 处理视频比图片慢；图片路径下这些只是上限，正常不会变慢）
- 九宫格 `main()` 的调用点同步改名，其余零改动

## 数据流

```
推荐接口 → models[] → 选 viewersCount 最高且 status=public
       → stream.urls["480p"] (m3u8)
       → ffmpeg 15s 重编码 → stream_*.mp4 (H.264/AAC)
       → Playwright CDP → x.com/compose/post → 输入文案 → 上传视频
       → 等视频预览(≤90s) → 等发布按钮(≤120s) → 发布 → 删除临时 mp4
```

## 测试

- 新增 `tests/test_record_stream_to_x.py`：
  - 接口响应样本存为 `tests/sample_recommended_response.json`
  - 测 `pick_top_streamer`：viewersCount 排序正确、public 过滤、无 public 时兜底
  - 测文案渲染：`{username}` 占位符替换
- ffmpeg 录制与浏览器发帖为外部集成，不做自动化，手动验证一次

## 范围外（YAGNI）

- 不改动九宫格图片业务的行为与 cron 脚本
- 不做全站推荐（去掉 `modelsList` 参数）——仅预留改 `API_URL` 一处即可
- 不做视频时长/码率可配置化
