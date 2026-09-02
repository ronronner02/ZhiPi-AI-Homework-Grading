# -*- coding: utf-8 -*-
"""一次性探针：用真实作业图跑完整识别入口，把每一阶段的失败原因原样打出来。

前端只显示「可能是网络或上游服务波动」，把 recognize_image 返回的 error
（含三阶段失败原因）丢掉了。这里直接调入口，并单独再跑一次三阶段，
定位到底是哪一步在挂。

    py -3 tools/probe_recognize_live.py [图片路径]
"""
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402  （import 副作用即加载 .env）
from pipeline import ocr  # noqa: E402

DEFAULT_IMG = (r"E:\希沃智教π\测评数据\测评数据\数学\学生页\83_student_2.jpg")
img_path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMG)
raw = img_path.read_bytes()
print("图片: %s (%.1f KB)" % (img_path.name, len(raw) / 1024))
print("普通超时 %ds / 强模型超时 %ds / 复识开关 %s / 复识预算 %d"
      % (ocr._normal_timeout(), ocr._strong_timeout(),
         ocr.refine_enabled(), ocr._refine_budget()))
print("=" * 60)

print("\n【1】入口 recognize_image（与界面完全同路）")
t0 = time.time()
res = ocr.recognize_image(raw, "image/jpeg")
print("  耗时 %.1fs  engine=%s" % (time.time() - t0, res.get("engine")))
if res.get("engine") == "none":
    print("  reason: %s" % res.get("reason"))
    print("  error : %s" % res.get("error"))
else:
    print("  staged=%s 题数=%d" % (res.get("staged"), len(res.get("questions") or [])))
    if res.get("staged_error"):
        print("  三阶段失败过，降级单次: %s" % res["staged_error"])

print("\n【2】单独跑三阶段 recognize_vlm_staged（拿完整堆栈）")
t0 = time.time()
try:
    st = ocr.recognize_vlm_staged(raw, "image/jpeg")
    print("  成功 %.1fs  题数=%d" % (time.time() - t0, len(st.get("questions") or [])))
except Exception:
    print("  失败 %.1fs" % (time.time() - t0))
    traceback.print_exc()

print("\n【3】单独跑单次 recognize_vlm（拿完整堆栈）")
t0 = time.time()
try:
    sg = ocr.recognize_vlm(raw, "image/jpeg")
    print("  成功 %.1fs  题数=%d" % (time.time() - t0, len(sg.get("questions") or [])))
except Exception:
    print("  失败 %.1fs" % (time.time() - t0))
    traceback.print_exc()
