# -*- coding: utf-8 -*-
"""手写作业样例图片生成器。

把 data/submissions.json 中全部学生作答渲染成「拍照上传的手写作业」仿真图片，
供 Demo 的图片批改链路（上传 → 识别 → 批改）离线演示使用：

- 页面版式：作业纸抬头（班级 / 学科 / 姓名）→ 印刷体题目 → 手写体作答；
- 手写模拟：楷体（中文/数学）与 Segoe Print（英文）逐字符渲染，
  随机抖动基线、字距、大小与倾角，蓝色钢笔墨色深浅变化；
- 卷面质量：按 OCR 清晰度分档 —— 清晰度低的样例叠加模糊、灰墨、
  涂改划线，模拟真实低质量卷面；
- 拍照效果：整页轻微旋转放在深色桌面上，叠加噪点与暗角。

全程确定性（以 submission_id 为随机种子），重复运行输出一致。

运行：
    python tools/gen_sample_images.py
输出：
    data/sample_images/SUB001.png ... SUB011.png 及 manifest.json
"""
import hashlib
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE_DIR = Path(__file__).resolve().parent.parent   # demo/
DATA_DIR = BASE_DIR / "data"
OUT_DIR = DATA_DIR / "sample_images"

FONT_DIR = Path("C:/Windows/Fonts")
FONT_KAI = FONT_DIR / "simkai.ttf"       # 楷体：中文 / 数学手写
FONT_EN_HAND = FONT_DIR / "segoepr.ttf"  # Segoe Print：英文手写
FONT_PRINT = FONT_DIR / "simsun.ttc"     # 宋体：印刷体题目
FONT_HEI = FONT_DIR / "simhei.ttf"       # 黑体：作业纸抬头

PAGE_W, PAGE_H = 880, 1080
MARGIN_X = 70
LINE_GAP = 52          # 作答区横线间距

PAPER = (253, 251, 244)
DESK = (128, 116, 102)
RULE = (196, 208, 224)
INK_BLUE = (36, 52, 130)
INK_GRAY = (72, 80, 96)
PRINT_BLACK = (40, 40, 44)

# 上标 / 下标字符 → (对应普通字符, 缩放, 垂直偏移比例)
SUPER_SUB = {
    "²": ("2", 0.66, -0.28),
    "³": ("3", 0.66, -0.28),
    "₁": ("1", 0.62, 0.28),
    "₂": ("2", 0.62, 0.28),
    "₃": ("3", 0.62, 0.28),
}


def _load(name):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _font(path, size):
    return ImageFont.truetype(str(path), size)


