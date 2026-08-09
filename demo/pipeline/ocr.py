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
识别失败时返回 {"engine": "none", "reason": 原因码, "error": 提示}，
原因码用于让前端给出对症的引导（no_match 引导改用内置样例图库、
vlm_failed 提示稍后重试、vlm_unavailable 说明配额或限流已触发）。
"""
import base64
import hashlib
import io
import json
import os
import time
from pathlib import Path

import requests

# ---------- 多模态调用的瞬时故障重试 ----------
#
# 中转网关会在多个上游 key 之间轮询，轮到失效的那个就把上游的
# 400 "API key not valid" 原样透出来。实测：同一张图连打 10 次全成功，
# 走完整链路 14 张里有 3 张失败——取决于轮到哪个上游，与图片无关。
#
# 不重试的后果很具体：体验者上传一张正常照片，随机看到「识别失败」，
# 会以为是自己的照片有问题，而这恰恰是我们最想证明能处理好的环节。
_TRANSIENT = (
    "api key not valid",       # 网关轮到失效上游 key
    "upstream",                # 网关自报上游故障
    "no available channel",    # 该模型当前无可用通道
    "rate limit", "too many requests",
    "bad gateway", "service unavailable", "gateway timeout",
)
_RETRY_MAX = 3          # 首次 + 最多 2 次重试
_RETRY_SLEEP = 1.2      # 退避基数（秒）：1.2、2.4


def _is_transient(status: int, body: str) -> bool:
    """是否为「重试一次就可能过」的瞬时故障。

    只认上述特征或 429/5xx。模型名写错（404）、本地 key 配错（401）
    属于请求本身有问题，重试只是重复烧钱。
    """
    if status in (429, 502, 503, 504):
        return True
    low = (body or "").lower()
    return any(sig in low for sig in _TRANSIENT)


def _post_with_retry(url: str, payload: dict, headers: dict, timeout: int = 90):
    """POST + 瞬时故障重试。失败时把网关正文带进异常，否则完全无法定位。"""
    last = None
    for attempt in range(1, _RETRY_MAX + 1):
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code < 400:
            return resp
        body = (resp.text or "").strip()
        last = requests.HTTPError(
            "%s %s ← %s" % (resp.status_code, resp.reason, body[:400]), response=resp)
        if attempt >= _RETRY_MAX or not _is_transient(resp.status_code, body):
            raise last
        time.sleep(_RETRY_SLEEP * (2 ** (attempt - 1)))
    raise last

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SAMPLE_DIR = DATA_DIR / "sample_images"

# dHash 感知哈希的汉明距离阈值（2048 位哈希）：≤ 该值视为同一张作业照片。
# 阈值依内置样例集实测标定：重编码/缩放变体到本体 ≤ 42，错配最近 60，
# 样例间最小 65，无关图片 ≥ 285 —— 取 50 并叠加次优间隔校验。
DHASH_THRESHOLD = 50
# 最优命中与次优命中的最小汉明间隔，防止近似样例间误配
DHASH_MARGIN = 8

# 允许的上传图片格式与像素上限。像素上限用于挡「解压炸弹」——几十 KB 的
# PNG 可以解出上亿像素，直接把内存吃满；公开部署时这是必须的一道校验。
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "BMP"}
MAX_IMAGE_PIXELS = 40_000_000


def validate_image(image_bytes: bytes) -> str | None:
    """校验上传内容确实是一张可解码的常见格式图片。

    返回错误提示字符串；校验通过返回 None。除了挡解压炸弹，也避免把
    「伪装成图片的垃圾文件」原样发给多模态大模型白烧额度。
    Pillow 未安装时跳过校验（不影响纯文本链路可用）。
    """
    try:
        from PIL import Image
        try:
            from PIL.Image import DecompressionBombError
        except ImportError:                      # 老版本 Pillow 没有这个类
            class DecompressionBombError(Exception):
                pass
    except ImportError:
        return None
    # 校验顺序是刻意安排的，从最便宜的检查到最贵的：
    #   读文件头 → 格式白名单 → 像素上限 → 结构完整性
    # 关键在于「像素上限」必须早于任何解码动作。反过来的话，一个声明
    # 30000×30000 的解压炸弹会先让解码器去分配 9 亿像素，校验本身就成了
    # 拒绝服务的入口。
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            fmt, (width, height) = img.format, img.size    # 只读文件头，不解码
    except DecompressionBombError:
        # Pillow 自带的炸弹护栏（默认 89478485 像素）先于我们触发
        return "图片分辨率过大，疑似异常文件，请压缩后重试。"
    except Exception:
        return "无法解析为图片，请上传 PNG / JPG / WebP 格式的作业照片。"

    if fmt not in ALLOWED_FORMATS:
        return "暂不支持 %s 格式，请上传 PNG / JPG / WebP 作业照片。" % (fmt or "该")
    if width * height > MAX_IMAGE_PIXELS:
        return ("图片分辨率过大（%d×%d），请压缩后重试。" % (width, height))

    # 到这里尺寸已确认在安全范围内，才做结构校验。
    # verify() 只查 CRC / 结构而不解码像素，能抓住「文件头正常但数据被截断」
    # ——上传中断、网络断流都会产出这种文件。不查的话它会一路走到多模态
    # 接口那里才失败，白烧一次额度，还把底层报错甩给体验者。
    # 注意：verify() 后 img 对象即失效，故必须重新 open 一次。
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.verify()
    except DecompressionBombError:
        return "图片分辨率过大，疑似异常文件，请压缩后重试。"
    except Exception:
        return "图片文件不完整或已损坏（可能上传中断），请重新拍照上传。"
    return None


# 多模态识别的转写 Prompt：只转写、不批改，公式用线性写法
# K12 学科枚举。识别阶段由模型直接归类，**不再**与题库做相似度匹配。
#
# 为什么改：原先靠 difflib 逐字符比对转写文本与 3 道内置题，取最相似的一道。
# 题库只有 3 道题，真实作业几乎必然不在里面，于是代码被迫「硬套」——
# 一页导数题因为共享标点与字母，被判成英语作文，再用英语评分标准批出 0 分。
# 那不是阈值调错，是缺一条「判不出」的出口。模型本来就能读懂这张纸，
# 让它直接说学科，比让它去跟 3 道无关的题比相似度可靠得多。
SUBJECTS = (
    "语文", "数学", "英语", "物理", "化学", "生物",
    "政治", "历史", "地理", "科学", "信息技术", "其他",
)

VLM_PROMPT = """你是一个作业照片识别引擎。请识别图片中的题目与学生手写作答。

