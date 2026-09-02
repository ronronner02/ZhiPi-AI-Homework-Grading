# -*- coding: utf-8 -*-
"""手写识别（OCR / 多模态）模块。

对应总体流程（详细设计方案 §7.1）中的「OCR / 公式 / 图形识别」环节。
只有一种识别引擎：

- **vlm（真实多模态识别）**：配置 ZHIPI_VLM_API_KEY 后，把作业照片发给
  OpenAI 兼容的多模态大模型（默认阿里云百炼 qwen-vl-max，可换任意兼容服务），
  转写手写文字 / 公式 / 英文，并让模型自评卷面清晰度（0-100）。

早期版本还有一条 sample-match（感知哈希匹配内置合成样例 → 返回预置转写）的
离线分支，已随内置样例换成**真实作业原件**一并下线：预置转写等于把识别与
批改都跳过，体验者看到的是一条假链路；现在内置样例与自己上传的照片走的是
同一条路，兜底就没有存在的余地了。

返回结构：
    {"engine": "vlm", "text": 转写文本, "clarity": 清晰度 0-100,
     "questions": [...]}
识别失败时返回 {"engine": "none", "reason": 原因码, "error": 提示}，
原因码用于让前端给出对症的引导（vlm_not_configured 说明未配置识别密钥、
vlm_failed 提示稍后重试、vlm_unavailable 说明配额或限流已触发）。

## vlm 引擎内部：三阶段识别

一次「读懂这张纸」被拆成三步，各步只负责一件事：

1. **版面解析**（LAYOUT_PROMPT）：这页有哪些题、题在哪、纸上有哪些手写块。
   这一步**刻意不判断哪块字迹属于哪道题**——只报「有什么、在哪里」。
2. **答案归属**（ATTRIBUTION_PROMPT）：把第 1 步的题目清单与手写块清单一起
   交回模型，让它决定每块字迹属于哪道题并逐字转写。
3. **定向复识**（REFINE_PROMPT，按需）：对空答 / 含〔?〕/ 单字符 / 归属把握低的题，
   裁出该题区域放大后单独再问一次。

为什么要拆：单次调用里「找题」「归属」「转写」三件事互相干扰，最典型的失败是
**学生把答案写出了答题框**——写到相邻题旁边或页边空白处。单次调用时模型按空间
就近归属，那段字迹就被算到邻题上或干脆被跳过，该题变成「未作答」，判 0 分。
分两步之后，归属阶段看到的是「全部题 + 全部手写块」的全局信息，可以按内容
对应关系跨区域归属，而不是只能就近取。

第 3 步单独存在的理由：整页调用里模型的注意力摊在几十行字上，一个潦草的
单字母（选择题的 D）或一个分数（5/3）很容易读错，而这类错误一旦发生，
**后面所有环节都无法发现**——判分、二次批改、双模型交叉验证消费的都是
同一份已经错了的转写文本。所以复识必须发生在转写层，不能靠评分层的冗余。

三阶段任一步失败即整体降级回单次调用（recognize_vlm），保证链路不中断。
可用 ZHIPI_STAGED_RECOGNIZE=0 关闭分阶段、ZHIPI_REFINE_ANSWERS=0 关闭复识。
"""
import base64
import hashlib
import io
import json
import math
import os
import re
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

    520-527 是 Cloudflare 自己的状态码（524=上游没在它的窗口内回完），
    走 CDN 的中转网关会大量返回。它们描述的全是「网关到上游这一段出了事」，
    与我们发了什么无关——不重试的话，体验者会随机看到「识别失败」，
    而重跑一次通常就好。实测这条链路上 524 就是这样冒出来的。
    """
    if status in (429, 502, 503, 504) or 520 <= status <= 527:
        return True
    low = (body or "").lower()
    return any(sig in low for sig in _TRANSIENT)


def _post_with_retry(url: str, payload: dict, headers: dict, timeout: int = None):
    """POST + 瞬时故障重试。失败时把网关正文带进异常，否则完全无法定位。

    连接层异常（连接被重置、读超时）同样要重试：这类请求根本没拿到响应，
    之前直接冒泡出去、一次都不重试——实测连续跑识别时网关会间歇性
    reset（WinError 10054），一抖就是整次识别失败，三阶段里任何一阶段抖到
    都会拖垮整页。演示现场重跑一次就好的故障，不该让体验者看见「识别失败」。
    """
    last = None
    tmo = _normal_timeout() if timeout is None else timeout
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=tmo)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
            if attempt >= _RETRY_MAX:
                raise
            time.sleep(_RETRY_SLEEP * (2 ** (attempt - 1)))
            continue
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


# ---------- EXIF 方向归一化 ----------
#
# 手机拍的照片普遍是「像素横着存 + 一个 EXIF 方向标记说该转多少度」。
# 浏览器认这个标记，Pillow 的 Image.open **不认**。于是同一份字节在链路上的
# 不同环节被看成了两张不同朝向的图：
#
#   浏览器显示原图      读 EXIF → 竖向（正确）
#   Pillow 读尺寸       忽略 EXIF → 报横向尺寸
#   多模态模型          取决于它的解码器，无法假定
#   marks 画完存新图    Pillow 存 JPEG 不带 EXIF → 浏览器只能按横向显示
#
# 结果就是批改图相对原图整体转了 90°，痕迹全部错位。实测这批测评数据 32 张
# 里有 2 张 Orientation=8（3508×2484 的像素实际该竖着看），真机拍照的比例
# 远高于此。
#
# 与其在每个读图的地方各自记得转一次（漏一处就又错位），不如**在入口转一次**：
# 上传时就把像素转正、去掉方向标记，此后全链路只存在一种朝向。
_ORIENT_TAG = 274          # EXIF Orientation
_REENCODE_QUALITY = 95     # 只在需要旋转时重编码，质量给高避免二次损失


def normalize_orientation(image_bytes: bytes, mime: str = "") -> bytes:
    """按 EXIF 方向把图片旋转到正立，并去掉方向标记。

    方向为 1（正立）或无 EXIF 时**原样返回**，不做任何重编码——绝大多数图
    走这条路，一个字节都不动。只有真的需要旋转时才解码重存。

    任何异常都返回原始字节：方向归一化是体验改善，不该成为上传失败的新原因。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.getexif().get(_ORIENT_TAG) in (None, 1):
                return image_bytes
            fmt = (img.format or "").upper()
            fixed = ImageOps.exif_transpose(img)      # 返回的新图已不含方向标记
            out = io.BytesIO()
            if fmt == "PNG":
                fixed.save(out, format="PNG", optimize=True)
            elif fmt == "WEBP":
                fixed.save(out, format="WEBP", quality=_REENCODE_QUALITY)
            else:
                # JPEG 及其它：统一存 JPEG。RGBA/P 模式存 JPEG 会抛，先转 RGB。
                # 刻意不用 subsampling="keep"：exif_transpose 返回的是一张新图，
                # 它的 format 是 None，Pillow 会因此抛 ValueError——而这个异常
                # 会被下面的 except 吞掉，函数原样返回未旋转的字节，
                # 于是「加了归一化但一张也没转正」，且完全静默。
                if fixed.mode not in ("RGB", "L"):
                    fixed = fixed.convert("RGB")
                fixed.save(out, format="JPEG", quality=_REENCODE_QUALITY)
            return out.getvalue()
    except Exception:
        return image_bytes


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

# 题型枚举。分值推定、Rubric 拆步、以及「客观题不按步骤扣分」的判分规则都按
# 它分派，所以这里是个封闭集合——模型给了集合外的词就按 stem/answer 的形态
# 回退推断（见 _normalize_qtype），不新增类别。
#
# 定义位置刻意提到学生页 prompt 之前：识别阶段就要把题型读出来。以前题型只在
# 教师答案页那条路上存在，学生页不带题型，于是整页批改的 prompt 里题型恒为空，
# 模型看不出这是填空题，就会拿「步骤缺失」去扣一道只要答案的填空题的分。
QTYPES = ("选择题", "填空题", "判断题", "计算题", "解答题", "翻译题", "作文题", "简答题")

