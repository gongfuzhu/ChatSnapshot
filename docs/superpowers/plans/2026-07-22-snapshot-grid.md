# 直播封面九宫格生成脚本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实时调用 models 接口，随机挑 9 个模型封面，拼成 3×3 九宫格 JPG 保存到本地。

**Architecture:** 单文件脚本 `snapshot_grid.py`，4 个职责单一的函数顺序执行：`fetch_data`（拉接口）→ `pick_9_covers`（随机去重选 9 个并拼 URL）→ `download_images`（下载封面）→ `make_grid`（cover 裁剪 + 3×3 拼接保存）。纯逻辑（URL 拼接、抽样）用 pytest 针对文档里的样例 JSON 单测；网络/图像部分手动运行验证。

**Tech Stack:** Python 3.14, `requests`, `Pillow`；测试用 `pytest`。

## Global Constraints

- 封面 URL 规则（逐字）：`https://img.doppiocdn.org/thumbs/{snapshotTimestamp}/{id}`
- API URL（逐字）：`https://zh.streams.modelapp.org/api/front/v2/models?primaryTag=girls&limit=24&topLimit=61&favoritesLimit=24&msBlock=true&byw=false&flags=0&srwm=false&rcmGrp=A&rbCnGr=true&iem=true&decMb=true&ctryTop=true&mlfv=false&rectf=false&eab=false&nic=true&removeShows=true&uniq=tbm65h13l0wkexi2`
- 输出格式：JPG，quality=90，文件名 `grid_YYYYMMDD_HHMMSS.jpg`，保存在脚本所在目录
- 单元格默认 320×240，格间距 4px，3×3 网格，请求超时 15s
- 抽样：随机选一个 models 非空的 block → 从其 models 随机选一个模型，按 `id` 去重，重复 9 次；可用模型总数 < 9 时用实际数量，空位留白
- 当前目录不是 git 仓库 → 跳过所有 commit 步骤（不执行 `git`）

---

### Task 1: 环境准备与项目骨架

**Files:**
- Create: `requirements.txt`
- Create: `snapshot_grid.py`（仅常量头 + `if __name__` 占位）
- Create: `tests/sample_response.json`（来自封面接口.txt 的响应样例）

**Interfaces:**
- Consumes: 无
- Produces: 脚本顶部常量 `API_URL`, `IMG_BASE="https://img.doppiocdn.org/thumbs"`, `CELL_W=320`, `CELL_H=240`, `GAP=4`, `GRID=3`, `TIMEOUT=15`, `JPEG_QUALITY=90`, `USER_AGENT`（一个常见浏览器 UA 字符串）

- [ ] **Step 1: 安装依赖**

Run:
```bash
pip install requests pillow pytest
```
Expected: 成功安装（PIL 与 requests 可 import）。

- [ ] **Step 2: 写 requirements.txt**

```
requests
pillow
```

- [ ] **Step 3: 写脚本骨架 snapshot_grid.py**

```python
"""直播封面九宫格生成脚本。"""
import io
import random
import sys
from datetime import datetime

import requests
from PIL import Image

API_URL = (
    "https://zh.streams.modelapp.org/api/front/v2/models"
    "?primaryTag=girls&limit=24&topLimit=61&favoritesLimit=24&msBlock=true"
    "&byw=false&flags=0&srwm=false&rcmGrp=A&rbCnGr=true&iem=true&decMb=true"
    "&ctryTop=true&mlfv=false&rectf=false&eab=false&nic=true&removeShows=true"
    "&uniq=tbm65h13l0wkexi2"
)
IMG_BASE = "https://img.doppiocdn.org/thumbs"
CELL_W = 320
CELL_H = 240
GAP = 4
GRID = 3
TIMEOUT = 15
JPEG_QUALITY = 90
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


if __name__ == "__main__":
    pass
```

- [ ] **Step 4: 保存样例 JSON 到 tests/sample_response.json**