【识别要求】
1. 一张图可能有多道题。**每道题单独一条**，按图中出现顺序排列；
2. 每道题都要分别给出：
   - stem：印刷体题目原文（题干）。若题干被裁掉或未拍全，填写能看到的部分；
     完全没有印刷题干则填空字符串 ""；
   - answer：该题下学生手写的作答，逐字转写；学生未作答则填 ""；
   - subject：学科，**只能从这个列表里选**：%s；
3. 数学公式用线性写法（如 x^2-5x+6=0、v=s/t、e^x），保留换行；
4. 字迹无法辨认处用〔?〕占位，不要猜测其含义；
5. 不要把印刷体题干混进 answer，也不要把手写作答混进 stem；
6. 若题目旁印有分值（如「本题 12 分」），填入 printed_max_score，否则填 null；
7. 评估整张卷面清晰度 clarity（0-100：90+ 工整清晰，60-89 可辨认，60 以下潦草模糊）。

只输出 JSON，不要输出多余文字：
{"clarity": 数字,
 "questions": [
   {"index": 1, "subject": "学科", "stem": "题干原文",
    "answer": "学生手写作答", "printed_max_score": 数字或null}
 ]}""" % "、".join(SUBJECTS)


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
    resp = _post_with_retry(
        base_url + "/chat/completions", payload,
        {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    clarity = data.get("clarity", 75)
    try:
        clarity = max(0.0, min(100.0, float(clarity)))
    except (TypeError, ValueError):
        clarity = 75.0

    questions = _normalize_questions(data)
    # text 保留为「全部作答拼接」：批量流水线、教师台、样例匹配分支都按
    # 单一 text 字段消费，多题结构是增量而非替换。
    text = "\n\n".join(q["answer"] for q in questions if q["answer"]).strip()
    if not text:
        # 兼容模型仍按旧格式只回 {"text": ...} 的情形
        text = str(data.get("text", "")).strip()

    return {
        "engine": "vlm",
        "model": model,
        "text": text,
        "clarity": clarity,
        "questions": questions,
    }


def _normalize_questions(data: dict) -> list:
    """把模型返回的 questions 数组规整成可信结构。

    模型是自由文本生成器，不能假定它守约：subject 可能写「高中数学」或英文，
    index 可能重复或缺失，questions 可能整个缺失。逐项校验后再进业务，
    否则脏 subject 会一路流到看板的学科筛选里。
    """
    raw = data.get("questions")
    if not isinstance(raw, list):
        raw = []

    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not stem and not answer:
            continue          # 空条目直接丢，别在界面上留一行空题
        out.append({
            "index": len(out) + 1,        # 就地重编号，不信模型给的 index
            "subject": _normalize_subject(item.get("subject")),
            "stem": stem,
            "answer": answer,
            "printed_max_score": _normalize_printed_score(item.get("printed_max_score")),
        })

    # 旧格式兜底：只有 {"text": ...} 时也造出一条，让下游统一按 questions 走
    if not out:
        legacy = str(data.get("text") or "").strip()
        if legacy:
            out.append({
                "index": 1,
                "subject": _normalize_subject(data.get("subject")),
                "stem": str(data.get("stem") or "").strip(),
                "answer": legacy,
                "printed_max_score": None,
            })
    return out


def _normalize_subject(value) -> str:
    """把模型给的学科归一到 SUBJECTS 白名单，认不出就是「其他」。

    容忍「高中数学」「数学（理）」这类前后缀写法：白名单项作为子串命中即算。
    刻意不做同义词映射表——那又是一张需要维护的猜测表，而白名单已够用。
    """
    s = str(value or "").strip()
    if not s:
        return "其他"
    if s in SUBJECTS:
        return s
    for name in SUBJECTS:
        if name != "其他" and name in s:
            return name
    return "其他"


def _normalize_printed_score(value):
    """印刷题面上标注的分值。取不到或不合理就是 None。

    它**不参与**判分，只作为「试卷原始分值」展示，供教师换算。
    上限 300 是为了挡住模型把题号、年份当分值填进来。
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > 300:
        return None
    return round(n, 1)


