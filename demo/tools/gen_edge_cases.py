# -*- coding: utf-8 -*-
"""生成边界条件 / 极端情况数据集，用于压测整条批改链路。

产出两套目录，用途严格区分：

    <out>/tested/     我用来跑测试、定位并修复缺陷的那批
    <out>/holdout/    刻意**不看不测**的一批，留给人工独立走全流程

为什么要分开：我自己测过的数据必然带有「我知道它会怎么失败」的偏见——
修完之后它一定通过，那不构成独立证据。holdout 由生成器用不同随机种子产出，
我不查看其内容、不对它跑任何断言，人工测出来的问题才是真问题。

覆盖的攻击面（每一项都对应线上真实会遇到的输入）：

  图片层
    - 极小 / 极大尺寸、极端长宽比（1×1、1×10000）
    - 解压炸弹（声明尺寸巨大但压缩后很小）
    - 截断文件、字节全零、伪装扩展名（.png 里装 JPEG / 装文本）
    - 带 alpha 通道、灰度、CMYK、调色板模式、EXIF 旋转
    - 动图（GIF/多帧 WebP）、单色纯白 / 纯黑（无任何笔迹）
    - 超大文件体积（触发 body / image 上限）

  文本与标注层
    - 空串、纯空白、仅换行、超长文本
    - 控制字符 / NUL / BOM / 零宽字符
    - RTL（阿拉伯语）、Emoji、CJK 兼容表意文字
    - LaTeX 与 Unicode 上下标混写（CER 记号归一的对照组）
    - 分数越界（负分、超满分）、needs_review 缺失 / 非布尔
    - question_id 不存在、image 路径不存在

用法：
    py -3 tools/gen_edge_cases.py <输出目录> [--seed 20260806]
"""
import argparse
import io
import json
import struct
import sys
import zlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from PIL import Image, ImageDraw
except ImportError:                                    # pragma: no cover
    print("需要 Pillow：py -3 -m pip install pillow")
    raise SystemExit(2)


# ---------------------------------------------------------------- 图片构造

def _paper(w, h, color=(253, 251, 244)):
    return Image.new("RGB", (w, h), color)


def img_tiny(path):
    """1×1 像素。考验「小到不可能有内容」时是否被当成正常图片送去烧额度。"""
    _paper(1, 1).save(path, "PNG")
    return "1×1 极小图"


def img_thin(path):
    """1×4000 极端长宽比。某些预处理会按长边缩放，短边会被算成 0 而崩。"""
    _paper(1, 4000).save(path, "PNG")
    return "1×4000 极端长宽比"


def img_wide(path):
    _paper(4000, 1).save(path, "PNG")
    return "4000×1 极端长宽比"


def img_over_pixels(path):
    """像素数超过 MAX_IMAGE_PIXELS（4000 万）。用纯色以便文件本身不大。"""
    _paper(8000, 6000).save(path, "PNG", compress_level=9)
    return "8000×6000 = 4800 万像素，超像素上限"


def img_decompression_bomb(path):
    """解压炸弹：PNG 头声明 30000×30000，实际数据是一行纯色重复。

    手工拼 PNG 而不用 Pillow 生成——Pillow 会真的分配 9 亿像素内存。
    这正是攻击的形态：文件几十 KB，解码方却要吃掉几 GB。
    """
    w = h = 30000

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit RGB
    raw = b"".join(b"\x00" + b"\xff" * (w * 3) for _ in range(1))
    # 只放一行真实数据，其余靠解码器自己去读（截断），足以让声明尺寸生效
    idat = zlib.compress(raw * 1, 9)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
    path.write_bytes(png)
    return "PNG 头声明 30000×30000 的解压炸弹（文件仅 %d 字节）" % len(png)


def img_truncated(path):
    """正常 PNG 砍掉后 40%。模拟上传中断 / 网络截断。"""
    buf = io.BytesIO()
    im = _paper(600, 400)
    ImageDraw.Draw(im).text((20, 20), "x^2-5x+6=0", fill=(36, 52, 130))
    im.save(buf, "PNG")
    data = buf.getvalue()
    path.write_bytes(data[: int(len(data) * 0.6)])
    return "PNG 截断至 60%"


def img_zero_bytes(path):
    path.write_bytes(b"\x00" * 4096)
    return "4096 字节全零"


