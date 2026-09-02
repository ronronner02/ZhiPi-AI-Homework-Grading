# -*- coding: utf-8 -*-
"""分辨率对分式识别的影响：前端把图压到 1800px，是不是压掉了分数线。

观察到的矛盾：同一张 hardwork_117，同一套 prompt——

    tools/ab_recognize.py（直接喂原图）        第 6 题 (3) 读出 1/(b-a)   正确
    tools/check_review_wiring.py（1800px）    第 6 题 (3) 读成 b-a        错

两者只差前端 compressImage 那一步压缩。如果这就是原因，那么「学生写 1/(b-a)、
系统读成 b-a」这类错在真实链路上是**上传就注定的**，后面裁图放大也救不回来：
放大只是插值，原图里没留下的那条分数线不会因为放大而出现。

裁图复识时 _crop_region 会把小图放大到 1200-2200px，所以三组进模型的图尺寸相近，
差的只是信息量——这正是要验的东西。

    py -3 tools/probe_resolution.py

消耗：1 次版面解析 + 3 种分辨率 × ROUNDS 次局部读取。
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402
from pipeline import ocr  # noqa: E402
from PIL import Image  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

IMG = Path(r"E:\希沃智教π\测评数据\测评数据\数学\学生页\hardwork_117.jpg")
TARGET_NO = "6"
TRUTH = "1/(b-a)"          # (a-b)^2/(b-a)^3 = 1/(b-a)
SIZES = (1800, 2400, None)  # None = 原图，不压缩
ROUNDS = 2


def shrink(raw, max_edge):
    if max_edge is None:
        return raw
    with Image.open(io.BytesIO(raw)) as im:
        if max(im.size) <= max_edge:
            return raw
        k = max_edge / max(im.size)
        out = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                       Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def main():
    if not ocr.vlm_configured():
        print("未配置 ZHIPI_VLM_API_KEY")
        return 1
    raw = ocr.normalize_orientation(IMG.read_bytes(), "image/jpeg")
    with Image.open(io.BytesIO(raw)) as im:
        print("原图 %dx%d，%d KB" % (im.width, im.height, len(raw) // 1024))

    items = ocr._normalize_layout(ocr._call_vlm(raw, "image/jpeg", ocr.LAYOUT_PROMPT))
    q = next((it for it in items
              if str(it.get("no", "")).strip().rstrip(".") == TARGET_NO), None)
    if not q or not q.get("bbox"):
        print("没定位到第 %s 题" % TARGET_NO)
        return 1
    prompt = ocr.REFINE_PROMPT_HEAD % (q.get("no") or TARGET_NO,
                                       q.get("qtype") or "计算题",
                                       (q.get("stem") or "").strip()[:500] or "（未识别到题干）")
    print("第 %s 题   人工事实：(3) = %s\n" % (TARGET_NO, TRUTH))

    for size in SIZES:
        src = shrink(raw, size)
        with Image.open(io.BytesIO(src)) as im:
            src_dim = "%dx%d" % (im.size)
        crop = ocr._crop_region(src, q["bbox"], ocr._CROP_PAD)
        if not crop:
            print("-- %s: 裁图失败" % size)
            continue
        with Image.open(io.BytesIO(crop[0])) as im:
            crop_dim = "%dx%d" % (im.size)
        print("-- 源图 %-9s（%s）→ 裁图 %s，%d KB"
              % (size or "原图", src_dim, crop_dim, len(crop[0]) // 1024))
        for i in range(ROUNDS):
            try:
                d = ocr._call_vlm(crop[0], crop[1], prompt)
            except Exception as exc:
                print("     第 %d 次失败：%s" % (i + 1, exc))
                continue
            ans = str(d.get("answer") or "").replace("\n", " ⏎ ")
            hit = TRUTH.replace(" ", "") in ans.replace(" ", "")
            print("     %d) %s %s" % (i + 1, "✔" if hit else "✘", ans[:80]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
