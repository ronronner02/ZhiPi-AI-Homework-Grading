#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
智批π 方案配图渲染器
  静态图：Chrome headless 按固定视口截图 → PNG（2x）
  动  图：把 N 帧竖排渲染成一张"雪碧图"，一次截图后用 Pillow 切帧，
          再交给 ffmpeg 合成 GIF。这样每个动图只需 1~2 次 Chrome 调用，
          比逐帧启动浏览器快一个数量级。

用法：
  python render.py shot  <html> <out.png> <w> <h> [scale]
  python render.py frames <html> <out.gif> <w> <h> <n_frames> <fps> [batch]
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
# Chrome 单次截图的高度上限（受 GPU 纹理尺寸限制），雪碧图分批就靠它
MAX_SHOT_H = 15000


def chrome_bin() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到 Chrome / Edge 可执行文件")


def file_url(path: Path, query: str = "") -> str:
    """中文路径必须百分号编码，否则 Chrome 会当成无效 URL 静默失败"""
    u = "file:///" + quote(str(path.resolve()).replace("\\", "/"), safe="/:")
    return u + (("?" + query) if query else "")


def shot(html: Path, out: Path, w: int, h: int, scale: float = 2.0, query: str = "") -> Path:
    out = out.resolve()          # Chrome 以自己的工作目录解析相对路径，必须给绝对路径
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="zhipi-shot-"))
    cmd = [
        chrome_bin(),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--disable-lcd-text",            # 关闭次像素抗锯齿，导出后放大不带彩边
        "--font-render-hinting=none",
        "--virtual-time-budget=4000",    # 等字体与 CSS 动画就位
        f"--user-data-dir={profile}",
        f"--force-device-scale-factor={scale}",
        f"--window-size={w},{h}",
        f"--screenshot={out}",
        file_url(html, query),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    shutil.rmtree(profile, ignore_errors=True)
    if not out.exists():
        sys.stderr.write(r.stderr[-2000:] + "\n")
        raise SystemExit(f"截图失败：{html}")
    return out


def frames_to_gif(html: Path, out: Path, w: int, h: int, n: int, fps: int, batch: int = 0) -> Path:
    """雪碧图切帧 → GIF。html 需支持 ?from=A&to=B 渲染指定帧区间，竖排。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="zhipi-gif-"))
    if batch <= 0:
        batch = max(1, MAX_SHOT_H // h)

    idx = 0
    for start in range(0, n, batch):
        end = min(start + batch, n)
        sheet_png = tmp / f"sheet{start}.png"
        # 雪碧图按 1x 渲染：GIF 不需要 2x，且能塞下更多帧
        shot(html, sheet_png, w, h * (end - start), scale=1.0,
             query=f"from={start}&to={end}")
        sheet = Image.open(sheet_png).convert("RGB")
        for k in range(end - start):
            sheet.crop((0, k * h, w, (k + 1) * h)).save(tmp / f"f{idx:04d}.png")
            idx += 1
        print(f"  帧 {start}~{end - 1} 已渲染")

    vf = (f"fps={fps},scale={w}:-1:flags=lanczos,"
          "split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
           "-i", str(tmp / "f%04d.png"), "-vf", vf, "-loop", "0", str(out)]
    subprocess.run(cmd, check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main() -> None:
    a = sys.argv[1:]
    if not a:
        raise SystemExit(__doc__)
    mode = a[0]
    if mode == "shot":
        html, out, w, h = Path(a[1]), Path(a[2]), int(a[3]), int(a[4])
        scale = float(a[5]) if len(a) > 5 else 2.0
        p = shot(html, out, w, h, scale)
        print(f"OK {p}  {Image.open(p).size}  {p.stat().st_size // 1024}KB")
    elif mode == "frames":
        html, out, w, h = Path(a[1]), Path(a[2]), int(a[3]), int(a[4])
        n, fps = int(a[5]), int(a[6])
        batch = int(a[7]) if len(a) > 7 else 0
        p = frames_to_gif(html, out, w, h, n, fps, batch)
        print(f"OK {p}  {n}帧@{fps}fps  {p.stat().st_size // 1024}KB")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