VLM_PROMPT = """你是一个作业照片识别引擎。请识别图片中的题目与学生手写作答。

【识别要求】
1. 一张图可能有多道题。**每道题单独一条**，按图中出现顺序排列；
2. 每道题都要分别给出：
   - no：卷面上印的题号原样（如「1」「(2)」「三、1」）；卷面没印题号则填 ""；
   - stem：印刷体题目原文（题干）。若题干被裁掉或未拍全，填写能看到的部分；
     完全没有印刷题干则填空字符串 ""。
     **每道题的 stem 只能是它自己那一小段**：完形填空、短文填空、语篇型填空里
     连续几个空共用一段短文时，不要把整段短文重复抄进每一道题的 stem——
     那样几道题的 stem 变成同一段文字，与教师答案页逐条比对时只有一道能配上，
     其余全被判成「题库里没有这道题」，分值和标准答案一起丢掉。
     只写这道题所在的那一句（含它自己的那个空），共用的上下文不要重复抄；
   - answer：该题学生手写的作答，逐字转写；学生未作答则填 ""；
   - subject：学科，**只能从这个列表里选**：%s；
   - qtype：题型，**只能从这个列表里选**：%s；
   - bbox：该题在**本页图片**中所占的矩形区域 [x0, y0, x1, y1]，
     用 0-1000 的整数表示相对坐标（左上角为 0,0，右下角为 1000,1000），
     范围要**框住该题的题干与学生作答**，不要把相邻题目框进来；
     实在判断不了位置就填 null，不要瞎填；
3. **学生的作答可能写出了答题框**——写到题目旁边、下方、页边空白，
   甚至写到相邻题目附近。归属时**以内容对应关系为准，不以纸面距离为准**：
   一段字迹里出现的数字、字母、算式若与某道题的题干相呼应，就归属那道题，
   即使它离另一道题更近。某题的答题框看起来是空的时，先在全页范围内找一遍
   有没有属于它的作答，确认找不到才填 ""；
4. 数学公式用线性写法（如 x^2-5x+6=0、v=s/t、e^x），保留换行；
5. 字迹无法辨认处用〔?〕占位，不要猜测其含义；
6. 不要把印刷体题干混进 answer，也不要把手写作答混进 stem；
7. 若题目旁印有分值（如「本题 12 分」），填入 printed_max_score，否则填 null；
8. 评估整张卷面清晰度 clarity（0-100：90+ 工整清晰，60-89 可辨认，60 以下潦草模糊）。

只输出 JSON，不要输出多余文字：
{"clarity": 数字,
 "questions": [
   {"index": 1, "no": "题号原样", "subject": "学科", "qtype": "题型",
    "stem": "题干原文", "answer": "学生手写作答",
    "printed_max_score": 数字或null, "bbox": [x0, y0, x1, y1]}
 ]}""" % ("、".join(SUBJECTS), "、".join(QTYPES))


# ---------- 教师页识别：从答案页建题库 ----------

# 教师页和学生页读的是同一张版式的纸，但要读出来的东西完全不同：
# 学生页要的是「这个学生写了什么」，教师页要的是「这题的标准答案是什么、值几分」。
# 用同一个 prompt 会把教师用红笔写的参考答案当成「学生作答」转写出来，
# 然后拿它去跟学生比——比出来当然全对。所以必须分成两个 prompt。
TEACHER_PROMPT = """你是一个作业答案页识别引擎。这张图是**教师用的答案页**，
上面印有题目，并由教师/教辅用红色（或其它颜色）笔迹填写了标准答案。
请逐题读出题面与标准答案，供后续给学生作业判分使用。

【识别要求】
1. 一页可能有多道题。**每道题单独一条**，按图中出现顺序排列；
2. 每道题给出：
   - no：卷面印的题号原样（如「1」「(2)」「三、1」）；没印题号则填 ""；
   - stem：印刷体题目原文（题干），逐字转写。这是后续与学生页对齐的依据，
     **务必完整准确**，不要概括、不要改写；
   - standard_answer：该题的标准答案（答案页上填写的内容）。
     选择题只写选项字母（如 "C"）；填空题写应填内容；
     解答题写完整解答要点；教师页该题空着则填 ""；
   - qtype：题型，**只能从这个列表里选**：%s；
   - printed_max_score：题目旁印的分值数字，没印则填 null。
     **只填卷面真的印了的数字**，不要按经验推断——推断由后续程序做，
     混进来会让教师以为这个分值是卷面上写的；
3. 数学公式用线性写法（如 x^2-5x+6=0、v=s/t、e^x），保留换行；
4. 字迹无法辨认处用〔?〕占位，不要猜测其含义；
5. 印刷体题干与手写答案不要互相混入；
6. 学科 subject **只能从这个列表里选**：%s。

只输出 JSON，不要输出多余文字：
{"subject": "学科",
 "questions": [
   {"index": 1, "no": "题号原样", "stem": "题干原文",
    "standard_answer": "标准答案", "qtype": "题型",
    "printed_max_score": 数字或null}
 ]}""" % ("、".join(QTYPES), "、".join(SUBJECTS))


def recognize_teacher_page(image_bytes: bytes, mime: str = "image/png") -> dict:
    """识别一张教师答案页，返回 {subject, questions:[...]}。

    只走真实多模态：答案页是教师现场上传的任意一张纸，没有「内置样例」
    可以匹配，离线哈希那条路在这里没有意义。未配置密钥时由调用方拦下并
    给出明确提示，而不是在这里造一份假题库——假题库会让后面每一题的
    「答案匹配度」都变成凭空捏造的数字。
    """
    data = _call_vlm(image_bytes, mime, TEACHER_PROMPT)
    subject = _normalize_subject(data.get("subject"))
    raw = data.get("questions")
    if not isinstance(raw, list):
        raw = []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or "").strip()
        answer = str(item.get("standard_answer") or "").strip()
        if not stem and not answer:
            continue
        out.append({
            "index": len(out) + 1,
            "no": str(item.get("no") or "").strip()[:12],
            "stem": stem,
            "standard_answer": answer,
            "qtype": _normalize_qtype(item.get("qtype"), stem, answer),
            "printed_max_score": _normalize_printed_score(item.get("printed_max_score")),
        })
    return {"subject": subject, "questions": out}


def _normalize_qtype(value, stem: str = "", answer: str = "") -> str:
    """把模型给的题型归一到 QTYPES；认不出就按题干/答案的形态推断。"""
    text = str(value or "").strip()
    for qt in QTYPES:
        if qt in text:
            return qt
    # 模型没给或给了集合外的说法（"单选"、"multiple choice"、"完形填空"…），
    # 按答案形态兜底：单个字母是选择，一两个词是填空，其余按简答。
    ans = (answer or "").strip()
    low = text.lower()
    if "选择" in text or "选项" in text or "choice" in low or "单选" in text:
        return "选择题"
    if "填空" in text or "blank" in low:
        return "填空题"
    if "翻译" in text or "translat" in low:
        return "翻译题"
    if "作文" in text or "writing" in low or "essay" in low:
        return "作文题"
    if len(ans) <= 3 and ans and all(c in "ABCDEFGT×√对错" for c in ans):
        return "选择题"
    if 0 < len(ans) <= 20 and "\n" not in ans:
        return "填空题"
    return "简答题"


# ---------- 真实多模态识别 ----------

def vlm_configured() -> bool:
    """是否配置了多模态识别密钥。"""
    return bool(os.environ.get("ZHIPI_VLM_API_KEY", "").strip())


def _strong_creds():
    """「强模型」凭据；没配置返回 None。

    为什么要分档：实测同一张 hardwork_127、同一套 prompt，主识别的轻量档模型
    把第 1 题读成「2x/2x+2y」（那是学生的推导过程），换成强模型读出「D（保持
    不变）」；第 2 题轻量档漏掉一整项，强模型完整。差距不在 prompt，在模型档位。
    代价是一次调用 60 秒上下、约十倍于轻量档，所以不做默认，由教师按需触发：
    一眼看上去就潦草的作业点一下「强模型重识别」，其余仍走快的那条。

    凭据优先取 ZHIPI_VLM_STRONG_*；没单独配就复用第二模型那套
    （ZHIPI_LLM_*_2），多数部署里它们本来就是同一个更强的模型。
    """
    key = (os.environ.get("ZHIPI_VLM_STRONG_API_KEY", "").strip()
           or os.environ.get("ZHIPI_LLM_API_KEY_2", "").strip())
    url = (os.environ.get("ZHIPI_VLM_STRONG_BASE_URL", "").strip()
           or os.environ.get("ZHIPI_LLM_BASE_URL_2", "").strip())
    model = (os.environ.get("ZHIPI_VLM_STRONG_MODEL", "").strip()
             or os.environ.get("ZHIPI_LLM_MODEL_2", "").strip())
    if not (key and url and model):
        return None
    return {"api_key": key, "base_url": url.rstrip("/"), "model": model}


def strong_available() -> bool:
    """是否配了强模型——前端据此决定要不要显示「强模型重识别」按钮。"""
    return _strong_creds() is not None


