# -*- coding: utf-8 -*-
"""一次性探针：直接打通 VLM 网关，把被前端吞掉的底层错误原样打出来。

前端对识别失败只显示一句「可能是网络或上游服务波动」，真实原因（模型名错、
key 失效、网关无通道、超时）全在 recognize_image 返回的 error 字段里，
而那个字段没有渲染出来。这里绕开整条链路直接调，看网关到底回了什么。

    py -3 tools/probe_vlm_live.py
"""
import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# 复用 app.py 的 .env 加载逻辑，保证读到的配置与真实服务一致
import app as _app  # noqa: E402  （import 副作用即加载 .env）
from pipeline import ocr  # noqa: E402

print("配置来源: %s" % _app._DOTENV_SOURCE)
print("VLM_BASE_URL: %s" % os.environ.get("ZHIPI_VLM_BASE_URL", "(未配)"))
print("VLM_MODEL   : %s" % os.environ.get("ZHIPI_VLM_MODEL", "(未配)"))
key = os.environ.get("ZHIPI_VLM_API_KEY", "")
print("VLM_API_KEY : %s" % (("已配 %d 位，尾号 %s" % (len(key), key[-4:]))
                            if key else "(未配)"))
print("普通识别超时: %d s" % ocr._normal_timeout())
print("强模型可用  : %s (%s)" % (ocr.strong_available(), ocr.strong_model_name()))
print("-" * 60)

# 造一张最小的合法图片，避免为探活烧掉一张大图的额度
from PIL import Image, ImageDraw  # noqa: E402
img = Image.new("RGB", (320, 160), "white")
ImageDraw.Draw(img).text((20, 60), "1 + 1 = 2", fill="black")
buf = io.BytesIO()
img.save(buf, format="PNG")
raw = buf.getvalue()
print("测试图: %d 字节" % len(raw))

t0 = time.time()
try:
    data = ocr._call_vlm(raw, "image/png", "这张图里写了什么？只回 JSON："
                                           '{"text":"..."}')
    print("成功 (%.1fs): %r" % (time.time() - t0, data))
except Exception as exc:
    print("失败 (%.1fs)" % (time.time() - t0))
    print("异常类型: %s" % type(exc).__name__)
    print("异常内容: %s" % exc)
    resp = getattr(exc, "response", None)
    if resp is not None:
        print("HTTP 状态: %s" % resp.status_code)
        print("网关正文: %s" % (resp.text or "")[:1200])
