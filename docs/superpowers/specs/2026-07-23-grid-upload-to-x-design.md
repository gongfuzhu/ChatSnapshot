# 九宫格自动上传到 X 设计文档

日期：2026-07-23

## 目标

一个脚本完成：生成直播封面九宫格图片 → 自动启动或连接 Chrome → 上传图片到 X (Twitter) 并发布。

在现有 `snapshot_grid.py`（生成九宫格）和用户提供的 OBS 上传脚本（Playwright 连接 CDP 上传视频）基础上整合，改为上传图片，并支持浏览器自动启动。

## 新文件

`upload_grid_to_x.py`

## 核心流程

1. **生成图片**：复用 `snapshot_grid.py` 的 `fetch_data / pick_9_covers / download_images / make_grid`，生成一张 `grid_YYYYmmdd_HHMMSS.jpg`。也支持命令行传入已有图片路径：`python upload_grid_to_x.py path/to/img.jpg`。
2. **准备浏览器**：
   - 先探测 `127.0.0.1:DEBUG_PORT` 端口。
   - 能连上 → `connect_over_cdp` 复用现有浏览器，标记 `started_by_us = False`。
   - 连不上 → 用 `subprocess` 启动真实 Chrome（`CHROME_PATH`），带 `--remote-debugging-port=DEBUG_PORT` 和 `--user-data-dir=USER_DATA_DIR`，轮询等端口就绪，标记 `started_by_us = True`。
3. **上传发帖**：打开 `X_COMPOSE_URL`，可选填 `POST_TEXT`，通过 `input[type=file]` 上传图片，等待渲染，等 `tweetButton` 可用后点击发布。
4. **清理**：`started_by_us == True` → 发帖后关闭 Chrome 进程；`False` → 保持开着。

## 配置项（脚本顶部）

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `CHROME_PATH` | `C:\Program Files\Google\Chrome\Application\chrome.exe` | Chrome 可执行文件路径 |
| `DEBUG_PORT` | `9222` | 远程调试端口 |
| `USER_DATA_DIR` | `%LOCALAPPDATA%\chrome-x-debug` | 自启 Chrome 的用户数据目录，可配置。独立目录时与日常 Chrome 隔离、稳定拿到调试端口；改成默认配置目录则免登录但要求日常 Chrome 全部关闭 |
| `X_COMPOSE_URL` | `https://x.com/compose/post` | 发帖页面 |
| `POST_TEXT` | `""` | 可选文案，空则纯图片发帖 |

## 关键决策

- **登录状态**：自动启动真实 Chrome，复用其登录态。
- **已开 Chrome 的处理**：先探测端口，能连就复用，连不上才启动新的。
- **发帖后**：脚本自己启动的关掉，复用已有的保持不动。
- **user-data-dir**：脚本顶部可配置，默认独立目录（首次需在该目录手动登录一次 X，之后保持登录）。

## 错误处理

以下情况打印清晰的中文提示和排查步骤，然后 `sys.exit(1)`：

- 找不到 `CHROME_PATH` 可执行文件。
- 端口在超时时间内未就绪（启动失败）。
- `connect_over_cdp` 连接失败。
- 上传 / 发布元素等待超时。

清理阶段（关闭自启 Chrome）用 `try/finally` 保证即便发帖异常也能执行。

## 结构

- `generate_grid() -> str`：生成图片返回路径。
- `is_port_open(host, port) -> bool`：探测端口。
- `launch_chrome() -> subprocess.Popen`：启动 Chrome 并等端口就绪。
- `upload_image_to_x(page, image_path)`：执行上传发帖。
- `main()`：编排流程，管理 `started_by_us` 与清理。

## 依赖

- 现有：`requests`、`pillow`（`requirements.txt` 已含）。
- 新增：`playwright`（需 `pip install playwright`，浏览器驱动用系统 Chrome 无需 `playwright install`）。将补充到 `requirements.txt`。