把 `封面接口.txt` 中 `响应` 之后的 JSON（从 `{` 到最后的 `}`）完整复制到 `tests/sample_response.json`。该样例含 3 个 block，其中 2 个 block 各有 1 个 model（`favoriteModels`、`recommendedModels`），第 3 个 `topStreamsModels` 的 models 为空。

- [ ] **Step 5: 验证脚本可导入**

Run:
```bash
python -c "import snapshot_grid; print(snapshot_grid.IMG_BASE)"
```
Expected: 输出 `https://img.doppiocdn.org/thumbs`

---

### Task 2: 封面 URL 拼接

**Files:**
- Modify: `snapshot_grid.py`
- Test: `tests/test_snapshot_grid.py`

**Interfaces:**
- Consumes: 常量 `IMG_BASE`
- Produces: `build_cover_url(model: dict) -> str` — 读取 model 的 `snapshotTimestamp` 与 `id`，返回 `f"{IMG_BASE}/{snapshotTimestamp}/{id}"`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_snapshot_grid.py`：
```python
import snapshot_grid as sg


def test_build_cover_url():
    model = {"id": 199570657, "snapshotTimestamp": "1784733180"}
    assert (
        sg.build_cover_url(model)
        == "https://img.doppiocdn.org/thumbs/1784733180/199570657"
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
pytest tests/test_snapshot_grid.py::test_build_cover_url -v
```
Expected: FAIL，`AttributeError: module 'snapshot_grid' has no attribute 'build_cover_url'`

- [ ] **Step 3: 实现 build_cover_url**

在常量之后加入：
```python
def build_cover_url(model):
    return f"{IMG_BASE}/{model['snapshotTimestamp']}/{model['id']}"
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
pytest tests/test_snapshot_grid.py::test_build_cover_url -v
```
Expected: PASS

---

### Task 3: 随机去重抽样 pick_9_covers

**Files:**
- Modify: `snapshot_grid.py`
- Test: `tests/test_snapshot_grid.py`

**Interfaces:**
- Consumes: `build_cover_url`
- Produces: `pick_9_covers(data: dict, n: int = 9) -> list[dict]` — 返回最多 n 个元素，每个元素为 `{"id": int, "url": str}`；按 `id` 去重；随机选 models 非空的 block 再随机选 model；可用模型不足 n 时返回实际数量（不死循环）

- [ ] **Step 1: 写失败测试**

在 `tests/test_snapshot_grid.py` 顶部加入：
```python
import json
import os

_SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "sample_response.json")
with open(_SAMPLE_PATH, encoding="utf-8") as f:
    SAMPLE = json.load(f)
```

追加测试：
```python
def test_pick_covers_dedup_and_limit():
    # 样例只有 2 个可用模型，请求 9 个应只返回 2 个且不重复
    picks = sg.pick_9_covers(SAMPLE, n=9)
    ids = [p["id"] for p in picks]
    assert len(picks) == 2
    assert len(set(ids)) == 2
    assert set(ids) == {199570657, 242433118}


def test_pick_covers_url_shape():
    picks = sg.pick_9_covers(SAMPLE, n=9)
    for p in picks:
        assert p["url"].startswith("https://img.doppiocdn.org/thumbs/")


def test_pick_covers_respects_n():
    picks = sg.pick_9_covers(SAMPLE, n=1)
    assert len(picks) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
