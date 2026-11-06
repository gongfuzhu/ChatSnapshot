#!/usr/bin/env python3
"""通用图集转 PDF 工具（基于 Pillow，无需外部依赖）。

支持输入格式：webp / jpg / jpeg / png / bmp / tiff / gif（取首帧）。
特点：
  - 自然排序（00001, 00002 ... 不会排成 1,10,2），可递归子目录
  - 自动应用 EXIF 方向；透明/灰度/CMYK 统一转 RGB（PDF 不支持 alpha）
  - 单目录 -> 单个 PDF；--batch 对每个子目录各生成一个 PDF
  - 逐张转换、可选 --max-width 降采样，4GB 小内存机器也能跑大图集
  - 坏图跳过并汇总报告，不让单张失败中断整本

用法示例：
  # 1) 一个目录合成一个 PDF（输出 默认 目录名.pdf）
  python images_to_pdf.py everia_downloads/攝影師-luka-模特-dora-眼镜娘私拍

  # 2) 指定输出文件
  python images_to_pdf.py path/to/imgs -o /tmp/dora.pdf

  # 3) 批量：everia_downloads 下每个子目录各出一个 PDF，放到 pdf_output/
  python images_to_pdf.py everia_downloads --batch --outdir pdf_output

  # 4) 限制宽度到 1600px（减小 PDF 体积/内存），递归收集
  python images_to_pdf.py imgs --max-width 1600 --recursive

  # 5) 直接指定若干图片
  python images_to_pdf.py a.webp b.png -o merged.pdf
"""
import argparse
import os
import re
import sys

from PIL import Image, ImageOps, ImageFile

# 允许加载较大的图，避免 DecompressionBomb 报错；截断损坏文件不直接崩溃
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

IMG_EXTS = (".webp", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif")
# PDF 内 JPEG 质量（仅在需要重新编码时生效）
DEFAULT_QUALITY = 90


def natural_key(name):
    """自然排序键：把文件名中的数字段按数值比较，其余按小写文本比较。"""
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def collect_images(paths, recursive=False):
    """从文件/目录混合输入中收集图片路径，按自然顺序排序。"""
    files = []
    for p in paths:
        if os.path.isfile(p):
            if p.lower().endswith(IMG_EXTS):
                files.append(os.path.abspath(p))
        elif os.path.isdir(p):
            walker = os.walk(p) if recursive else [(p, [], os.listdir(p))]
            for root, _dirs, names in walker:
                for n in names:
                    if n.lower().endswith(IMG_EXTS):
                        files.append(os.path.abspath(os.path.join(root, n)))
        else:
            print(f"[警告] 路径不存在，跳过: {p}", file=sys.stderr)
    # 去重后自然排序
    return sorted(set(files), key=lambda x: natural_key(os.path.basename(x)))


def find_subdirs(root):
    """批量模式：列出 root 下含图片的直接子目录。"""
    result = []
    for name in sorted(os.listdir(root), key=natural_key):
        d = os.path.join(root, name)
        if os.path.isdir(d) and any(
            f.lower().endswith(IMG_EXTS) for f in os.listdir(d)
        ):
            result.append(d)
    return result


def load_rgb(path, max_width=None):
    """打开单张图并转成可写入 PDF 的 RGB，处理方向/透明/降采样。"""
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)          # 应用 EXIF 旋转
    if getattr(im, "is_animated", False):
        im.seek(0)                             # 动图取首帧
    if max_width and im.width > max_width:
        h = round(im.height * max_width / im.width)
        im = im.resize((max_width, h), Image.LANCZOS)
    if im.mode in ("RGBA", "LA", "P"):
        # 透明/调色板图铺白底再转 RGB，避免黑底
        bg = Image.new("RGB", im.size, (255, 255, 255))
        rgba = im.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    return im


