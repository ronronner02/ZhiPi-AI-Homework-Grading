# -*- coding: utf-8 -*-
"""在学生作业原图上画批改痕迹。

## 为什么必须画在原图上

批改结果做成一张漂亮的报告页，学生看到的是「一份关于我作业的报告」；
把勾叉画回他自己那张纸上，学生看到的是「老师批过的我的作业」。
后者才是这条产品线要交付的东西——教师批改的最终产物一直都是那张纸。

## 痕迹跟着**作答**走，不跟着题目走

教师批卷时，红勾是打在学生写的那个答案旁边的——看一眼就知道是哪一处错了。
所以落点按这个顺序取：

1. `answer_box`：归属阶段判给这道题的手写块外接框，就是「学生的作答」
   在纸上的实际位置（见 ocr._apply_answer_boxes）；
2. `bbox`：题目框。只有版面阶段给了题、却没能定位作答时才用；
3. 都没有 → 页面右缘按题序均分，并在记号旁标出题号，不假装知道位置。

早期版本一律画成「白底圆角框 + 勾 + 得分」，并且刻意躲到页边空白处。那样
每处痕迹都是一块贴纸，与作答之间没有视觉关联，一页十几道题看下来分不清
哪个勾对应哪个答案。现在只画记号本身：勾 / 半勾 / 叉 / 圈，紧贴作答右侧，
不盖住学生的字迹。

坐标**不精确**（实测会整体偏移、会把相邻两题框在一起），产品上仍然照标不误：
位置略偏不影响判读，而「因为没把握所以不画」等于这个功能不存在。

## 为什么在服务端画

痕迹要和结果一起进教师工作台、要能导出、要能推给家长。画在前端 canvas 上
只有当前这个浏览器有；画在服务端得到的是一张真实存在的图片，谁都能拿。
"""
import io

# 批改痕迹的配色。红是批改的约定俗成色，四种判定各自一色，
# 不靠形状单独承担区分——打印成灰度时形状仍然分得开。
COLOR_RIGHT = (220, 38, 38)      # 全对：红勾（教师红笔的习惯，不是绿的）
COLOR_PARTIAL = (217, 119, 6)    # 部分对：橙
COLOR_WRONG = (220, 38, 38)      # 错：红
COLOR_BLANK = (100, 116, 139)    # 未作答：灰
COLOR_HEAD_BG = (17, 24, 39)     # 页眉底色
COLOR_HEAD_FG = (255, 255, 255)

_MARK_COLOR = {
    "correct": COLOR_RIGHT,
    "partial": COLOR_PARTIAL,
    "wrong": COLOR_WRONG,
    "blank": COLOR_BLANK,
}

# 对错记号用**线段画**，不用字符渲染。
# 实测：Windows 上能显示中文的字体（msyh / simhei / simsun）全都不含
# U+2713 ✓ 与 U+2717 ✗，画出来是一个空心方框（豆腐块）——痕迹图上每道题
# 旁边顶着一个方框，比不画还糟。含这两个符号的 seguisym 又不保证在
# Linux 容器里存在。而勾和叉本来就是两三笔的形状，直接画线既没有字体
# 依赖，形态也更接近教师手写的记号。

# 输出图片的长边上限。原图常有 5000px 宽（手机直出 / 高 DPI 扫描），
# 按原尺寸画完再传给浏览器是几 MB 的无谓流量，而批改痕迹在 1600px 上
# 已经完全看得清。
MAX_EDGE = 1600
JPEG_QUALITY = 88


