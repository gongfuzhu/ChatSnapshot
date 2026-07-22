# 直播封面九宫格生成脚本 — 设计文档

日期：2026-07-22

## 目标

编写一个 Python 脚本 `snapshot_grid.py`，实时调用直播平台的 models 接口，随机挑选 9 个模型的封面，拼成一张 3×3 九宫格图片（JPG）保存到本地。

## 技术选型

- **HTTP**：`requests`
- **图像处理**：`Pillow`
- 需要安装：`pip install requests pillow`（当前环境 Python 3.14.4，两者均未安装）

## 数据来源

实时请求接口：

```
https://zh.streams.modelapp.org/api/front/v2/models?primaryTag=girls&limit=24&topLimit=61&favoritesLimit=24&msBlock=true&byw=false&flags=0&srwm=false&rcmGrp=A&rbCnGr=true&iem=true&decMb=true&ctryTop=true&mlfv=false&rectf=false&eab=false&nic=true&removeShows=true&uniq=tbm65h13l0wkexi2
```

响应结构：`blocks[]` → 每个 block 含 `models[]`，每个 model 含 `id` 与 `snapshotTimestamp`。

封面 URL 拼接规则：

```
https://img.doppiocdn.org/thumbs/{snapshotTimestamp}/{id}
```

## 整体架构

单文件脚本，4 个职责单一的函数按顺序执行：

```
fetch_data()  →  pick_9_covers()  →  download_images()  →  make_grid()
   拉接口          随机选9个           下载封面             拼3x3并保存
```

## 组件细节

### fetch_data() -> dict
- GET 请求 API_URL，带 `User-Agent` 请求头，超时 15s。
- 返回解析后的 JSON dict。
- 失败时打印友好错误并以 exit code 1 退出。

### pick_9_covers(data) -> list[dict]
- 抽样逻辑：**随机选一个 block（仅在 `models` 非空的 block 中选）→ 从该 block 的 models 里随机选一个模型**，重复直到凑够 9 个。
- **去重**：用模型 `id` 集合记录已选，重复则重抽。
- **兜底**：先统计所有 block 汇总后的可用（去重）模型总数；若总数 < 9，则有多少用多少，并打印警告，避免死循环。
- 每个结果元素提取 `id`、`snapshotTimestamp`，拼出封面 URL。

### download_images(covers) -> list[Image]
- 逐张 `requests.get` 下载（超时 15s），用 Pillow 从字节流打开为 Image。
- 单张失败不影响整体：跳过并打印警告（最终列表可能 < 9 张）。

### make_grid(images) -> 保存文件
- 每个单元格统一尺寸 `CELL_W × CELL_H`（默认 320×240，4:3）。
- 每张图按 **cover 模式**（居中裁剪缩放）填满单元格，避免变形和黑边。
- 3×3 布局，格间距 `GAP`（默认 4px）。
- 空位（图片不足 9 张时）留白（白色背景）。
- 输出保存为 `grid_YYYYMMDD_HHMMSS.jpg`（JPG 格式，quality=90）到脚本所在目录。

## 配置项（脚本顶部常量）

| 常量 | 默认 | 说明 |
|------|------|------|
| `API_URL` | 上述完整 URL | 数据接口 |
| `IMG_BASE` | `https://img.doppiocdn.org/thumbs` | 封面前缀 |
| `CELL_W, CELL_H` | 320, 240 | 单元格尺寸 |
| `GAP` | 4 | 格间距(px) |
| `GRID` | 3 | 3×3 |
| `TIMEOUT` | 15 | 请求超时(秒) |
| `JPEG_QUALITY` | 90 | 输出 JPG 质量 |

## 错误处理

- 接口请求失败 → 打印错误并退出（exit 1）。
- 可用模型不足 9 → 警告，用实际数量拼，空位留白。
- 单张图下载/解码失败 → 跳过，该格留白。

## 输出

- 格式：**JPG**。
- 文件名：`grid_YYYYMMDD_HHMMSS.jpg`，保存在脚本所在目录。

## 测试方式

一次性小工具，无自动化测试。验证方式：运行脚本 → 检查生成的 jpg 为 3×3 九宫格拼图。