def strong_default() -> bool:
    """ZHIPI_VLM_STRONG=1 时所有识别默认走强模型（离线评测、精度优先的部署用）。"""
    return (str(os.environ.get("ZHIPI_VLM_STRONG", "")).strip().lower()
            in ("1", "true", "yes", "on")) and strong_available()


def strong_model_name() -> str:
    return (_strong_creds() or {}).get("model", "")


# ---------- 识别调用的超时预算 ----------
#
# 这两个值定的是「教师愿意等多久」，而不是「模型通常要多久」。取舍已经从
# 「课堂现场盯着屏幕等」改成「上传完人就可以走开，批完自动推飞书卡片」：
# 前者要求快速失败（宁可报错也别转圈），后者恰好相反——网关慢一点、
# 重试一轮都不该让一份作业作废，因为没人在等这一秒。
#
# 代价要说清楚：模型真的挂了时，也要等满这个时长才报错。这是为「不误杀慢
# 但会成功的请求」付的钱，在离开式批改下划算，在现场演示时不划算——
# 要演示就用下面两个环境变量调回 90 / 300。
#
# 放大效应必须记住：整页识别是**串行三阶段 + 逐题定向复识**，每一段各算一次
# 超时，每次还最多重试 3 遍。单次 300 秒不等于整页 300 秒，最坏是它的数倍。
_NORMAL_TIMEOUT_DEFAULT = 300
_STRONG_TIMEOUT_DEFAULT = 900


def _normal_timeout() -> int:
    """轻量档识别的单次超时秒数，可用 ZHIPI_VLM_TIMEOUT 覆盖（默认 300）。

    以前这里是写死的 90，连个环境变量都没有——现场想放宽只能改代码重启，
    而这恰恰是最需要按部署环境调的一个数（自建网关和官方直连能差一个量级）。
    """
    try:
        return max(30, min(1200, int(str(os.environ.get(
            "ZHIPI_VLM_TIMEOUT", "")).strip())))
    except (TypeError, ValueError):
        return _NORMAL_TIMEOUT_DEFAULT


def _strong_timeout() -> int:
    """强模型识别的单次超时秒数，可用 ZHIPI_VLM_STRONG_TIMEOUT 覆盖（默认 900）。

    强模型比轻量档慢一个数量级（实测整页 60-75 秒），但慢的尾部很长，
    所以预算给得很宽。注意：调大它救不了「网关主动断开」——实测中转在
    75 秒左右会 reset，那是服务端行为，只能靠缩短单次请求的工作量
    （小图、短 prompt）来避开，等再久也没用。
    """
    try:
        return max(60, min(1200, int(os.environ.get("ZHIPI_VLM_STRONG_TIMEOUT",
                                                    _STRONG_TIMEOUT_DEFAULT))))
    except (TypeError, ValueError):
        return _STRONG_TIMEOUT_DEFAULT


def _call_vlm(image_bytes: bytes, mime: str, prompt: str, strong: bool = False) -> dict:
    """把一张图 + 一段 prompt 发给多模态大模型，返回解析后的 JSON。

    学生页与教师页读的是同一张纸但问的是两件事，prompt 必须分开；
    发请求、重试、抠 JSON 这套管道是一样的，所以只在这里写一遍。

    strong=True 且配了强模型时改走强模型，并给更长的超时。

    环境变量：
        ZHIPI_VLM_API_KEY   多模态接口密钥（必填）
        ZHIPI_VLM_BASE_URL  默认阿里云百炼 compatible-mode
        ZHIPI_VLM_MODEL     默认 qwen-vl-max
        ZHIPI_VLM_STRONG_*  强模型凭据（不配则复用 ZHIPI_LLM_*_2）
        ZHIPI_VLM_STRONG    置 1 时所有识别默认走强模型
        ZHIPI_VLM_TIMEOUT         轻量档超时秒数，默认 300
        ZHIPI_VLM_STRONG_TIMEOUT  强模型超时秒数，默认 900
    异常向上抛出，由调用方决定降级策略。
    """
    creds = (_strong_creds() if strong else None)
    timeout = _strong_timeout() if creds else _normal_timeout()
    if creds is None:
        creds = {
            "api_key": os.environ["ZHIPI_VLM_API_KEY"],
            "base_url": os.environ.get(
                "ZHIPI_VLM_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
            "model": os.environ.get("ZHIPI_VLM_MODEL", "qwen-vl-max"),
        }
    api_key, base_url, model = creds["api_key"], creds["base_url"], creds["model"]

    data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(image_bytes).decode())
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0,
        "stream": False,
    }
    resp = _post_with_retry(
        base_url + "/chat/completions", payload,
        {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        timeout=timeout)
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    data["_model"] = model
    return data


def recognize_vlm(image_bytes: bytes, mime: str = "image/png",
                  strong: bool = False) -> dict:
    """调用 OpenAI 兼容多模态大模型转写手写作业（学生页）。"""
    data = _call_vlm(image_bytes, mime, VLM_PROMPT, strong=strong)
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
        "model": data.get("_model", ""),
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
            "no": str(item.get("no") or "").strip()[:12],
            "subject": _normalize_subject(item.get("subject")),
            "qtype": _normalize_qtype(item.get("qtype"), stem, answer),
            "stem": stem,
            "answer": answer,
            "printed_max_score": _normalize_printed_score(item.get("printed_max_score")),
            "bbox": _normalize_bbox(item.get("bbox")),
        })

    # 旧格式兜底：只有 {"text": ...} 时也造出一条，让下游统一按 questions 走
    if not out:
        legacy = str(data.get("text") or "").strip()
        if legacy:
            stem = str(data.get("stem") or "").strip()
            out.append({
                "index": 1,
                "no": "",
                "subject": _normalize_subject(data.get("subject")),
                "qtype": _normalize_qtype("", stem, legacy),
                "stem": stem,
                "answer": legacy,
                "printed_max_score": None,
                "bbox": None,
            })
    return out


# ---------------------------------------------------------------------------
# 三阶段识别：版面解析 → 答案归属 → 定向复识
# ---------------------------------------------------------------------------
#
# 为什么把「找题」和「归属答案」拆成两次调用，见模块顶部说明。核心是：
# 归属阶段必须**同时看到全部题目与全部手写块**才能做跨区域归属，而单次调用里
# 模型是边找题边就近取答案的，一旦学生写出答题框就必然归错或漏掉。
#
# 代价是每页多一次图片调用（图片 token 翻倍）。这是刻意付的：判分错误里
# 「作答没被认到」这一类是最严重的——学生答对了却记 0 分，教师复核时看到的
# 转写是空的，连「系统看错了」都判断不出来。


def staged_enabled() -> bool:
    """是否启用分阶段识别。置 0 回落单次调用（成本敏感场景 / 排障用）。"""
    return os.environ.get("ZHIPI_STAGED_RECOGNIZE", "1").strip() != "0"


def refine_enabled() -> bool:
    """是否启用第三阶段的定向复识。"""
    return os.environ.get("ZHIPI_REFINE_ANSWERS", "1").strip() != "0"


def _refine_budget() -> int:
    """单页最多复识几道题。潦草的页可能整页都需要复识，但那已经该转人工了，
    不值得为它打十几次调用——上限之外的题留着由置信度分流去接。"""
    try:
        return max(0, min(20, int(os.environ.get("ZHIPI_REFINE_MAX", "6"))))
    except (TypeError, ValueError):
        return 6


