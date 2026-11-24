# -*- coding: utf-8 -*-
"""把明细里用到的怪物立绘、属性图标抓到本地，避免每次打开详情都回源 nanoka。

- 立绘：https://static.nanoka.cc/assets/hsr/monsterfigure/<file>.webp
  原图 2048×2048、约 1 MB，缩到宽 240 再存 WebP（每张 ~10 KB），
  既够卡片显示（最大 80×104），又不会把仓库撑大。
- 属性图标：https://static.nanoka.cc/assets/hsr/element/<name>.webp（几百字节，原样存）。

只补缺失文件：日常抓取新增的怪会多出几个文件，重复执行无副作用。
用法：python scripts/fetch_assets.py
"""
import glob
import io
import json
import os
import sys

import urllib.request

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MON_DIR = os.path.join(ROOT, "assets", "mon")
ELEM_DIR = os.path.join(ROOT, "assets", "elem")
ART_BASE = "https://static.nanoka.cc/assets/hsr/monsterfigure/"
ELEM_BASE = "https://static.nanoka.cc/assets/hsr/element/"
MON_W = 240          # 立绘目标宽度（卡片最大显示 80px，2x 屏也够）
QUALITY = 82

UA = {"User-Agent": "Mozilla/5.0"}


def get(url, timeout=60):
    """用标准库下载（不依赖 requests：这个脚本通常用带 Pillow 的 default 环境跑）"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def collect():
    """从明细里收集立绘文件名与用到的属性"""
    arts, elems = set(), set()
    for path in glob.glob(os.path.join(ROOT, "data", "details", "*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("art"), str):
                    arts.add(node["art"])
                if isinstance(node.get("weak"), list):
                    for e in node["weak"]:
                        elems.add(str(e).lower())
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
    return sorted(a for a in arts if a), sorted(e for e in elems if e)


def fetch_art(name):
    dst = os.path.join(MON_DIR, name)
    if os.path.exists(dst):
        return "skip"
    raw = get(ART_BASE + name)
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    if im.width > MON_W:
        h = round(im.height * MON_W / im.width)
        im = im.resize((MON_W, h), Image.LANCZOS)
    im.save(dst, "WEBP", quality=QUALITY, method=6)
    return "ok"


def fetch_elem(name):
    dst = os.path.join(ELEM_DIR, name + ".webp")
    if os.path.exists(dst):
        return "skip"
    with open(dst, "wb") as f:
        f.write(get(f"{ELEM_BASE}{name}.webp", timeout=30))
    return "ok"


def main():
    os.makedirs(MON_DIR, exist_ok=True)
    os.makedirs(ELEM_DIR, exist_ok=True)
    arts, elems = collect()
    print(f"明细里用到：{len(arts)} 张立绘、{len(elems)} 个属性图标")

    ok = skip = fail = 0
    for i, a in enumerate(arts, 1):
        try:
            st = fetch_art(a)
            ok += st == "ok"
            skip += st == "skip"
        except Exception as e:
            fail += 1
            print(f"  ✗ {a}: {type(e).__name__} {str(e)[:60]}")
        if i % 20 == 0:
            print(f"  … {i}/{len(arts)}")

    for e in elems:
        try:
            fetch_elem(e)
        except Exception as ex:
            fail += 1
            print(f"  ✗ {e}: {type(ex).__name__} {str(ex)[:60]}")

    total = sum(os.path.getsize(os.path.join(MON_DIR, f)) for f in os.listdir(MON_DIR))
    etotal = sum(os.path.getsize(os.path.join(ELEM_DIR, f)) for f in os.listdir(ELEM_DIR))
    print(f"立绘：新增 {ok} / 已有 {skip} / 失败 {fail}")
    print(f"本地资源：assets/mon {len(os.listdir(MON_DIR))} 个 {total/1024/1024:.2f} MB，"
          f"assets/elem {len(os.listdir(ELEM_DIR))} 个 {etotal/1024:.1f} KB")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