def _load_font(size: int):
    """找一个能显示中文的字体。找不到就退回 PIL 内置位图字体。

    内置字体画不出中文，那时痕迹会退化成一串方块——所以按常见系统字体
    逐个试。Windows 演示机上 msyh/simhei 必有其一，Linux 容器里装了
    fonts-noto-cjk 也能命中。
    """
    from PIL import ImageFont
    candidates = (
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def _text_size(draw, text: str, font) -> tuple:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _draw_glyph(draw, verdict: str, x: float, y: float, size: float, color) -> None:
    """画一个对错记号：勾 / 半勾 / 叉 / 空心圈。(x, y) 是记号的左上角。

    全部用线段与椭圆画，不依赖任何字体——见 _MARK_COLOR 上方的注释。
    笔画比早期版本粗（0.19 而不是 0.16）：记号底下不再垫白底方框，直接压在
    印刷题干与手写笔迹上，细笔画会被底下的字吃掉，一眼扫过去看不见。
    """
    w = max(3, round(size * 0.19))          # 笔画粗细随记号大小走
    s = size
    if verdict == "correct":
        # 勾：先下后上，右上那笔更长，形态接近手写的 ✓
        draw.line([(x + s * 0.10, y + s * 0.52), (x + s * 0.38, y + s * 0.82)],
                  fill=color, width=w)
        draw.line([(x + s * 0.38, y + s * 0.82), (x + s * 0.90, y + s * 0.16)],
                  fill=color, width=w)
    elif verdict == "partial":
        # 半勾：勾的形状 + 一道斜杠，表示「部分给分」。教师批卷时的常见写法。
        draw.line([(x + s * 0.10, y + s * 0.52), (x + s * 0.36, y + s * 0.80)],
                  fill=color, width=w)
        draw.line([(x + s * 0.36, y + s * 0.80), (x + s * 0.78, y + s * 0.26)],
                  fill=color, width=w)
        draw.line([(x + s * 0.62, y + s * 0.86), (x + s * 0.94, y + s * 0.60)],
                  fill=color, width=max(2, w - 1))
    elif verdict == "blank":
        # 空心圈：未作答。和「错」区分开——没写和写错不是一回事。
        draw.ellipse([x + s * 0.14, y + s * 0.18, x + s * 0.86, y + s * 0.86],
                     outline=color, width=w)
    else:
        draw.line([(x + s * 0.16, y + s * 0.20), (x + s * 0.84, y + s * 0.84)],
                  fill=color, width=w)
        draw.line([(x + s * 0.84, y + s * 0.20), (x + s * 0.16, y + s * 0.84)],
                  fill=color, width=w)


def render(page_bytes: bytes, questions: list, header: dict = None) -> bytes:
    """把逐题批改结果画到页图上，返回 JPEG 字节。

    questions: pagegrader 输出的 questions 列表（需含 index/no/score/
        max_score/verdict，answer_box / bbox 可选）。只画属于这一页的题，
        由调用方筛好。
    header: {"student":…, "total":…, "max":…, "status":…}，画在页顶的总分条。
    """
    from PIL import Image, ImageDraw, ImageOps

    with Image.open(io.BytesIO(page_bytes)) as src:
        # 再按 EXIF 转正一次。正常链路里上传入口已经拉平过（见
        # ocr.normalize_orientation），这里是**防御**：本函数也被评测脚本、
        # 批量工具直接拿原始照片调用，而输出的 JPEG 不带 EXIF——一旦入口那层
        # 被绕过，得到的就是一张相对原图整体旋转 90° 的批改件，痕迹全错位。
        # 已经正立的图上这个调用不产生任何拷贝，代价可以忽略。
        img = ImageOps.exif_transpose(src).convert("RGB")
        scale = min(1.0, MAX_EDGE / max(img.width, img.height))
        if scale < 1.0:
            img = img.resize((round(img.width * scale), round(img.height * scale)),
                             Image.LANCZOS)

        width, height = img.size
        # 记号的基准大小随页面尺寸走。比早期的「牌子」大一号：现在它是纸面上
        # 唯一的批改痕迹，要在一屏缩略图里也认得出是勾还是叉。
        base = max(18, round(min(width, height) * 0.034))
        font_small = _load_font(max(11, round(base * 0.46)))

        # 痕迹画在一张透明层上再合成。不透明地直接盖会把学生那行答案抹掉——
        # 批改件本该让人**同时**看到「学生写了什么」和「老师判了什么」。
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        bar_h = _draw_header(draw, width, header, base) if header else 0
        # 定位不到作答的题沿页面右缘按题序均分排布，彼此不叠。
        fallback = [q for q in questions if not _anchor(q)]
        slot_h = (height - bar_h) / max(1, len(fallback))

        placed = []      # 已落下的记号，后面的要躲开，免得两处痕迹叠在一起
        fb_i = 0
        for q in questions:
            verdict = q.get("verdict") or "wrong"
            color = _MARK_COLOR.get(verdict, COLOR_WRONG)
            anchor = _anchor(q)
            label = _label_of(q, located=bool(anchor))
            lw, lh = _text_size(draw, label, font_small) if label else (0, 0)

            if anchor:
                size = _glyph_size(anchor, height, base)
                x0, y0 = _place_glyph(anchor, size, lw, width, height,
                                      bar_h, placed)
            else:
                size = base
                x0 = width - size - lw - 6
                y0 = min(max(bar_h + 2, bar_h + slot_h * (fb_i + 0.5) - size / 2),
                         height - size - 2)
                fb_i += 1

            _draw_glyph(draw, verdict, x0, y0, size, color + (255,))
            if label:
                draw.text((x0 + size + max(2, size * 0.08),
                           y0 + (size - lh) / 2), label,
                          font=font_small, fill=color + (255,),
                          stroke_width=2, stroke_fill=(255, 255, 255, 200))
            placed.append((x0, y0, x0 + size + lw + 4, y0 + size))

        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY)
        return out.getvalue()