def img_empty(path):
    path.write_bytes(b"")
    return "0 字节空文件"


def img_text_as_png(path):
    """扩展名 .png，内容是纯文本。伪装文件的典型形态。"""
    path.write_bytes("这不是图片，只是一段文本。".encode("utf-8"))
    return ".png 扩展名但内容是 UTF-8 文本"


def img_jpeg_as_png(path):
    """真实 JPEG 存成 .png。校验必须看魔数而不是扩展名。"""
    buf = io.BytesIO()
    _paper(500, 300).save(buf, "JPEG", quality=85)
    path.write_bytes(buf.getvalue())
    return "真实 JPEG 内容 + .png 扩展名"


def img_alpha(path):
    """RGBA 半透明。转 JPEG 时透明区会变黑，遮住笔迹。"""
    im = Image.new("RGBA", (600, 400), (253, 251, 244, 0))
    d = ImageDraw.Draw(im)
    d.text((30, 30), "v=s/t=200/20=10m/s", fill=(36, 52, 130, 255))
    im.save(path, "PNG")
    return "RGBA 全透明背景 + 不透明笔迹"


def img_grayscale(path):
    im = _paper(600, 400).convert("L")
    ImageDraw.Draw(im).text((30, 30), "x1=2, x2=3", fill=0)
    im.save(path, "PNG")
    return "8bit 灰度图（mode=L）"


def img_palette(path):
    im = _paper(600, 400).convert("P", palette=Image.ADAPTIVE, colors=16)
    im.save(path, "PNG")
    return "调色板模式（mode=P，16 色）"


def img_cmyk(path):
    """CMYK JPEG。扫描仪 / 印刷流程会产出，Pillow 能开但通道顺序不同。"""
    im = _paper(600, 400).convert("CMYK")
    im.save(path, "JPEG")
    return "CMYK 色彩空间 JPEG"


def img_exif_rotated(path):
    """竖排内容 + EXIF Orientation=6（需顺时针旋转 90° 才正）。

    不处理 EXIF 的话，模型看到的是躺倒的字，识别必然失败。
    """
    im = _paper(400, 600)
    d = ImageDraw.Draw(im)
    d.text((30, 40), "x^2-5x+6=0", fill=(36, 52, 130))
    d.text((30, 90), "(x-2)(x-3)=0", fill=(36, 52, 130))
    exif = im.getexif()
    exif[0x0112] = 6                      # Orientation = Rotate 90 CW
    im.save(path, "JPEG", exif=exif)
    return "EXIF Orientation=6（需旋转 90°）"


def img_animated_gif(path):
    frames = []
    for i in range(3):
        f = _paper(300, 200, (250 - i * 5, 248, 240))
        ImageDraw.Draw(f).text((20, 20), "frame %d" % i, fill=(40, 40, 44))
        frames.append(f)
    frames[0].save(path, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    return "3 帧动画 GIF（格式白名单外）"


def img_blank_white(path):
    _paper(800, 600, (255, 255, 255)).save(path, "PNG")
    return "纯白空白页（无任何笔迹）"


def img_blank_black(path):
    _paper(800, 600, (0, 0, 0)).save(path, "PNG")
    return "纯黑页（拍照全黑 / 镜头遮挡）"


def img_huge_filesize(path):
    """尺寸合法但文件体积巨大（随机噪声不可压缩），触发体积上限。"""
    import random
    rnd = random.Random(1234)
    w = h = 2200
    im = Image.new("RGB", (w, h))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(w * h)])
    im.save(path, "PNG", compress_level=0)
    return "2200×2200 不可压缩噪声，文件 %.1f MB" % (path.stat().st_size / 1024 / 1024)


IMAGE_CASES = [
    ("tiny_1x1", img_tiny),
    ("thin_1x4000", img_thin),
    ("wide_4000x1", img_wide),
    ("over_pixels", img_over_pixels),
    ("decompression_bomb", img_decompression_bomb),
    ("truncated", img_truncated),
    ("zero_bytes", img_zero_bytes),
    ("empty_file", img_empty),
    ("text_as_png", img_text_as_png),
    ("jpeg_as_png", img_jpeg_as_png),
    ("alpha_channel", img_alpha),
    ("grayscale", img_grayscale),
    ("palette_mode", img_palette),
    ("cmyk_jpeg", img_cmyk),
    ("exif_rotated", img_exif_rotated),
    ("animated_gif", img_animated_gif),
    ("blank_white", img_blank_white),
    ("blank_black", img_blank_black),
    ("huge_filesize", img_huge_filesize),
]