LAYOUT_PROMPT = """你是一个作业照片的**版面解析**引擎。
这一步只做版面解析：找出纸上有哪些题、有哪些手写块、各自在什么位置。
**不要判断哪块字迹属于哪道题**——那由下一步完成。

【任务一：找出所有题目】
每道题一条，按图中出现顺序排列，给出：
- no：卷面上印的题号原样（如「1」「(2)」「三、1」）；没印则填 ""；
- stem：印刷体题干原文，逐字转写。**这是下一步归属答案的唯一依据，务必完整**，
  不要概括、不要改写；题干被裁掉则转写能看到的部分。
  **印刷的数学公式必须照样转写出来**，用线性写法：分式写成 a/b、(x+5)/(2x+1)，
  指数写成 x^2，根号写成 √3，保留换行。分数线是印刷排版的一部分，
  **不是留给学生填空的横线**——把公式吞成 ___ 会让题干只剩
  「约分：(1)___；(2)___」，与教师答案页上的同一道题再也对不上，
  判分时这道题就成了「题库中无此题」，分值退回按题型推定，教师白改一遍。
  **只有学生手写填上去的内容才用 ___ 占位**：答题横线上、括号里、方框内、
  田字格中的**手写字迹**。分辨只看笔迹本身（手写有连笔、粗细不匀、基线起伏、
  墨色与印刷不同），不看它出现在什么位置、也不看写得好不好看——学生写得工整时
  最容易被当成印刷体，一旦混进 stem，这道题就变成「学生没作答」，他的答案和
  分数一起消失；
- subject：学科，只能从这个列表里选：%s；
- qtype：题型，只能从这个列表里选：%s；
- printed_max_score：题旁印的分值数字，没印填 null；
- bbox：该题**印刷部分**所占矩形 [x0,y0,x1,y1]，用 0-1000 的整数相对坐标
  （左上角 0,0，右下角 1000,1000）；判断不了填 null；
- answer_area：该题留给学生作答的印刷空白区（横线、方框、空白行）的矩形，
  同样用 0-1000 相对坐标；判断不了填 null。

**大题标题、题型说明不是题**：像「四、从方框中选词并用其适当形式填空，使短文
完整、通顺」「二、选择题（每小题 2 分）」这样，整句都在交代下面那组小题该怎么做、
自身既没有设问也没有要填的空——不要把它单列成一条题。列出来它必然是「学生
没作答」，白扣一道题的分，还会把下面小题的题号挤乱。判断只看它自己有没有
需要作答的地方，不看它编号是「四、」还是「1.」：「四、计算下列各题」后面
跟着的那些算式才是题，标题那一行不是。

**页首页尾续过来的题照样要列**：纸的最上方常有上一页续下来的半道题（题号可能
从「4」开头，甚至只剩一个答句），最下方也可能有开了个头的下一题。它们上面同样
有学生的作答，漏掉它就等于把那道题的分数扔了。看不出题号就把 no 填 ""，
不要因为「不完整」「不像一道题的开头」而跳过。

**双栏卷子上，一道题的后半截可能印在另一栏顶部**：那是上一题的一部分，不要
另起一条。判断依据是它能不能独立成题——像「—No, I don't.」这样只有答句、
没有设问的片段不能独立成题，应当并进它所属的那道题。

【任务二：找出所有手写块】
纸上每一处**学生手写**的连续内容各算一块，**与题目无关地独立列出**：
- id：从 1 开始递增的整数；
- bbox：该手写块的矩形范围，0-1000 相对坐标；
- brief：该块内容的简短摘录（30 字以内，供下一步归属判断用，不必完整转写）。
  这一块被划掉 / 涂黑 / 打叉时，摘录前加「【已划掉】」——学生改过的地方常常
  新旧两个答案并排，分不出哪个作废，判分就会拿他自己已经否掉的那个去比对；
  笔迹明显比同页别处**淡**时（擦过的铅笔残影、写坏擦掉后重写的底稿、
  背面透过来的墨迹），摘录前加「【淡痕】」。数学卷上学生常先用铅笔打草稿、
  擦掉再用黑笔写正式解答，两份叠在同一处；淡的那份是他已经废弃的，
  拿它去判分等于按草稿给分；
- near_no：纸面上离这块字迹最近的印刷题号（填题号原样）；判断不了填 ""。

**一道题的作答被分散写在几处时，每一处各算一块**：学生常在主答题区写一段，
再把算式或最终答案补在页边、中缝、题目右侧、甚至下一题旁边。这几处之间隔着
印刷内容或大片空白，**不要合并成一个大框**——合并出来的框往往框不住写得最远
的那一段，那段文字就连同它的位置一起丢了，而它多半正是最终答案。

**手写块清单必须覆盖纸上所有学生笔迹**，包括写在页边的、挤在两题之间的、
以及明显写出了答题区的内容。不要因为「看不出它属于哪道题」就漏掉某一块——
归属是下一步的事，这一步漏了它就再也回不来了。
姓名、班级、日期这类也照样列出，下一步会把它们排除。

**只列纸上真的看得见的笔迹**：某道题的答题区是空白的，就不要为它列手写块，
也不要把 brief 写成「他应该会这样解」。凭题目内容推想出一段解答填进去，
是所有错误里最坏的一种——它读得通、格式对、看不出任何异常，会一路走完
归属、判分、分流，最后教师看到的是系统替学生写的解法，而纸上根本没有这些字。
空白就是空白，宁可这道题一块都不列。

【整页清晰度】
clarity：0-100（90+ 工整清晰，60-89 可辨认，60 以下潦草模糊）。

只输出 JSON，不要输出多余文字：
{"clarity": 数字,
 "questions": [
   {"index": 1, "no": "题号原样", "subject": "学科", "qtype": "题型",
    "stem": "题干原文", "printed_max_score": 数字或null,
    "bbox": [x0,y0,x1,y1], "answer_area": [x0,y0,x1,y1]}
 ],
 "writings": [
   {"id": 1, "bbox": [x0,y0,x1,y1], "brief": "内容摘录", "near_no": "题号"}
 ]}""" % ("、".join(SUBJECTS), "、".join(QTYPES))


# 单题区域的面积上限（占整页比例）。模型偶尔会把整页当成一道题的框返回，
# 拿它去画批改痕迹就是在页面正中盖一个大对勾。超过这个比例的框判为
# 「没框准」，退化成 None（画在页边默认位置），而不是照着画一个错的。
# 一页只有一道题时不适用——那种情况下整页确实就是这道题，由调用方放行。
BBOX_MAX_AREA = 0.5


def _normalize_bbox(value, max_area: float = BBOX_MAX_AREA):
    """把模型给的 [x0,y0,x1,y1] 归一到 0-1 浮点四元组，不可信则 None。

    模型报的是 0-1000 整数相对坐标，但实际会漂：给 0-1 小数的、给像素值的、
    左右颠倒的、超界的都见过。这里只认「能自圆其说」的框：四个数、能转浮点、
    修正顺序后仍有非零面积、且面积不至于大到框住半页。判不出就返回 None，
    让上层退回默认位置——**画错位置比不画更糟**，那会让教师以为系统看错了题。

    max_area 可放宽：一页只有一两道题时，单题占大半页是正常的，
    按默认上限会把正确的框也判掉（调用方按题数决定，见 _relax_area）。
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        nums = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if any(n != n or n in (float("inf"), float("-inf")) for n in nums):
        return None
    scale = 1000.0 if max(nums) > 1.5 else 1.0
    x0, y0, x1, y1 = (n / scale for n in nums)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(1.0, x1), min(1.0, y1)
    if x1 - x0 <= 0.01 or y1 - y0 <= 0.01:
        return None
    if (x1 - x0) * (y1 - y0) > max_area:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


ATTRIBUTION_PROMPT_HEAD = """你是一个作业**答案归属**引擎。
上一步已经从这张图里解析出题目清单与手写块清单（都列在下面）。
现在请判断**每一块手写内容属于哪道题**，并把归属到同一道题的内容逐字转写。

【归属原则】
1. **以内容对应关系为准，不以纸面距离为准。** 学生经常写出答题框：作答可能
   写在题目下方、右侧、页边空白，甚至挤到相邻题目附近。一块字迹里出现的
   数字、字母、算式、词句，只要与某道题的题干相呼应——用了该题给的条件、
   回答了该题所问、延续了该题上一行的推导——就归属那道题，
   **即使它在纸面上离另一道题更近**。
2. **一道题可以对应多个手写块**：作答被拆成几处写时，按纸面阅读顺序合并转写。
3. **一块手写也可能不属于任何题**：涂鸦、草稿验算、姓名班级日期，
   归到 question_index = 0，它们不会进入判分。
4. 一道题确实没有任何手写属于它（学生真的没做），就不要为它编造答案，
   answer 填 ""、confidence 照实给。

【转写要求】
- **以图为准，手写块清单只是线索**：上一步的块清单可能漏掉或框小了某几处
  笔迹。转写时请直接看图，把属于这道题的作答**全部**转写出来，包括写在
  页边、中缝、题目右侧、相邻题旁的补充算式与最终答案——那几行常常正是
  最终结果，漏掉它，这道题就会被判成「没算完」；
- 逐字转写，不要修正学生的错误，也不要补全他没写的内容；
- **不要概括、不要用自己的话描述学生在做什么**。写「计算判断是否触礁」
  这类描述是错的，只能转写纸上实际写着的字符；实在认不出的字用〔?〕占位；
- 数学公式用线性写法：x^2-5x+6=0、v=s/t、e^x；
- 分数一律写成 分子/分母（如 5/3），**不要折算成小数**；带分数写成 a b/c；
- **分式必须把分子和分母都读出来**：先找那条横的分数线，线**上方**是分子、
  **下方**是分母。分子是 1 时同样要写出来——写成 1/(b-a)，绝不能只写 b-a。
  **只读到分母就当成答案，是这类题最常见的错读**；