class HandWriter:
    """逐字符手写渲染：抖动基线 / 大小 / 倾角，墨色深浅变化。"""

    def __init__(self, draw, rng, font_path, size, ink, messy=0.0):
        self.draw = draw
        self.rng = rng
        self.font_path = font_path
        self.size = size
        self.ink = ink
        self.messy = messy  # 0 工整 ~ 1 潦草
        self._font_cache = {}

    def _f(self, size):
        size = max(10, int(size))
        if size not in self._font_cache:
            self._font_cache[size] = _font(self.font_path, size)
        return self._font_cache[size]

    def _ink(self):
        """墨色深浅随笔画随机波动；潦草卷面墨色更浅更散。"""
        r, g, b = self.ink
        jitter = self.rng.randint(-14, 26) + int(self.messy * 30)
        return (min(255, r + jitter), min(255, g + jitter), min(255, b + jitter))

    def char_width(self, ch):
        if ch in SUPER_SUB:
            base, scale, _ = SUPER_SUB[ch]
            return self.draw.textlength(base, font=self._f(self.size * scale))
        return self.draw.textlength(ch, font=self._f(self.size))

    def text_width(self, text):
        pad = 1 + self.messy * 1.5
        return sum(self.char_width(c) + pad for c in text)

    def write_line(self, x, y, text):
        """在 (x, y) 基线位置手写一行，返回结束 x 坐标。"""
        cx = float(x)
        for ch in text:
            if ch == " ":
                cx += self.size * (0.32 + self.rng.uniform(0, 0.12))
                continue
            size = self.size
            dy_extra = 0.0
            if ch in SUPER_SUB:
                ch, scale, dy_ratio = SUPER_SUB[ch]
                size = self.size * scale
                dy_extra = self.size * dy_ratio
            jitter = 1.0 + self.messy * 2.2
            dx = self.rng.uniform(-1.2, 1.2) * jitter
            dy = self.rng.uniform(-1.8, 1.8) * jitter + dy_extra
            fsize = size * self.rng.uniform(1 - 0.04 * jitter, 1 + 0.04 * jitter)
            font = self._f(fsize)
            w = self.draw.textlength(ch, font=font)
            if self.messy > 0.4 and self.rng.random() < 0.3:
                # 潦草卷面：个别字符轻微倾斜（单字渲染到小图再旋转）
                self._tilted_char(cx + dx, y + dy, ch, font, w)
            else:
                self.draw.text((cx + dx, y + dy), ch, font=font, fill=self._ink())
            cx += w + 1 + self.messy * 1.5
        return cx

    def _tilted_char(self, x, y, ch, font, w):
        pad = 8
        h = int(font.size * 1.6)
        tile = Image.new("RGBA", (int(w) + pad * 2, h + pad * 2), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.text((pad, pad), ch, font=font, fill=self._ink() + (255,))
        angle = self.rng.uniform(-6, 6)
        tile = tile.rotate(angle, resample=Image.BICUBIC, expand=False)
        self.draw._image.paste(tile, (int(x) - pad, int(y) - pad), tile)


def wrap_hand(writer, text, max_w):
    """按手写宽度对文本换行；英文按词、中文按字符断行。"""
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        # 英文段落按单词断行
        if all(ord(c) < 128 for c in para):
            words, cur = para.split(" "), ""
            for w in words:
                cand = (cur + " " + w).strip()
                if writer.text_width(cand) <= max_w or not cur:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = w
            if cur:
                lines.append(cur)
        else:
            cur = ""
            for ch in para:
                if writer.text_width(cur + ch) <= max_w or not cur:
                    cur += ch
                else:
                    lines.append(cur)
                    cur = ch
            if cur:
                lines.append(cur)
    return lines


def wrap_print(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w or not cur:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def render_page(question, submission):
    """渲染一页手写作业，返回 PIL Image。"""
    rng = random.Random(submission["submission_id"])
    clarity = submission["ocr"]["clarity"]
    messy = max(0.0, min(1.0, (92 - clarity) / 45))  # 清晰度越低越潦草

    img = Image.new("RGB", (PAGE_W, PAGE_H), PAPER)
    draw = ImageDraw.Draw(img)

    subject = question["subject"]
    is_english = subject == "英语"
    hand_font = FONT_EN_HAND if is_english else FONT_KAI
    hand_size = 30 if is_english else 34
    ink = INK_GRAY if messy > 0.55 else INK_BLUE

    # ---- 作业纸抬头 ----
    hei20 = _font(FONT_HEI, 21)
    title = "初二(3)班 · %s作业" % subject
    draw.text((MARGIN_X, 42), title, font=hei20, fill=PRINT_BLACK)
    draw.text((PAGE_W - 330, 42), "姓名：", font=_font(FONT_PRINT, 20), fill=PRINT_BLACK)
    name_writer = HandWriter(draw, rng, FONT_KAI, 26, ink, messy * 0.6)
    name_writer.write_line(PAGE_W - 268, 36, submission["student_name"])
    draw.line([(MARGIN_X - 10, 84), (PAGE_W - MARGIN_X + 10, 84)], fill=(90, 90, 96), width=2)
    draw.line([(MARGIN_X - 10, 88), (PAGE_W - MARGIN_X + 10, 88)], fill=(150, 150, 156), width=1)

    # ---- 印刷体题目 ----
    print_font = _font(FONT_PRINT, 24)
    q_text = "题目：" + question["question_text"]
    if question.get("max_score"):
        q_text += "（%d分）" % question["max_score"]
    q_lines = wrap_print(draw, q_text, print_font, PAGE_W - MARGIN_X * 2)
    y = 116
    for ln in q_lines:
        draw.text((MARGIN_X, y), ln, font=print_font, fill=PRINT_BLACK)
        y += 36

    # ---- 作答区横线 ----
    ans_top = y + 30
    for ly in range(ans_top + LINE_GAP, PAGE_H - 60, LINE_GAP):
        draw.line([(MARGIN_X - 10, ly), (PAGE_W - MARGIN_X + 10, ly)], fill=RULE, width=1)

    # ---- 手写作答 ----
    writer = HandWriter(draw, rng, hand_font, hand_size, ink, messy)
    max_w = PAGE_W - MARGIN_X * 2 - 20
    text = submission["ocr"]["text"]
    lines = []
    if is_english:
        lines.append("__TITLE__My Weekend")
    else:
        lines.append("解：")
    lines += wrap_hand(writer, text, max_w)

    baseline = ans_top + 4
    for ln in lines:
        if baseline > PAGE_H - 90:
            break
        if ln.startswith("__TITLE__"):
            t = ln[len("__TITLE__"):]
            tw = writer.text_width(t)
            writer.write_line((PAGE_W - tw) / 2, baseline, t)
        elif ln:
            writer.write_line(MARGIN_X + rng.uniform(0, 14), baseline, ln)
        baseline += LINE_GAP

    # ---- 低清晰度卷面：涂改划线 ----
    if messy > 0.5:
        for _ in range(rng.randint(1, 2)):
            sx = rng.randint(MARGIN_X + 40, PAGE_W - MARGIN_X - 180)
            sy = rng.randint(ans_top + LINE_GAP, min(int(baseline), PAGE_H - 120))
            pts = []
            for i in range(14):
                pts.append((sx + i * 9 + rng.uniform(-3, 3),
                            sy + rng.uniform(-5, 5)))
            draw.line(pts, fill=writer._ink(), width=2)

    # ---- 拍照效果：模糊 / 噪点 / 桌面背景 / 暗角 ----
    if messy > 0.45:
        img = img.filter(ImageFilter.GaussianBlur(0.7 + messy * 0.5))
    else:
        img = img.filter(ImageFilter.GaussianBlur(0.3))

    angle = rng.uniform(-1.4, 1.4)
    rotated = img.convert("RGBA").rotate(angle, resample=Image.BICUBIC,
                                         expand=True, fillcolor=(0, 0, 0, 0))
    canvas = Image.new("RGB", rotated.size, DESK)
    # 纸张阴影
    shadow = Image.new("RGBA", rotated.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle([10, 12, rotated.size[0] - 2, rotated.size[1] - 2], fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    canvas.paste(Image.new("RGB", rotated.size, DESK), (0, 0))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow)
    canvas = Image.alpha_composite(canvas, rotated).convert("RGB")

    # 噪点 + 暗角
    px = canvas.load()
    w, h = canvas.size
    nrng = random.Random(submission["submission_id"] + "_noise")
    for _ in range(int(w * h * 0.004)):
        x = nrng.randint(0, w - 1)
        y2 = nrng.randint(0, h - 1)
        r, g, b = px[x, y2]
        d = nrng.randint(-18, 18)
        px[x, y2] = (max(0, min(255, r + d)), max(0, min(255, g + d)), max(0, min(255, b + d)))
    vignette = Image.new("L", (w, h), 0)
    vd = ImageDraw.Draw(vignette)
    vd.ellipse([-w * 0.25, -h * 0.25, w * 1.25, h * 1.25], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(120))
    dark = Image.new("RGB", (w, h), (16, 14, 12))
    canvas = Image.composite(canvas, dark, vignette)
    return canvas


def main():
    questions = {q["question_id"]: q for q in _load("questions.json")["questions"]}
    submissions = _load("submissions.json")["submissions"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = {}
    for sub in submissions:
        sid = sub["submission_id"]
        img = render_page(questions[sub["question_id"]], sub)
        out = OUT_DIR / ("%s.png" % sid)
        img.save(out, optimize=True)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        manifest[sid] = {
            "file": out.name,
            "sha256": digest,
            "student_name": sub["student_name"],
            "question_id": sub["question_id"],
            "subject": questions[sub["question_id"]]["subject"],
            "clarity": sub["ocr"]["clarity"],
        }
        print("生成 %s -> %s (%d KB)" % (sid, out.name, out.stat().st_size // 1024))

    with open(OUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("完成：%d 张样例图片 + manifest.json" % len(manifest))


if __name__ == "__main__":
    main()