# ---------------------------------------------------------------- 文本 / 标注构造

# 每项：(用例名, ocr_text_human, 说明)
TEXT_CASES = [
    ("empty", "", "空串——参照长度 0，CER 应为 None 而不是崩或 0%"),
    ("whitespace_only", "   \t  \n  ", "纯空白"),
    ("newlines_only", "\n\n\n", "仅换行"),
    ("single_char", "x", "单字符参照，1 处错就是 100% CER"),
    ("very_long", "x^2-5x+6=0\n" * 400, "超长文本（约 4400 字符）"),
    ("nul_byte", "x=2\x00x=3", "含 NUL 字节"),
    ("control_chars", "x=2\x01\x02\x1fx=3", "含 C0 控制字符"),
    ("bom_prefix", "﻿x^2-5x+6=0", "带 UTF-8 BOM"),
    ("zero_width", "x​=‌2‍", "零宽字符（视觉相同，字节不同）"),
    ("rtl_arabic", "الحل: x=2, x=3", "RTL 阿拉伯语混排"),
    ("emoji", "x=2 ✅ x=3 ❌", "含 Emoji（界面禁用，但输入可能带）"),
    ("cjk_compat", "㎡ ㎏ ℃ Ⅻ", "CJK 兼容表意与罗马数字"),
    ("latex_mixed", r"$x^{2}-5x+6=0$ \Rightarrow x_{1}=2", "LaTeX 写法"),
    ("unicode_sup_sub", "x²-5x+6=0\nx₁=2, x₂=3", "Unicode 上下标（记号归一对照组）"),
    ("ascii_sup_sub", "x^2-5x+6=0\nx1=2, x2=3", "ASCII 上下标（同上，应等价）"),
    ("mixed_scripts", "解：x=2，answer is 2 и 3", "中英俄混排"),
    ("only_punctuation", "。，；：！？", "仅标点"),
    ("html_injection", "<script>alert(1)</script>", "HTML/JS 注入尝试"),
    ("json_breaker", '{"text": "unterminated', "破坏 JSON 结构的片段"),
    ("very_long_line", "1" * 3000, "单行 3000 字符无换行"),
]

# 分数与标注的越界组合：(用例名, 覆盖字段, 说明)
LABEL_CASES = [
    ("score_negative", {"human_score": -5}, "负分"),
    ("score_over_max", {"human_score": 9999}, "远超满分"),
    ("score_float", {"human_score": 7.5}, "小数分"),
    ("score_string", {"human_score": "8"}, "分数是字符串"),
    ("score_null", {"human_score": None}, "分数为 null"),
    ("tags_empty", {"human_error_tags": []}, "错因空数组（合法）"),
    ("tags_unknown", {"human_error_tags": ["不存在的错因标签"]}, "枚举外错因"),
    ("tags_string", {"human_error_tags": "计算错误"}, "错因是字符串而非数组"),
    ("tags_duplicated", {"human_error_tags": ["计算错误", "计算错误"]}, "错因重复"),
    ("needs_review_missing", {"needs_review": "__DELETE__"}, "缺 needs_review（应跳过表F）"),
    ("needs_review_string", {"needs_review": "yes"}, "needs_review 是字符串"),
    ("needs_review_null", {"needs_review": None}, "needs_review 为 null"),
    ("bad_question_id", {"question_id": "Q_NOT_EXIST"}, "题目 ID 不存在"),
    ("missing_image", {"image": "images/does_not_exist.png"}, "图片路径不存在"),
    ("clarity_out_of_range", {"clarity": 999}, "清晰度越界"),
    ("clarity_negative", {"clarity": -1}, "清晰度负数"),
]