- 分子或分母是多项式时用括号括起来：1/(x+1)、(a-b)/(a+b)、(x-1)/(x^2-1)；
- 选择题只写选项字母（A / B / C / D）；判断题写 √ 或 ×；
- **学生划掉的内容不是他的答案**：被横线划去、涂黑、打叉的部分整段跳过，
  只转写他最后留下的那个。同一个空里新旧答案并排时取没被划掉的那个；
  两个都没划掉、分不出先后时，取写在后面（右侧或下方）的那个，
  并把 confidence 压到 0.6 以下——这种情况该让教师看一眼；
- **深浅两种笔迹叠在一处时只取深的**：擦掉的铅笔草稿会留下一层淡淡的残影，
  学生正式的解答压在它上面或写在旁边，墨色明显更深、笔画更实。只转写深色
  那一份，淡痕整段跳过——**把淡痕当成作答，是这类卷面最常见的整段错读**：
  深色的完整解题过程被丢掉，交上去的却是他擦掉不要的草稿。整道题只有淡痕、
  找不到深色作答时才转写淡痕，并把 confidence 压到 0.6 以下；
- 字迹无法辨认处用〔?〕占位，不要猜测其含义；
- **图上没有的字不要写进 answer**：这道题的答题区确实空着，answer 就填 ""。
  照着题目推想学生会怎么解、再把推想写成他的作答，比留空错得远——那段字
  带着完整的解题步骤，读得通、判得了分，后面没有任何一个环节能发现纸上
  根本不存在它。你只转写看得见的笔迹，看不见就是没有；
- 印刷体题干不要混进 answer。

【每道题输出一条】
- question_index：题目清单里的 index；
- writing_ids：归属到该题的手写块 id 列表（没有则空数组）；
- answer：合并后的完整转写；
- overflow：该题的作答是否写出了它自己的答题区（true / false）。
  **只要有任何一部分作答落在本题印刷区与答题空白之外**——写到页边、中缝、
  题目右侧、下一题旁边——就填 true，不必整段都在外面；
- confidence：0 到 1 的小数，表示你对「这段作答确实属于这道题」的把握；
- note：归属理由（20 字以内）。overflow 为 true 或 confidence < 0.7 时**必填**。

只输出 JSON，不要输出多余文字：
{"attributions": [
   {"question_index": 1, "writing_ids": [1,3], "answer": "转写内容",
    "overflow": true, "confidence": 0.9, "note": "归属理由"}
 ],
 "unassigned": [{"writing_ids": [5], "brief": "内容", "why": "草稿验算"}]}

【题目清单】
%s

【手写块清单】
%s"""


def build_attribution_prompt(questions: list, writings: list) -> str:
    """把阶段一的版面解析结果排成阶段二的输入清单。"""
    qlines = []
    for q in questions:
        head = "题 index=%d" % q["index"]
        if q.get("no"):
            head += "　卷面题号 %s" % q["no"]
        if q.get("qtype"):
            head += "　题型 %s" % q["qtype"]
        if q.get("bbox"):
            head += "　题目位置 %s" % _bbox_text(q["bbox"])
        if q.get("answer_area"):
            head += "　答题区 %s" % _bbox_text(q["answer_area"])
        qlines.append(head)
        qlines.append("  题干：%s" % ((q.get("stem") or "").strip()
                                     or "（未识别到印刷题干）"))
    wlines = []
    for w in writings:
        line = "手写块 id=%d　位置 %s" % (w["id"], _bbox_text(w.get("bbox")))
        if w.get("near_no"):
            line += "　纸面最近题号 %s" % w["near_no"]
        line += "　内容摘录：%s" % ((w.get("brief") or "").strip() or "（未提供）")
        wlines.append(line)
    return ATTRIBUTION_PROMPT_HEAD % (
        "\n".join(qlines) or "（未解析到题目）",
        "\n".join(wlines) or "（未找到任何学生手写内容）")


def _bbox_text(bbox) -> str:
    """把 0-1 相对坐标写成 prompt 里可读的 0-1000 整数框。"""
    if not bbox:
        return "未知"
    return "[%d,%d,%d,%d]" % tuple(round(v * 1000) for v in bbox)


def _relax_area(count: int) -> float:
    """题数少时放宽单题面积上限。

    一页就一道大题（导数题、作文）时，它本来就占大半页；按 0.5 的通用上限会把
    正确的框判成「没框准」，痕迹退回页边默认位置，反而更差。
    """
    if count <= 1:
        return 0.92
    if count == 2:
        return 0.70
    return BBOX_MAX_AREA


def _normalize_layout(data: dict) -> list:
    """把阶段一的题目清单规整成结构（此时还没有 answer）。"""
    raw = data.get("questions")
    if not isinstance(raw, list):
        raw = []
    kept = [it for it in raw if isinstance(it, dict)
            and (str(it.get("stem") or "").strip() or str(it.get("no") or "").strip())]
    area = _relax_area(len(kept))
    out = []
    for it in kept:
        stem = str(it.get("stem") or "").strip()
        out.append({
            "index": len(out) + 1,
            "no": str(it.get("no") or "").strip()[:12],
            "subject": _normalize_subject(it.get("subject")),
            "qtype": _normalize_qtype(it.get("qtype"), stem, ""),
            "stem": stem,
            "answer": "",                 # 阶段二填
            "printed_max_score": _normalize_printed_score(it.get("printed_max_score")),
            "bbox": _normalize_bbox(it.get("bbox"), area),
            "answer_area": _normalize_bbox(it.get("answer_area"), area),
        })
    return out


def _normalize_writings(data: dict) -> list:
    """把阶段一的手写块清单规整成结构。

    手写块的框**不设面积上限**：一大段解答本来就可能占半页，
    而这个框只用于阶段二的归属判断和阶段三的裁图，不用于画痕迹。
    """
    raw = data.get("writings")
    if not isinstance(raw, list):
        raw = []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        bbox = _normalize_bbox(it.get("bbox"), 1.0)
        brief = str(it.get("brief") or "").strip()[:120]
        if not bbox and not brief:
            continue
        out.append({
            "id": len(out) + 1,           # 就地重编号，阶段二按这个 id 回指
            "bbox": bbox,
            "brief": brief,
            "near_no": str(it.get("near_no") or "").strip()[:12],
        })
    return out


def _clamp01(value, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


# 能不能拿一个框当「学生作答的位置」用（批改痕迹的落点）。
# 与 _box_trustworthy 分开是因为两者防的不是同一件事：那个防的是「裁图裁不小」，
# 只挡竖条；这里防的是「记号落在离答案很远的地方」，要挡的是**退化框**——
# 横贯整页却几乎没有高度的一条（模型偶尔把一整行印刷横线报成手写块），
# 以及大到半页的框。落点错了比不落更糟：教师会以为系统判的是旁边那道题。
_ANSWER_BOX_MAX_AREA = 0.50
_ANSWER_BOX_FLAT = 0.06        # 高（或宽）低于此比例即视为退化


def _answer_box_ok(box) -> bool:
    if not box:
        return False
    w, h = box[2] - box[0], box[3] - box[1]
    if w <= 0 or h <= 0 or w * h > _ANSWER_BOX_MAX_AREA:
        return False
    if w >= 0.90 and h <= _ANSWER_BOX_FLAT:
        return False
    if h >= 0.90 and w <= _ANSWER_BOX_FLAT:
        return False
    return True


def _apply_answer_boxes(questions: list, writings: list) -> None:
    """给每道题算出**学生作答**所在的框（0-1 相对坐标），就地写进 answer_box。

    批改痕迹要落在学生写的那几个字上，而不是题目框里。这三个来源按可信度排：

    1. 归属阶段判给这道题的手写块的外接框 —— 最准，它就是「这道题的作答」
       在纸上的实际位置；
    2. 版面阶段标出的答题区 answer_area —— 印刷的横线 / 括号 / 答题框，
       学生没写或写得很淡时只有它；
    3. 都没有（或算出来的框退化了）→ None，由痕迹层退回题目框、再退到页边。

    每一层都过 _answer_box_ok：宁可退回「贴着题目画」，也不要拿一个横贯整页的
    框当答案位置——后者会把勾画到别的题旁边，而图上看不出这是坐标的问题。
    """
    by_id = {w["id"]: w for w in writings}
    for q in questions:
        boxes = [by_id[i].get("bbox") for i in q.get("writing_ids") or []
                 if i in by_id and _box_trustworthy(by_id[i].get("bbox"))]
        boxes = [b for b in boxes if _answer_box_ok(b)]
        union = _union_bbox(boxes)
        if not _answer_box_ok(union):
            union = None
        area = q.get("answer_area")
        q["answer_box"] = union or (area if _answer_box_ok(area) else None)


def _apply_attribution(questions: list, data: dict, writings: list) -> dict:
    """把阶段二的归属结果写回题目清单，返回归属统计。

    按 question_index 对齐而不是按数组下标：模型偶尔漏一条或多给一条，
    按下标硬对会让所有题整体错位——错位之后每道题的作答都挂在了别的题上，
    分数照出、证据链照给，是最难被发现的一类错误（与 pagegrader 同一考虑）。
    """
    rows = data.get("attributions")
    if not isinstance(rows, list):
        rows = []
    by_index = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("question_index"))
        except (TypeError, ValueError):
            continue
        if idx >= 1:
            by_index.setdefault(idx, row)

    valid_ids = {w["id"] for w in writings}
    overflow_count = 0
    for q in questions:
        row = by_index.get(q["index"]) or {}
        q["answer"] = str(row.get("answer") or "").strip()
        q["overflow"] = bool(row.get("overflow"))
        q["attribution_confidence"] = _clamp01(row.get("confidence"), 0.75)
        q["attribution_note"] = str(row.get("note") or "").strip()[:60]
        ids = row.get("writing_ids")
        q["writing_ids"] = [i for i in ids if i in valid_ids] \
            if isinstance(ids, list) else []
        if q["overflow"] and q["answer"]:
            overflow_count += 1

    assigned = {i for q in questions for i in q["writing_ids"]}
    return {
        "overflow_questions": overflow_count,
        "writings_total": len(writings),
        "writings_assigned": len(assigned),
        # 有手写块却没归到任何题：可能是草稿，也可能是归属漏了。如实报出来，
        # 让教师界面能提示「这页有 N 处笔迹未计入判分」。
        "writings_unassigned": len(valid_ids - assigned),
    }


def recognize_vlm_staged(image_bytes: bytes, mime: str = "image/png",
                         strong_refine: bool = False) -> dict:
    """三阶段识别：版面解析 → 答案归属 →（按需）定向复识。

    任一阶段抛异常都向上传递，由 recognize_image 决定降级为单次调用。
    strong_refine=True 时第三阶段改用强模型（教师点「强模型重读」时）。
    """
    layout = _call_vlm(image_bytes, mime, LAYOUT_PROMPT)
    questions = _normalize_layout(layout)
    if not questions:
        raise ValueError("版面解析未识别到任何题目")
    writings = _normalize_writings(layout)

    attr_data = _call_vlm(image_bytes, mime,
                          build_attribution_prompt(questions, writings))
    stats = _apply_attribution(questions, attr_data, writings)
    _apply_answer_boxes(questions, writings)

    refined = _refine_answers(image_bytes, mime, questions, writings,
                              strong=strong_refine) \
        if refine_enabled() else []

    try:
        clarity = max(0.0, min(100.0, float(layout.get("clarity", 75))))
    except (TypeError, ValueError):
        clarity = 75.0

    text = "\n\n".join(q["answer"] for q in questions if q["answer"]).strip()
    return {
        "engine": "vlm",
        "staged": True,
        "model": layout.get("_model", ""),
        "text": text,
        "clarity": clarity,
        "questions": questions,
        "layout_stats": stats,
        "refined": refined,
    }


# ---------- 阶段三：定向复识 ----------
#
# 触发这一步的四类题，都是「整页调用最容易读错、而读错之后再也无人发现」的：
#   〔?〕      模型自己都说认不出，还硬给了个转写
#   空答       题目在那儿、纸上有没归属的笔迹，却判成没做
#   短答案     选择题的单个字母、填空题的一个分数——笔画少，一笔之差就是另一个答案
#   低把握     归属阶段自报 confidence 低，或作答写出了答题框
#
# 为什么必须在这一层修：判分、二次批改、双模型交叉验证消费的都是这份转写文本。
# 转写错了，三道防线全部在错误的前提上达成一致，置信度还会因为「两次批改高度
# 一致」而升高——错得更自信。

REFINE_PROMPT_HEAD = """下面这张图是一份学生作业中**某一道题的局部放大**。