pytest tests/test_snapshot_grid.py -v -k pick
```
Expected: FAIL，`has no attribute 'pick_9_covers'`

- [ ] **Step 3: 实现 pick_9_covers**

```python
def pick_9_covers(data, n=9):
    blocks = [b for b in data.get("blocks", []) if b.get("models")]
    # 汇总去重后的可用模型总数，作为兜底上限
    all_ids = {m["id"] for b in blocks for m in b["models"]}
    target = min(n, len(all_ids))

    picked = []
    seen = set()
    while len(picked) < target:
        block = random.choice(blocks)
        model = random.choice(block["models"])
        if model["id"] in seen:
            continue
        seen.add(model["id"])
        picked.append({"id": model["id"], "url": build_cover_url(model)})
    return picked
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
pytest tests/test_snapshot_grid.py -v
```
Expected: 全部 PASS

---

### Task 4: 数据获取 fetch_data

**Files:**
- Modify: `snapshot_grid.py`

**Interfaces:**
- Consumes: 常量 `API_URL`, `USER_AGENT`, `TIMEOUT`
- Produces: `fetch_data() -> dict` — GET API_URL（带 UA、超时），返回解析后的 JSON；失败打印错误并 `sys.exit(1)`

- [ ] **Step 1: 实现 fetch_data**

```python
def fetch_data():
    try:
        resp = requests.get(
            API_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[错误] 请求接口失败: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 2: 手动验证（需联网）**

Run:
```bash
python -c "import snapshot_grid as sg; d = sg.fetch_data(); print('blocks:', len(d.get('blocks', [])))"
```
Expected: 打印 `blocks: N`（N ≥ 1）。若网络不可用则打印错误并退出 1（属预期行为）。

---

### Task 5: 封面下载 download_images

**Files:**
- Modify: `snapshot_grid.py`

**Interfaces:**
- Consumes: 常量 `USER_AGENT`, `TIMEOUT`；`pick_9_covers` 产出的 `{"id", "url"}` 列表
- Produces: `download_images(covers: list[dict]) -> list[Image.Image]` — 逐张下载并用 Pillow 打开为 RGB；单张失败跳过并打印警告

- [ ] **Step 1: 实现 download_images**

```python
def download_images(covers):
    images = []
    for c in covers:
        try:
            resp = requests.get(
                c["url"], headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            images.append(img)
        except (requests.RequestException, OSError) as e:
            print(f"[警告] 下载封面失败 id={c['id']}: {e}", file=sys.stderr)
    return images
```

- [ ] **Step 2: 手动验证（需联网）**

Run:
```bash
python -c "import snapshot_grid as sg; d=sg.fetch_data(); c=sg.pick_9_covers(d); imgs=sg.download_images(c); print('下载成功:', len(imgs))"
```
Expected: 打印 `下载成功: K`（K 通常为 9，个别失败会 < 9）。

---

### Task 6: cover 裁剪辅助 fit_cover

**Files:**
- Modify: `snapshot_grid.py`
- Test: `tests/test_snapshot_grid.py`

**Interfaces:**
- Consumes: 无
- Produces: `fit_cover(img: Image.Image, w: int, h: int) -> Image.Image` — 居中裁剪缩放到恰好 w×h，不变形

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_snapshot_grid.py`：
```python
from PIL import Image as _Image


def test_fit_cover_exact_size():
    src = _Image.new("RGB", (1000, 500), "red")
    out = sg.fit_cover(src, 320, 240)
    assert out.size == (320, 240)


def test_fit_cover_tall_source():
    src = _Image.new("RGB", (100, 900), "blue")
    out = sg.fit_cover(src, 320, 240)
    assert out.size == (320, 240)
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
pytest tests/test_snapshot_grid.py -v -k fit_cover
```
Expected: FAIL，`has no attribute 'fit_cover'`

- [ ] **Step 3: 实现 fit_cover**

```python
def fit_cover(img, w, h):
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w, new_h = int(src_w * scale + 0.5), int(src_h * scale + 0.5)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
pytest tests/test_snapshot_grid.py -v -k fit_cover
```
Expected: PASS

---

### Task 7: 九宫格拼接与保存 make_grid

**Files:**
- Modify: `snapshot_grid.py`
- Test: `tests/test_snapshot_grid.py`

**Interfaces:**
- Consumes: `fit_cover`；常量 `CELL_W`, `CELL_H`, `GAP`, `GRID`, `JPEG_QUALITY`
- Produces: `make_grid(images: list[Image.Image], out_path: str) -> None` — 生成 3×3 画布（白底），每张 cover 填入格子，空位留白，保存为 JPG（quality=JPEG_QUALITY）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_snapshot_grid.py`：
```python
def test_make_grid_creates_jpg(tmp_path):
    imgs = [_Image.new("RGB", (400, 300), "green") for _ in range(9)]
    out = tmp_path / "grid.jpg"
    sg.make_grid(imgs, str(out))
    assert out.exists()
    result = _Image.open(str(out))
    expected_w = sg.GRID * sg.CELL_W + (sg.GRID - 1) * sg.GAP
    expected_h = sg.GRID * sg.CELL_H + (sg.GRID - 1) * sg.GAP
    assert result.size == (expected_w, expected_h)


def test_make_grid_handles_fewer_images(tmp_path):
    imgs = [_Image.new("RGB", (400, 300), "green") for _ in range(4)]
    out = tmp_path / "grid_partial.jpg"
    sg.make_grid(imgs, str(out))
    assert out.exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
pytest tests/test_snapshot_grid.py -v -k make_grid
```
Expected: FAIL，`has no attribute 'make_grid'`

- [ ] **Step 3: 实现 make_grid**

```python
def make_grid(images, out_path):
    canvas_w = GRID * CELL_W + (GRID - 1) * GAP
    canvas_h = GRID * CELL_H + (GRID - 1) * GAP
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    for idx in range(GRID * GRID):
        if idx >= len(images):
            break
        cell = fit_cover(images[idx], CELL_W, CELL_H)
        row, col = divmod(idx, GRID)
        x = col * (CELL_W + GAP)
        y = row * (CELL_H + GAP)
        canvas.paste(cell, (x, y))
    canvas.save(out_path, "JPEG", quality=JPEG_QUALITY)
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
pytest tests/test_snapshot_grid.py -v
```
Expected: 全部 PASS

---

### Task 8: 主流程 main

**Files:**
- Modify: `snapshot_grid.py`

**Interfaces:**
- Consumes: `fetch_data`, `pick_9_covers`, `download_images`, `make_grid`
- Produces: `main() -> None`；`if __name__ == "__main__": main()`

- [ ] **Step 1: 实现 main 并接线**

替换文件底部的 `if __name__` 块：
```python
def main():
    data = fetch_data()
    covers = pick_9_covers(data, n=GRID * GRID)
    if len(covers) < GRID * GRID:
        print(f"[警告] 可用模型仅 {len(covers)} 个，不足 9 个，空位将留白。")
    images = download_images(covers)
    if not images:
        print("[错误] 没有任何封面下载成功，退出。", file=sys.stderr)
        sys.exit(1)
    out_path = datetime.now().strftime("grid_%Y%m%d_%H%M%S.jpg")
    make_grid(images, out_path)
    print(f"[完成] 已生成九宫格: {out_path}（{len(images)} 张封面）")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 端到端运行（需联网）**

Run:
```bash
python snapshot_grid.py
```
Expected: 打印 `[完成] 已生成九宫格: grid_YYYYMMDD_HHMMSS.jpg（9 张封面）`，目录下出现该 jpg 文件。

- [ ] **Step 3: 打开生成的 jpg 肉眼检查**

确认是 3×3 九宫格、封面无明显变形/黑边、空位（如有）为白色。

---

## Self-Review

**Spec coverage:**
- 实时调接口 → Task 4 ✓
- 随机 block 再随机 model + 去重 + 兜底 → Task 3 ✓
- 封面 URL 规则 → Task 2 ✓
- 下载封面、单张失败跳过 → Task 5 ✓
- cover 裁剪不变形 → Task 6 ✓
- 3×3 拼接、空位留白、JPG quality=90、文件名格式 → Task 7 + Task 8 ✓
- 配置常量 → Task 1 ✓
- 错误处理（接口失败退出、不足 9 警告、单张失败跳过）→ Task 3/4/5/8 ✓

**Placeholder scan:** 无 TBD/TODO；每个代码步骤含完整代码。

**Type consistency:** `pick_9_covers` 产出 `{"id","url"}` 被 `download_images` 消费一致；`download_images` 产出 `list[Image]` 被 `make_grid` 消费一致；`fit_cover(img,w,h)` 在 Task 6 定义、Task 7 调用签名一致。

**Note:** 当前目录非 git 仓库，计划已省略 commit 步骤；如需版本管理可先 `git init`。
