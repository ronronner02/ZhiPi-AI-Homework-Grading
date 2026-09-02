# -*- coding: utf-8 -*-
"""同一张作业、同一套 prompt，换模型能差多少。

背景：deploy/.env 里主识别模型是 gemini-3.1-flash-lite-preview（轻量档），
而 gpt-5.6-luna 只配在 ZHIPI_LLM_*_2 上、仅用于判分的交叉验证，**不参与识别**。
用户手工把同一张图喂给 gpt-5.6-luna 能读出正确答案，我们的链路读错——那么问题
到底出在流水线，还是出在识别模型的档位上？这个脚本把变量收敛到只剩「模型」。

跑的是单次识别（recognize_vlm），不是三阶段：三阶段有版面、归属、复识三次调用，
换模型的影响会被摊薄，不利于看清模型本身的差距。

    py -3 tools/probe_model_gap.py [用例名]

消耗：每个模型 1 次整页识别。
"""
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402
from pipeline import ocr  # noqa: E402
from PIL import Image  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DATA = Path(r"E:\希沃智教π\测评数据\测评数据\数学\学生页")
CASES = {
    # 第 1 题：分式 x/(x+y) 的 x、y 同扩 2 倍 → 值不变 → D。学生潦草写 D。
    "hardwork_127": ("hardwork_127.jpg", "1", "D"),
    "hardwork_117": ("hardwork_117.jpg", "5", "1/(x+1)"),
}
NAME = sys.argv[1] if len(sys.argv) > 1 else "hardwork_127"
FILE, TARGET_NO, TRUTH = CASES[NAME]
MAX_EDGE = 1600          # 与前端 compressImage 一致


def upload_bytes(path):
    raw = path.read_bytes()
    if len(raw) <= 1200 * 1024:
        return raw
    with Image.open(io.BytesIO(raw)) as im:
        k = min(1, MAX_EDGE / max(im.size))
        o = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                     Image.LANCZOS)
        buf = io.BytesIO()
        o.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def run(img, label):
    t0 = time.time()
    try:
        data = ocr.recognize_vlm(img, "image/jpeg")
    except Exception as exc:
        print("  %-28s 失败 %s" % (label, str(exc)[:70]))
        return
    qs = data.get("questions") or []
    hit = None
    for q in qs:
        if str(q.get("no", "")).strip().rstrip(".") == TARGET_NO:
            hit = q
            break
    print("  %-28s %.1fs  %d 题" % (label, time.time() - t0, len(qs)))
    if hit is None:
        print("      第 %s 题：未识别到（题号可能被拆成小问）" % TARGET_NO)
    else:
        ans = (hit.get("answer") or "(空)").replace("\n", " ⏎ ")
        ok = TRUTH.replace(" ", "") in ans.replace(" ", "")
        print("      第 %s 题 %s answer=%s" % (TARGET_NO, "✔" if ok else "✘", ans[:80]))
    for q in qs[:4]:
        print("        %-5s %-5s %s" % (q.get("no"), q.get("qtype"),
                                        (q.get("answer") or "").replace("\n", " ")[:52]))


def main():
    img = upload_bytes(_DATA / FILE)
    with Image.open(io.BytesIO(img)) as im:
        dims = "%dx%d" % im.size
    print("\n%s  上传态 %s %d KB   人工事实：第 %s 题 = %s\n"
          % (FILE, dims, len(img) // 1024, TARGET_NO, TRUTH))

    run(img, "主识别 %s" % os.environ.get("ZHIPI_VLM_MODEL", "?"))

    key2 = os.environ.get("ZHIPI_LLM_API_KEY_2", "").strip()
    url2 = os.environ.get("ZHIPI_LLM_BASE_URL_2", "").strip()
    model2 = os.environ.get("ZHIPI_LLM_MODEL_2", "").strip()
    if not (key2 and url2 and model2):
        print("\n  未配置第二模型，跳过对比")
        return 0
    saved = {k: os.environ.get(k) for k in
             ("ZHIPI_VLM_API_KEY", "ZHIPI_VLM_BASE_URL", "ZHIPI_VLM_MODEL")}
    os.environ.update({"ZHIPI_VLM_API_KEY": key2, "ZHIPI_VLM_BASE_URL": url2,
                       "ZHIPI_VLM_MODEL": model2})
    try:
        run(img, "第二模型 %s" % model2)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return 0


if __name__ == "__main__":
    sys.exit(main())