题号：%s
题型：%s
题干：%s

图中可能同时包含相邻题目的内容，**只转写属于上面这道题的学生手写作答**。
学生的作答可能写在题干下方、右侧或页边空白处。

【转写要求】
- 逐字转写，不要修正学生的错误，也不要补全他没写的内容；
- 选择题：只写选项字母（A / B / C / D）。字母潦草时按笔画走向仔细分辨，
  特别注意区分容易混的几组：D 与 O、B 与 8、C 与 G；
- 判断题：只写 √ 或 ×；
- 填空题：只写填的内容；
- 分数一律写成 分子/分母（如 5/3），**不要折算成小数、不要约分、不要写反分子分母**；
  带分数写成 a b/c；
- **分式必须把分子和分母都读出来**：先找那条横的分数线，线**上方**是分子、
  **下方**是分母。分子是 1 时同样要写出来——写成 1/(b-a)，绝不能只写 b-a。
  **只读到分母就当成答案，是这类题最常见的错读**；
- 分子或分母是多项式时用括号括起来：1/(x+1)、(a-b)/(a+b)、(x-1)/(x^2-1)；
- 数学公式用线性写法（x^2-5x+6=0、v=s/t、e^x），保留换行；
- **划掉的不算**：被横线划去、涂黑、打叉的内容整段跳过，只转写学生最后留下的
  那个答案；新旧两个答案并排且都没划掉时，取写在后面的那个并把 confidence
  压到 0.6 以下；
- **淡痕不算**：擦掉的铅笔草稿留下的淡淡残影不是作答。同一块地方深浅两种
  笔迹叠在一起时，只转写墨色深、笔画实的那一份，淡的整段跳过；
  整块只有淡痕时才转写它，并把 confidence 压到 0.6 以下；
- 确实找不到属于这道题的学生手写作答，answer 填 ""；
- 个别笔画无法辨认时用〔?〕占位，不要猜测。

