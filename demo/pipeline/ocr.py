# -*- coding: utf-8 -*-
"""手写识别（OCR / 多模态）模块。

对应总体流程（详细设计方案 §7.1）中的「OCR / 公式 / 图形识别」环节。
支持两种识别引擎，保证「输入作业图片 → 输出批改结果」链路在任何环境都能演示：

- **vlm（真实多模态识别）**：配置 ZHIPI_VLM_API_KEY 后，把作业照片发给
  OpenAI 兼容的多模态大模型（默认阿里云百炼 qwen-vl-max，可换任意兼容服务），
  转写手写文字 / 公式 / 英文，并让模型自评卷面清晰度（0-100）。
- **sample-match（离线演示识别）**：未配置密钥时，对上传图片计算 SHA-256
  与 dHash 感知哈希，与内置 11 张手写样例图片（data/sample_images/，由
  tools/gen_sample_images.py 生成）比对；命中后返回该样例的标准转写，
  完整模拟「拍照 → 识别」体验，无网络也能现场演示。

两种引擎返回统一结构：
    {"engine": "vlm" | "sample-match",
     "text": 转写文本, "clarity": 清晰度 0-100,
     "matched_submission_id": 命中的内置作答 ID（仅 sample-match）}
识别失败时返回 {"engine": "none", "error": 提示}。
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SAMPLE_DIR = DATA_DIR / "sample_images"

# dHash 感知哈希的汉明距离阈值（2048 位哈希）：≤ 该值视为同一张作业照片。
# 阈值依内置样例集实测标定：重编码/缩放变体到本体 ≤ 42，错配最近 60，
# 样例间最小 65，无关图片 ≥ 285 —— 取 50 并叠加次优间隔校验。
DHASH_THRESHOLD = 50
# 最优命中与次优命中的最小汉明间隔，防止近似样例间误配
DHASH_MARGIN = 8

# 多模态识别的转写 Prompt：只转写、不批改，公式用线性写法
VLM_PROMPT = """你是一个手写作业识别引擎。请把图片中「学生手写的作答内容」逐字转写出来：
1. 只转写手写部分，忽略印刷体题目、姓名、班级抬头；
2. 数学公式用线性写法（如 x^2-5x+6=0、v=s/t），保留换行；
3. 若字迹无法辨认，用〔?〕占位；
4. 最后评估卷面清晰度 clarity（0-100：90+ 工整清晰，60-89 可辨认，60 以下潦草模糊）。
只输出 JSON：{"text": "转写文本", "clarity": 数字}"""


# ---------- 离线演示识别：感知哈希匹配内置样例 ----------

def dhash(image, hash_size: int = 32) -> int:
    """对 PIL 图像计算增强 dHash（差值感知哈希）。

    针对「同版式作业纸」低对比场景做了三点强化：
    1. 自动对比度拉伸，避免均匀纸面导致哈希坍缩；
    2. 水平 + 垂直双向梯度，捕捉笔画结构；
    3. 32×32 网格共 2048 位，能区分同题不同作答的细微差异。
    """
    from PIL import ImageOps
    gray = ImageOps.autocontrast(image.convert("L")).resize(
        (hash_size + 1, hash_size + 1), 2)  # 2 = BILINEAR
    px = gray.load()
    bits = 0
    for y in range(hash_size):
        for x in range(hash_size):
            bits = (bits << 1) | (1 if px[x, y] > px[x + 1, y] else 0)
    for y in range(hash_size):
        for x in range(hash_size):
            bits = (bits << 1) | (1 if px[x, y] > px[x, y + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """两个感知哈希（2048 位整数）的汉明距离。"""
    return bin(a ^ b).count("1")


_sample_index = None  # 惰性构建：[{sid, sha256, dhash}]


def _build_sample_index() -> list:
    """扫描内置样例图片，建立 SHA-256 + dHash 索引（进程内缓存）。"""
    global _sample_index
    if _sample_index is not None:
        return _sample_index
    index = []
    manifest_path = SAMPLE_DIR / "manifest.json"
    if manifest_path.exists():
        from PIL import Image  # 延迟导入，未安装 Pillow 时不影响其它功能
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        for sid, meta in manifest.items():
            fp = SAMPLE_DIR / meta["file"]
            if not fp.exists():
                continue
            raw = fp.read_bytes()
            with Image.open(io.BytesIO(raw)) as img:
                index.append({
                    "sid": sid,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "dhash": dhash(img),
                })
    _sample_index = index
    return index


def match_sample(image_bytes: bytes):
    """把上传图片与内置样例比对，命中返回 submission_id，否则 None。"""
    sha = hashlib.sha256(image_bytes).hexdigest()
    index = _build_sample_index()
    for item in index:
        if item["sha256"] == sha:
            return item["sid"]
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as img:
            h = dhash(img)
    except Exception:
        return None
    dists = sorted((hamming(h, item["dhash"]), item["sid"]) for item in index)
    if not dists or dists[0][0] > DHASH_THRESHOLD:
        return None
    # 次优间隔校验：与第二名距离过近说明无法可靠区分，宁可不匹配
    if len(dists) > 1 and dists[1][0] - dists[0][0] < DHASH_MARGIN:
        return None
    return dists[0][1]


# ---------- 真实多模态识别 ----------

def vlm_configured() -> bool:
    """是否配置了多模态识别密钥。"""
    return bool(os.environ.get("ZHIPI_VLM_API_KEY", "").strip())


def recognize_vlm(image_bytes: bytes, mime: str = "image/png") -> dict:
    """调用 OpenAI 兼容多模态大模型转写手写作业。

    环境变量：
        ZHIPI_VLM_API_KEY   多模态接口密钥（必填）
        ZHIPI_VLM_BASE_URL  默认阿里云百炼 compatible-mode
        ZHIPI_VLM_MODEL     默认 qwen-vl-max
    异常向上抛出，由调用方决定降级策略。
    """
    api_key = os.environ["ZHIPI_VLM_API_KEY"]
    base_url = os.environ.get(
        "ZHIPI_VLM_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = os.environ.get("ZHIPI_VLM_MODEL", "qwen-vl-max")

    data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(image_bytes).decode())
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": VLM_PROMPT},
            ],
        }],
        "temperature": 0,
        "stream": False,
    }
    resp = requests.post(
        base_url + "/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    clarity = data.get("clarity", 75)
    try:
        clarity = max(0.0, min(100.0, float(clarity)))
    except (TypeError, ValueError):
        clarity = 75.0
    return {
        "engine": "vlm",
        "model": model,
        "text": str(data.get("text", "")).strip(),
        "clarity": clarity,
    }


def _extract_json(content: str) -> dict:
    """从模型返回文本中提取 JSON（容忍 ```json 代码块包裹）。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


# ---------- 统一识别入口 ----------

def recognize_image(image_bytes: bytes, mime: str = "image/png",
                    submissions: dict = None) -> dict:
    """作业照片统一识别入口。

    优先级：
    1. 配置了 ZHIPI_VLM_API_KEY → 真实多模态识别（失败时若能匹配样例则降级）；
    2. 未配置 → 感知哈希匹配内置样例，命中返回该样例标准转写；
    3. 都不行 → engine="none" + 引导提示。

    参数：
        image_bytes: 图片原始字节。
        mime:        图片 MIME 类型。
        submissions: submission_id -> 作答记录，用于取匹配样例的预置转写。
    """
    matched = match_sample(image_bytes)

    if vlm_configured():
        try:
            result = recognize_vlm(image_bytes, mime)
            if matched:
                result["matched_submission_id"] = matched
            return result
        except Exception as exc:
            if not matched:
                return {
                    "engine": "none",
                    "error": "多模态识别调用失败：%s" % exc,
                }
            # 失败但命中样例 → 降级为演示识别

    if matched and submissions and matched in submissions:
        sub = submissions[matched]
        return {
            "engine": "sample-match",
            "text": sub["ocr"]["text"],
            "clarity": float(sub["ocr"]["clarity"]),
            "matched_submission_id": matched,
        }

    return {
        "engine": "none",
        "error": ("未能识别该图片：离线演示模式仅支持内置样例作业照片。"
                  "配置 ZHIPI_VLM_API_KEY 后可识别任意手写作业照片。"),
    }


# ---------- 兼容旧接口（文本流水线仍在使用） ----------

def mock_ocr(submission: dict) -> dict:
    """从内置作答数据取预置转写文本与清晰度分（文本批改流水线使用）。"""
    ocr = submission.get("ocr", {})
    return {
        "text": ocr.get("text", ""),
        "clarity": float(ocr.get("clarity", 0)),
    }