def build_manifest(out_dir: Path, questions: dict, seed: int, tag: str) -> list:
    """构造 manifest：图片用例 + 文本用例 + 标注越界用例，各自独立成条。"""
    import random
    rnd = random.Random(seed)
    qids = sorted(questions.keys())
    items = []

    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    notes = {}

    # 1. 图片类：文本给一份正常参照，让 CER 能算，问题集中在图片本身
    for name, fn in IMAGE_CASES:
        fname = "%s.png" % name
        if name in ("cmyk_jpeg", "exif_rotated"):
            fname = "%s.jpg" % name
        elif name == "animated_gif":
            fname = "%s.gif" % name
        path = img_dir / fname
        try:
            notes[name] = fn(path)
        except Exception as exc:
            notes[name] = "生成失败：%s" % exc
            continue
        qid = qids[rnd.randrange(len(qids))]
        items.append({
            "item_id": "IMG_%s" % name.upper(),
            "question_id": qid,
            "image": "images/%s" % fname,
            "ocr_text_human": "x^2-5x+6=0\n(x-2)(x-3)=0\nx1=2, x2=3",
            "human_score": 10,
            "human_error_tags": [],
            "human_grader": "EDGE_%s" % tag,
            "clarity": 85,
            "needs_review": False,
            "_edge_note": notes[name],
        })

    # 2. 文本类：不带图片，问题集中在参照文本
    for name, text, note in TEXT_CASES:
        items.append({
            "item_id": "TXT_%s" % name.upper(),
            "question_id": qids[rnd.randrange(len(qids))],
            "ocr_text_human": text,
            "human_score": 5,
            "human_error_tags": ["计算错误"],
            "human_grader": "EDGE_%s" % tag,
            "clarity": 70,
            "needs_review": True,
            "_edge_note": note,
        })

    # 3. 标注越界类：基线合法，只改一个字段
    for name, override, note in LABEL_CASES:
        item = {
            "item_id": "LBL_%s" % name.upper(),
            "question_id": qids[0],
            "ocr_text_human": "x^2-5x+6=0\nx1=2, x2=3",
            "human_score": 8,
            "human_error_tags": ["表达不完整"],
            "human_grader": "EDGE_%s" % tag,
            "clarity": 80,
            "needs_review": False,
            "_edge_note": note,
        }
        for k, v in override.items():
            if v == "__DELETE__":
                item.pop(k, None)
            else:
                item[k] = v
        items.append(item)

    return items


def write_set(out_dir: Path, questions_path: Path, items: list, tag: str, seed: int):
    out_dir.mkdir(parents=True, exist_ok=True)
    io.open(out_dir / "manifest.json", "w", encoding="utf-8", newline="\n").write(
        json.dumps(items, ensure_ascii=False, indent=2))
    import shutil
    shutil.copy2(questions_path, out_dir / "questions.json")

    counts = {"IMG": 0, "TXT": 0, "LBL": 0}
    for it in items:
        counts[it["item_id"].split("_")[0]] += 1

    io.open(out_dir / "README.md", "w", encoding="utf-8", newline="\n").write(
        "# 边界条件数据集（%s，seed=%d）\n\n"
        "**这不是实测数据。** 全部由 tools/gen_edge_cases.py 程序构造，\n"
        "用于压测链路对异常输入的处理，其 CER / 一致率 / kappa 等指标\n"
        "**不得作为产品准确率引用**。\n\n"
        "条目构成：图片类 %d 条、文本类 %d 条、标注越界类 %d 条，共 %d 条。\n\n"
        "每条 manifest 里的 `_edge_note` 字段说明该条在攻击什么。\n"
        % (tag, seed, counts["IMG"], counts["TXT"], counts["LBL"], len(items)))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="生成边界条件 / 极端情况数据集")
    ap.add_argument("out_dir", help="输出根目录（下设 tested/ 与 holdout/）")
    ap.add_argument("--seed", type=int, default=20260806)
    args = ap.parse_args()

    qpath = BASE_DIR / "data" / "questions.json"
    questions = {q["question_id"]: q
                 for q in json.load(io.open(qpath, encoding="utf-8"))["questions"]}

    root = Path(args.out_dir)
    total = {}
    for tag, seed_offset in (("tested", 0), ("holdout", 7919)):
        sub = root / tag
        items = build_manifest(sub, questions, args.seed + seed_offset, tag)
        counts = write_set(sub, qpath, items, tag, args.seed + seed_offset)
        total[tag] = (len(items), counts)
        print("已生成 %s：%d 条（图片 %d / 文本 %d / 标注 %d）"
              % (sub, len(items), counts["IMG"], counts["TXT"], counts["LBL"]))

    print()
    print("tested/  —— 我会对它跑测试并修复发现的缺陷")
    print("holdout/ —— 我不查看、不测试，留给你独立走全流程")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