只输出 JSON，不要输出多余文字：
{"answer": "转写内容", "legible": true/false, "confidence": 0到1的小数}"""

# 裁图外扩比例：作答常常越出题目框，只按框裁会把答案裁掉。
_CROP_PAD = 0.06
# 裁出来的小图放大到长边至少这么多像素——单个字母只占原图几十像素时，
# 不放大等于让模型继续看同样模糊的东西。
_CROP_MIN_EDGE = 1200
_CROP_MAX_EDGE = 2200
# 「短答案」的长度阈值：≤ 这个长度的作答按单字符/短填空对待，一律复识。
_SHORT_ANSWER_LEN = 3
_LOW_CONFIDENCE = 0.7

# 题干里出现分数线，或要求约分 / 通分，都意味着这道题的作答很可能是分式。
_FRACTIONAL_HINT = ("约分", "通分", "分式")
# 作答里的小问编号：(1)、（2）……
_SUBQ_RE = re.compile(r"[(（]\s*\d+\s*[)）]")


def _looks_fractional(stem: str) -> bool:
    stem = stem or ""
    return "/" in stem or any(k in stem for k in _FRACTIONAL_HINT)


def _fraction_gap(stem: str, answer: str) -> bool:
    """分式题的作答里，分数线是不是少了。

    不能只判「整段答案里有没有 /」：一道题的几个小问合并成一个字符串之后，
    只要有一个小问读出了分式，就会盖住另一个只读到分母的小问——实测第 6 题
    读成「(1) x^3/8; (2) -m/5n; (3) b-a」，(3) 明明丢了分子，整段却含两条
    分数线。所以按小问数比对：有几个小问，就该有几条分数线。

    约分结果偶尔确实是整式（(x^2-1)/(x-1) = x+1），这时会误判成需要复识。
    代价只是多花一次局部读取，而漏掉的那一侧是「学生答对却被判错、还带着
    高置信度自动通过」——两边不对等，宁可多读一次。
    """
    if not _looks_fractional(stem):
        return False
    subs = len(_SUBQ_RE.findall(answer or ""))
    if subs <= 1:
        return "/" not in (answer or "")
    return (answer or "").count("/") < subs


# 手写块 bbox 的形状下限。低于它就认为这个框本身不可信，不拿它去裁图。
#
# 模型报的块框会漂得很离谱：实测 page_29 右栏一篇短文填空，8 个横写的单词
# （parents、sisters、photos……）被报成宽 1%-8%、高 33%-48% 的竖条，
# 形状与内容完全对不上。这种框并进复识的裁图范围，会把大半页拉进来，
# 等于没裁——复识本来就是靠「裁小放大」才看得清的。
_BOX_MIN_RATIO = 0.28
_BOX_MIN_H = 0.20


def _box_trustworthy(bbox) -> bool:
    """这个手写块框的形状可不可信（用于决定要不要拿它去裁图）。"""
    if not bbox:
        return False
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w <= 0 or h <= 0:
        return False
    return not (h >= _BOX_MIN_H and w / h < _BOX_MIN_RATIO)


def _needs_refine(q: dict, has_unassigned: bool) -> str:
    """该题是否需要定向复识；返回原因码，不需要则返回 ""。"""
    answer = (q.get("answer") or "").strip()
    if "〔?〕" in answer:
        return "illegible"
    if not answer:
        # 学生真没做的题也会走到这里。只在「纸上还有没归属的笔迹」时才复识：
        # 空白卷不该为每道空题各打一次调用。
        return "blank_with_stray" if has_unassigned else ""
    # 分式题却读出一个不含分数线的答案：整页读时常常只抓到分母——实测学生写
    # 1/(b-a)，归属阶段读成 b-a，答案完全变了却没有任何异常信号（长度正常、
    # 归属把握高、没越界），上面几条一条都拦不住。而同一块字迹裁出来放大重读
    # 能稳定读对，所以这里值得单独花一次调用。
    # 选择题的转写落不到选项字母上：多半是把题旁的演算过程当成了答案，
    # 而真正勾选的那个字母还在括号里没被读到——实测 hardwork_127 第 1 题
    # 被读成「2x/2x+2y」（那是学生的推导），选项 D 就写在括号里。
    # 「答案根本不是答案」比字迹潦草更该重读，所以排在前面。
    if (q.get("qtype") or "") == "选择题" and not _is_choice_answer(answer):
        return "choice_mismatch"
    if _looks_fractional(q.get("stem")) and _fraction_gap(q.get("stem"), answer):
        return "fraction_mismatch"
    if q.get("attribution_confidence", 1.0) < _LOW_CONFIDENCE:
        return "low_confidence"
    if q.get("overflow"):
        return "overflow"
    if len(answer) <= _SHORT_ANSWER_LEN:
        return "short_answer"
    return ""


# 复识优先级：空答与认不出是「会直接判错分」的，排在前面；短答案量最大，
# 排最后——预算用完时被留下的是危害最小的一类。分式不匹配紧随其后：它同样
# 会直接判错分，而且比 low_confidence 更硬——那是模型自报的把握，
# 这是「答案结构与题目对不上」的客观矛盾。
_REFINE_PRIORITY = {
    "blank_with_stray": 0, "illegible": 1, "choice_mismatch": 2,
    "fraction_mismatch": 3, "low_confidence": 4, "overflow": 5, "short_answer": 6,
}


def _union_bbox(boxes: list):
    """若干 0-1 框的外接框。空列表返回 None。"""
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _crop_region(image_bytes: bytes, bbox, pad: float):
    """按 0-1 相对框裁出局部并放大，返回 (JPEG 字节, mime)；失败返回 None。

    放大是必要的而不是锦上添花：一个选择题的字母在 5000px 宽的原图里只占几十
    像素，整页调用时模型看到的就是这么点信息量。裁出来单独放大到 1200px 长边，
    同一个模型能看清笔画走向——这是「D 还是 O」这类判别唯一的抓手。
    """
    if not bbox:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as src:
            img = src.convert("RGB")
            w, h = img.size
            x0 = max(0, int((bbox[0] - pad) * w))
            y0 = max(0, int((bbox[1] - pad) * h))
            x1 = min(w, int((bbox[2] + pad) * w))
            y1 = min(h, int((bbox[3] + pad) * h))
            if x1 - x0 < 8 or y1 - y0 < 8:
                return None
            crop = img.crop((x0, y0, x1, y1))
            edge = max(crop.size)
            if edge < _CROP_MIN_EDGE:
                k = min(_CROP_MIN_EDGE / edge, _CROP_MAX_EDGE / edge)
                crop = crop.resize((max(1, round(crop.width * k)),
                                    max(1, round(crop.height * k))),
                                   Image.LANCZOS)
            elif edge > _CROP_MAX_EDGE:
                k = _CROP_MAX_EDGE / edge
                crop = crop.resize((round(crop.width * k), round(crop.height * k)),
                                   Image.LANCZOS)
            out = io.BytesIO()
            crop.save(out, format="JPEG", quality=92)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return None


# 干净的选项转写：允许括号、顿号、句点等修饰，但不能夹带算式或多行内容。
_CHOICE_ANSWER_RE = re.compile(r"^[\s（(]*[A-Ha-h]([\s、,，/]*[A-Ha-h])*[\s）)．.]*$")


def _is_choice_answer(text: str) -> bool:
    return bool(_CHOICE_ANSWER_RE.match((text or "").strip()))


def _refine_answers(image_bytes: bytes, mime: str, questions: list,
                    writings: list, strong: bool = False) -> list:
    """对高风险题裁图重识别，就地修正 questions 里的 answer。

    返回复识记录列表（每条含题号、原因、改前改后），供界面与评测复盘展示。
    **每一次改写都留痕**：转写被系统悄悄改过一次，而教师看到的是最终值，
    那么当结果仍然不对时，他无法判断是识别错还是判分错。
    """
    assigned = {i for q in questions for i in (q.get("writing_ids") or [])}
    has_unassigned = bool({w["id"] for w in writings} - assigned)
    by_id = {w["id"]: w for w in writings}

    todo = []
    for q in questions:
        reason = _needs_refine(q, has_unassigned)
        if reason:
            todo.append((_REFINE_PRIORITY.get(reason, 9), q["index"], reason, q))
    todo.sort(key=lambda t: (t[0], t[1]))
    todo = todo[:_refine_budget()]

    records = []
    for _prio, _idx, reason, q in todo:
        # 裁图范围要把「归属到这道题的手写块」也框进来——作答越界时它就在
        # 题目框之外，只按题目框裁会把答案本身裁掉，复识必然看不到。
        # 形状离谱的块框不并进来：它多半是模型漂出来的，并了反而把裁图撑到
        # 大半页，复识赖以看清笔画的那点放大倍数就没了。
        boxes = [q.get("bbox"), q.get("answer_area")]
        boxes += [b for b in ((by_id.get(i) or {}).get("bbox")
                              for i in (q.get("writing_ids") or []))
                  if _box_trustworthy(b)]
        region = _union_bbox(boxes)
        # 空答的题多外扩一些：它的作答大概率就在框外不远处，
        # 而我们并不知道具体在哪一侧。
        pad = 0.16 if reason == "blank_with_stray" else _CROP_PAD
        # 不在这里做旋转：图片在上传入口就已经过 normalize_orientation 转正立
        # 并抹掉了方向标记（app.py 的 _decode_upload / 拆页两处），版面阶段拿到
        # 的 bbox 也是按这份正立像素给的。这里再转一次，框和图就对不上了。
        crop = _crop_region(image_bytes, region, pad)
        if not crop:
            continue
        prompt = REFINE_PROMPT_HEAD % (
            q.get("no") or str(q["index"]),
            q.get("qtype") or "未判定",
            (q.get("stem") or "").strip()[:500] or "（未识别到印刷题干）")
        try:
            data = _call_vlm(crop[0], crop[1], prompt, strong=strong)
        except Exception:
            continue          # 复识失败就保留原转写，不该让整次识别失败
        new_answer = str(data.get("answer") or "").strip()
        before = (q.get("answer") or "").strip()
        # 只在复识给出了非空结果且与原值不同时改写。复识返回空不能覆盖原值：
        # 那可能只是裁图没框住答案，而原转写是对的。
        changed = bool(new_answer) and new_answer != before
        # 选择题的复识结果必须仍然是干净的选项字母。复识看的是放大后的局部图，
        # 常把题目旁边的演算过程一并读进来——实测有把干净的 "D" 换成
        # 「D / (x+1)(x-1) / -(x+1)」三行草稿的。原值本来就是合法选项时，
        # 这种「改写」只会让判分更难，不采纳。
        if (changed and (q.get("qtype") or "") == "选择题"
                and _is_choice_answer(before) and not _is_choice_answer(new_answer)):
            changed = False
        if changed:
            q["answer"] = new_answer
            q["refined"] = True
            # 两次读取给出了不同的转写。改写后的值**未必更对**——实测有
            # 「x-1 → x」这种越改越错的（正确答案是 1/(x+1)，两次都没读出分式）。
            # 它真正说明的是这块字迹读不稳，所以标出来请教师看一眼，
            # 而不是当成「已经修好了」。
            q["refine_changed"] = True
        q["refine_reason"] = reason
        if data.get("legible") is False:
            q["legible_hint"] = False
        records.append({
            "index": q["index"],
            "no": q.get("no", ""),
            "reason": reason,
            "before": before[:200],
            "after": new_answer[:200],
            "changed": changed,
            "confidence": _clamp01(data.get("confidence"), 0.75),
        })
    return records


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


#  从「12分」「（12分）」「本题 12 分」里取数字。
#  Prompt 要求返回纯数字，但模型照抄卷面写法是常态；直接 float() 会抛，
#  于是分值被静默丢成 None，教师就少了换算判别分的依据——而这正是
#  printed_max_score 这个字段存在的唯一目的。
_SCORE_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _normalize_printed_score(value):
    """印刷题面上标注的分值。取不到或不合理就是 None。

    它**不参与**判分，只作为「试卷原始分值」展示，供教师换算。
    上限 300 是为了挡住模型把题号、年份当分值填进来。

    容忍模型返回「12分」这类带字的写法：只抽第一个数字，抽不到才算没有。
    刻意只抽**第一个**数字——「第12题，本题8分」这种含两个数字的串，
    取第一个会得到题号 12 而不是分值 8，但两者都在合法区间内，无从分辨；
    与其猜，不如让它落在「可能不准但只用于展示」的既有口径里，
    真正的判分分母始终是体系判别分的 15，不受这个值影响。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        m = _SCORE_NUM_RE.search(str(value))
        if not m:
            return None
        try:
            n = float(m.group(1))
        except ValueError:
            return None
    # float("nan") / float("inf") 通过 try-except 不会抛，但它们通过 JSON 序列化
    # 会产生 NaN / Infinity——不是合法的 JSON 值，浏览器的 JSON.parse 会 SyntaxError，
    # 整个 recognize-image 响应就不可解析了。
    if math.isnan(n) or math.isinf(n):
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