def _sanitize_json_control_chars(text: str) -> str:
    """把 JSON 字符串值里的裸控制字符转成合法转义。

    多模态模型经常在 {"text":"..."} 里直接塞真实换行/制表符，而不是 \\n / \\t。
    标准 json.loads 会报 Invalid control character。这与图片无关，是输出格式不稳。
    只处理字符串内部；结构空白（键之间的换行）保持原样。
    """
    out: list[str] = []
    in_str = False
    escape = False
    for ch in text:
        o = ord(ch)
        if in_str:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\":
                out.append(ch)
                escape = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif o < 32:
                if ch == "\n":
                    out.append("\\n")
                elif ch == "\r":
                    out.append("\\r")
                elif ch == "\t":
                    out.append("\\t")
                else:
                    out.append("\\u%04x" % o)
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def _extract_json(content: str) -> dict:
    """从模型返回文本中提取 JSON（容忍代码块包裹，以及字符串内裸换行）。"""
    text = content.strip() if isinstance(content, str) else str(content)
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 常见失败：text 字段含未转义换行。修完再 parse；仍失败则原样抛出。
        return json.loads(_sanitize_json_control_chars(text))


# ---------- 统一识别入口 ----------

def recognize_image(image_bytes: bytes, mime: str = "image/png",
                    submissions: dict = None, allow_vlm: bool = True,
                    unavailable_note: str = "") -> dict:
    """作业照片统一识别入口。

    优先级：
    1. 配置了 ZHIPI_VLM_API_KEY 且本次允许调用 → 真实多模态识别
       （失败时若能匹配样例则降级）；
    2. 否则 → 感知哈希匹配内置样例，命中返回该样例标准转写；
    3. 都不行 → engine="none" + 原因码 + 引导提示。

    参数：
        image_bytes:       图片原始字节。
        mime:              图片 MIME 类型。
        submissions:       submission_id -> 作答记录，用于取匹配样例的预置转写。
        allow_vlm:         本次是否允许发起真实多模态调用。公开部署时由调用方
                           按日配额 / 限流结果传入 False，此时静默回落样例匹配，
                           而不是把「配额用尽」当成识别失败甩给体验者。
        unavailable_note:  allow_vlm=False 且无法回落时附加的说明（如配额已用尽）。
    """
    matched = match_sample(image_bytes)

    if vlm_configured() and allow_vlm:
        try:
            result = recognize_vlm(image_bytes, mime)
            if matched:
                result["matched_submission_id"] = matched
            return result
        except Exception as exc:
            if not matched:
                return {
                    "engine": "none",
                    "reason": "vlm_failed",
                    "error": "多模态识别调用失败：%s" % exc,
                }
            # 失败但命中样例 → 降级为演示识别

    if matched and submissions and matched in submissions:
        sub = submissions[matched]
        # 样例分支也给出 questions，让下游只认一种结构。内置样例恒为单题，
        # 学科与题面由调用方按 question_id 从题库回填（这里拿不到题库）。
        return {
            "engine": "sample-match",
            "text": sub["ocr"]["text"],
            "clarity": float(sub["ocr"]["clarity"]),
            "matched_submission_id": matched,
            "questions": [{
                "index": 1,
                "subject": "",          # 由 app.py 按题库填
                "stem": "",
                "answer": sub["ocr"]["text"],
                "printed_max_score": None,
            }],
        }

    if vlm_configured() and not allow_vlm:
        return {
            "engine": "none",
            "reason": "vlm_unavailable",
            "error": unavailable_note or "真实识别暂不可用，可先用内置样例作业照片体验完整链路。",
        }

    return {
        "engine": "none",
        "reason": "no_match",
        "error": ("这张照片不在内置样例库中：当前为离线演示模式，"
                  "只能识别内置的手写样例作业照片。"),
    }



# ---------- 兼容旧接口（文本流水线仍在使用） ----------

def mock_ocr(submission: dict) -> dict:
    """从内置作答数据取预置转写文本与清晰度分（文本批改流水线使用）。"""
    ocr = submission.get("ocr", {})
    return {
        "text": ocr.get("text", ""),
        "clarity": float(ocr.get("clarity", 0)),
    }