def build_pdf(image_paths, out_pdf, max_width=None):
    """把一组图片合成单个多页 PDF。返回 (成功数, 失败列表)。"""
    if not image_paths:
        raise ValueError("没有可转换的图片")

    pages, failed = [], []
    for i, p in enumerate(image_paths, 1):
        try:
            pages.append(load_rgb(p, max_width=max_width))
        except Exception as e:
            failed.append((p, str(e)[:80]))
            print(f"  [跳过] 第{i}张无法读取: {os.path.basename(p)} ({str(e)[:50]})",
                  file=sys.stderr)
        if i % 20 == 0:
            print(f"  已处理 {i}/{len(image_paths)}")

    if not pages:
        raise RuntimeError("所有图片均无法读取，未生成 PDF")

    os.makedirs(os.path.dirname(os.path.abspath(out_pdf)), exist_ok=True)
    first, rest = pages[0], pages[1:]
    first.save(
        out_pdf, "PDF", save_all=True, append_images=rest,
        resolution=100.0, quality=DEFAULT_QUALITY,
    )
    # 显式释放
    for im in pages:
        im.close()
    return len(pages), failed


def default_pdf_name(src_dir, outdir):
    base = os.path.basename(os.path.abspath(src_dir)).strip() or "output"
    return os.path.join(outdir or os.path.dirname(os.path.abspath(src_dir)),
                        base + ".pdf")


def main():
    ap = argparse.ArgumentParser(
        description="通用图集转 PDF（webp/jpg/png 等 -> 多页 PDF）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="图片文件和/或目录（可多个）")
    ap.add_argument("-o", "--output", help="输出 PDF 路径（单目录/文件模式）")
    ap.add_argument("--outdir", help="批量模式下 PDF 的输出目录")
    ap.add_argument("--batch", action="store_true",
                    help="对输入目录下每个含图子目录各生成一个 PDF")
    ap.add_argument("--recursive", action="store_true",
                    help="单目录模式下递归收集所有子目录中的图片")
    ap.add_argument("--max-width", type=int, default=0,
                    help="超过该宽度的图等比缩小（px），0=不缩小")
    args = ap.parse_args()

    max_width = args.max_width or None
    total_ok, total_fail, made = 0, 0, []

    if args.batch:
        roots = [d for d in args.inputs if os.path.isdir(d)]
        if not roots:
            ap.error("--batch 需要至少一个目录作为输入")
        jobs = []
        for root in roots:
            for sub in find_subdirs(root):
                jobs.append((sub, collect_images([sub])))
        if not jobs:
            print("未发现含图片的子目录。")
            sys.exit(1)
        for sub, imgs in jobs:
            out = default_pdf_name(sub, args.outdir)
            print(f"[批量] {os.path.basename(sub)}（{len(imgs)} 张）-> {out}")
            ok, failed = build_pdf(imgs, out, max_width)
            total_ok += ok
            total_fail += len(failed)
            made.append(out)
            print(f"  ✅ {os.path.basename(out)}（{ok} 页"
                  + (f"，{len(failed)} 张失败" if failed else "") + "）")
    else:
        imgs = collect_images(args.inputs, recursive=args.recursive)
        if args.output:
            out = args.output
        elif len(args.inputs) == 1 and os.path.isdir(args.inputs[0]):
            out = default_pdf_name(args.inputs[0], args.outdir)
        else:
            ap.error("多个输入源时请用 -o 指定输出 PDF 路径")
        print(f"共收集 {len(imgs)} 张图片 -> {out}")
        ok, failed = build_pdf(imgs, out, max_width)
        total_ok, total_fail = ok, len(failed)
        made.append(out)
        print(f"✅ 完成：{out}（{ok} 页，{os.path.getsize(out) // 1024} KB"
              + (f"，{len(failed)} 张失败" if failed else "") + "）")

    print(f"\n合计生成 {len(made)} 个 PDF，{total_ok} 页，失败 {total_fail} 张。")
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