# 三阶段整体失败后重试一次的间隔。
#
# 为什么要在**整页这一层**再重试：单次 HTTP 调用层已经有 3 次重试
# （_post_with_retry），能走到降级这一步说明那层也没救回来。但三阶段是
# 2+N 次调用串起来的，**任何一次**耗尽重试都会让整页掉到单次路径，
# 出错面积随题数放大。单份测试时三科都稳走三阶段，批量连打几十页就频繁降级
# ——而降级出来的转写质量差得很具体：实测英语完形填空，单次路径会把
# 相邻 4 道题的题干读成同一段文字，于是贪心一对一只能配上一道，
# 其余 3 道全部报「题库中无此题」。教师看到的是题库没建好，其实是识别降了级。
#
# 只对网关侧的瞬时故障重试。版面解析没读到题（ValueError）这类不重试：
# 重跑一次结果一样，纯属白烧额度和时间。
_STAGED_RETRY_SLEEP = 2.0


def _recognize_vlm_best(image_bytes: bytes, mime: str, strong: bool = False):
    """先试三阶段识别，失败降级单次调用。返回 (结果 or None, 错误说明)。

    降级而不是直接失败：三阶段多了两次调用与两次 JSON 解析，出错面积比单次大
    （模型漏字段、归属数组为空、网关抖动）。而单次调用是已经跑通过的链路，
    它能给出的结果虽然更容易漏掉越界作答，也远好于让体验者看到「识别失败」。
    降级时把原因带在 staged_error 里，排障时才知道分阶段那条路挂在哪。

    **降级必须留下痕迹**：它以前只往结果里塞一个没人读的 staged_error，
    日志不打、界面不显示。于是「这一页为什么匹配得这么差」在任何一处都查不到
    ——教师以为题库有问题，实际是识别悄悄换了条更差的路。

    strong=True 时**只把定向复识换成强模型**，版面与归属仍走常规档。
    刻意不让强模型读整页：实测整页 + 长 prompt 在中转网关上要 75 秒以上、
    时快时慢，超时就直接 504，重试三次仍是同样结果（一次实测 226 秒后失败
    回落，回落出来的转写正是教师报错的那个）。而强模型真正的价值是把某个
    具体答案读准——那是小图 + 短 prompt 的活，稳定又快。
    """
    use_strong = strong and strong_available()
    staged_error = ""
    if staged_enabled():
        for attempt in (1, 2):
            try:
                result = recognize_vlm_staged(image_bytes, mime,
                                              strong_refine=use_strong)
                if use_strong:
                    result["strong"] = True
                    result["model_tier"] = strong_model_name()
                return result, ""
            except requests.RequestException as exc:
                staged_error = "%s: %s" % (type(exc).__name__, exc)
                if attempt == 1:
                    print("[智批π] 三阶段识别遇到网关故障，2 秒后重试一次：%s"
                          % staged_error[:200], flush=True)
                    time.sleep(_STAGED_RETRY_SLEEP)
                    continue
            except Exception as exc:
                # 不是网关问题（版面没读到题、JSON 缺字段…），重试无意义
                staged_error = "%s: %s" % (type(exc).__name__, exc)
            break
        if staged_error:
            print("[智批π] 三阶段识别失败，降级为单次识别（转写质量会下降，"
                  "题库匹配可能变差）：%s" % staged_error[:300], flush=True)
    try:
        result = recognize_vlm(image_bytes, mime)
        result["staged"] = False
        if staged_error:
            result["staged_error"] = staged_error
        if use_strong:
            # 单次路径没有手写块清单，复识只能按题目自身 bbox 裁图；
            # 触发条件与三阶段共用，用得上的是「答案本身看着不对」那几条。
            _refine_after_single(image_bytes, mime, result, strong=True)
            result["strong"] = True
            result["model_tier"] = strong_model_name()
        return result, ""
    except Exception as exc:
        detail = "%s" % exc
        if staged_error:
            detail += "（分阶段识别亦失败：%s）" % staged_error
        return None, detail


def _refine_after_single(image_bytes: bytes, mime: str, result: dict,
                         strong: bool = False) -> None:
    """单次识别之后的定向复识（就地改写 result["questions"]）。"""
    if not refine_enabled():
        return
    questions = result.get("questions") or []
    records = _refine_answers(image_bytes, mime, questions, [], strong=strong)
    if records:
        result["refined"] = records
        result["text"] = "\n\n".join(
            q.get("answer") or "" for q in questions if q.get("answer")).strip()


def recognize_image(image_bytes: bytes, mime: str = "image/png",
                    allow_vlm: bool = True,
                    unavailable_note: str = "", strong: bool = False) -> dict:
    """作业照片统一识别入口。

    只有一条路：真实多模态识别。识别不了就明说识别不了——内置样例现在也是
    真实作业原件，与体验者自己上传的照片走同一条链路，没有「预置转写」可以
    在这里兜底，兜底反而会让人以为跑通了。

    参数：
        image_bytes:       图片原始字节。
        mime:              图片 MIME 类型。
        allow_vlm:         本次是否允许发起真实多模态调用。公开部署时由调用方
                           按日配额 / 限流结果传入 False。
        unavailable_note:  allow_vlm=False 时附加的说明（如配额已用尽）。
        strong:            改用强模型识别（教师在界面上点「强模型重识别」时传入）。
    """
    if not vlm_configured():
        return {
            "engine": "none",
            "reason": "vlm_not_configured",
            "error": ("识别需要联网调用多模态大模型：请配置 ZHIPI_VLM_API_KEY "
                      "后重试（内置样例是真实作业原件，同样需要联网识别）。"),
        }

    if not allow_vlm:
        return {
            "engine": "none",
            "reason": "vlm_unavailable",
            "error": unavailable_note or "真实识别暂不可用，请稍后再试。",
        }

    result, error = _recognize_vlm_best(image_bytes, mime,
                                        strong=strong or strong_default())
    if result is not None:
        return result
    return {
        "engine": "none",
        "reason": "vlm_failed",
        "error": "多模态识别调用失败：%s" % error,
    }



# ---------- 兼容旧接口（文本流水线仍在使用） ----------

def mock_ocr(submission: dict) -> dict:
    """从内置作答数据取预置转写文本与清晰度分（文本批改流水线使用）。"""
    ocr = submission.get("ocr", {})
    return {
        "text": ocr.get("text", ""),
        "clarity": float(ocr.get("clarity", 0)),
    }