def _anchor(q: dict):
    """这道题的记号该贴着谁：优先学生作答框，其次题目框，都没有则 None。"""
    return q.get("answer_box") or q.get("bbox") or None


def _label_of(q: dict, located: bool) -> str:
    """记号旁边要不要写字。默认不写——一个勾就够了，写满数字反而看不清。

    两种情况必须写：
    - 部分给分：光看半勾不知道给了几分，而「给了几分」正是这类题的争议点；
    - 位置是猜的：不写题号，教师无从判断这个记号对应哪一题。
    """
    if not located:
        return "第%s题 %g/%g" % (q.get("no") or q.get("index"),
                                q.get("score", 0), q.get("max_score", 0))
    if (q.get("verdict") or "") == "partial":
        return "%g/%g" % (q.get("score", 0), q.get("max_score", 0))
    return ""


def _glyph_size(anchor, height: int, base: int) -> float:
    """记号大小跟着这道题作答区的高度走，再夹在 [base*0.5, base*1.9] 里。

    跟着走是因为选择题的一个字母与解答题的半页演算，旁边该配的记号不是一个
    尺寸。下限压到 base 的一半，是被密排的练习册逼出来的：单词填空一行只有
    二十来像素高，而记号若恒等于 base（约 40px），上下两行的记号就会互相
    挤开，被迫躲到手写字上去——躲开重叠的代价变成了盖住答案。
    上限则挡住模型偶尔给出的整页高的框，免得画出一个占满页面的巨勾。
    """
    box_h = max(0.0, (anchor[3] - anchor[1])) * height
    if box_h <= 0:
        return base
    return max(base * 0.5, min(base * 1.9, box_h * 1.15))


def _overlaps(box, others, tol: float = 2.0) -> bool:
    x0, y0, x1, y1 = box
    for a0, b0, a1, b1 in others:
        if x0 < a1 - tol and x1 > a0 + tol and y0 < b1 - tol and y1 > b0 + tol:
            return True
    return False


def _place_glyph(anchor, size: float, lw: float, width: int, height: int,
                 bar_h: float, placed: list) -> tuple:
    """给记号挑落点：紧贴作答的**右侧**，与教师批卷的落笔位置一致。

    候选按「教师会写在哪儿」排：右侧 → 右侧上下微调 → 左侧。全被占才退回作答框
    右端里面——盖住一点字，仍然强于画到别的题上去。上下微调这一档是必要的：
    密排的填空题上下行只差十几像素，只认「正右方」会让第二行的记号一路退到
    左边或压在字上，而实际上往下挪半个记号就有位置。
    """
    need = size + lw + 4
    gap = max(3, round(size * 0.16))
    x_left, x_right = anchor[0] * width, anchor[2] * width
    y_top, y_bot = anchor[1] * height, anchor[3] * height
    y_mid = (y_top + y_bot) / 2 - size / 2

    def clamp_y(y):
        return min(max(bar_h + 2, y), height - size - 2)

    candidates = [
        (x_right + gap, y_mid),                     # ① 作答右侧
        (x_right + gap, y_mid + size * 0.55),       # ② 右侧略下
        (x_right + gap, y_mid - size * 0.55),       # ③ 右侧略上
        (x_left - gap - need, y_mid),               # ④ 作答左侧
    ]
    for x0, y0 in candidates:
        y0 = clamp_y(y0)
        if x0 < 2 or x0 + need > width - 2:
            continue
        if not _overlaps((x0, y0, x0 + need, y0 + size), placed):
            return x0, y0

    # ⑤ 退回作答框右端内侧
    x0 = min(max(2, x_right - need), width - need - 2)
    return x0, clamp_y(y_mid)


def _draw_header(draw, width: int, header: dict, base: int) -> int:
    """页顶总分条，返回它的高度。画在透明层上，所以颜色都要带 alpha。"""
    font = _load_font(max(14, round(base * 0.78)))
    pad = round(base * 0.45)
    bar_h = round(base * 1.9)
    draw.rectangle([0, 0, width, bar_h], fill=COLOR_HEAD_BG + (255,))
    left = "%s　%g / %g 分" % (header.get("student") or "学生作业",
                              header.get("total", 0), header.get("max", 0))
    draw.text((pad, (bar_h - base * 0.9) / 2), left, font=font, fill=COLOR_HEAD_FG + (255,))
    right = {"green": "AI 自动批改 · 高置信",
             "yellow": "AI 批改 · 待教师确认",
             "red": "AI 批改 · 需人工复核"}.get(header.get("status"), "AI 批改")
    w, _h = _text_size(draw, right, font)
    draw.text((width - w - pad, (bar_h - base * 0.9) / 2), right,
              font=font, fill=COLOR_HEAD_FG + (255,))
    return bar_h
