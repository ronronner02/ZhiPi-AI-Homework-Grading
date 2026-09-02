# -*- coding: utf-8 -*-
"""对照实验：换 prompt 能不能读对「分子是 1 的分式」。

失败样本（hardwork_117 第 5 题第 (2) 问）：题目 (x-1)/(x^2-1)，学生写的是
1/(x+1)——一条分数线，线上一个 1，线下 x+1。整页识别读成 "x-1"，裁图放大复识
改成 "x"，两次都丢掉了分子和分数线，只抓到分母的一部分。

现行 REFINE_PROMPT 里只有一句「分数一律写成 a/b」，没有针对这个失败模式：
模型不是不会写 a/b，是**根本没看见那条分数线**。所以对照三种问法：

    A 现行 prompt
    B 现行 + 分式必须分子分母都读出、分子为 1 也要写
    C 在 B 之上，先让它描述手写结构（几条分数线、线上线下各是什么）再给转写

C 的赌注是：强制先描述结构，模型就必须先决定「有没有分数线」，而不是扫一眼
写个大概。代价是输出变长、多几十个 token。

    py -3 tools/probe_fraction_prompt.py

消耗：1 次整页识别 + 3 种 prompt × ROUNDS 次局部读取。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402
from pipeline import ocr  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

IMG = Path(r"E:\希沃智教π\测评数据\测评数据\数学\学生页\hardwork_117.jpg")
TARGET_NO = "5"
# 人工事实：第 (2) 问 (x-1)/(x^2-1) = 1/(x+1)。判定只看这一问读没读出分数结构。
TRUTH_HINT = "(2) 应为 1/(x+1)"
ROUNDS = 2

FRACTION_RULE = """- **分式必须把分子和分母都读出来**：先找横着的分数线，线**上方**是分子、
  **下方**是分母，写成 分子/分母。分子是 1 时同样要写出来——写成 1/(b-a)，
  绝不能只写 b-a。**只读到分母就当成答案，是这类题最常见的错读**，
  答案会完全不同；
- 分母或分子是多项式时用括号括起来：1/(x+1)、(a-b)/(a+b)、(x-1)/(x^2-1)；"""

STRUCTURE_RULE = """
【先看结构，再转写】
在 layout 字段里先用一句话描述你看到的手写结构，例如：
  「一条分数线，线上是 1，线下是 x+1」
  「没有分数线，只有一个 x」
描述完再填 answer。answer 必须与你描述的结构一致。
"""


def build(kind, no, qtype, stem):
    base = ocr.REFINE_PROMPT_HEAD % (no, qtype, stem)
    if kind == "A":
        return base
    # 把分式规则插在原有那条「分数一律写成 a/b」之后，其余保持不变，
    # 保证三组之间只差这一个变量
    anchor = "- 数学公式用线性写法"
    out = base.replace(anchor, FRACTION_RULE + "\n" + anchor)
    if kind == "C":
        out = out.replace('只输出 JSON，不要输出多余文字：\n{"answer"',
                          STRUCTURE_RULE + '\n只输出 JSON，不要输出多余文字：\n'
                          '{"layout": "结构描述", "answer"')
    return out


def main():
    if not ocr.vlm_configured():
        print("未配置 ZHIPI_VLM_API_KEY")
        return 1
    raw = ocr.normalize_orientation(IMG.read_bytes(), "image/jpeg")
    # 用版面解析阶段拿题目，不用单次识别：单次识别会把第 5 题的三个小问拆成
    # 三条题号为 "(1)" 的独立记录，按题号根本找不到「第 5 题」。
    layout = ocr._call_vlm(raw, "image/jpeg", ocr.LAYOUT_PROMPT)
    items = ocr._normalize_layout(layout)
    print("版面解析题号：%s" % [it.get("no") for it in items])
    q = None
    for item in items:
        if str(item.get("no", "")).strip().rstrip(".") == TARGET_NO:
            q = item
            break
    if not q or not q.get("bbox"):
        print("没定位到第 %s 题（或它没有 bbox）" % TARGET_NO)
        return 1

    crop = ocr._crop_region(raw, q["bbox"], ocr._CROP_PAD)
    if not crop:
        print("裁图失败")
        return 1
    print("第 %s 题裁图 %d KB   人工事实：%s" % (TARGET_NO, len(crop[0]) // 1024, TRUTH_HINT))
    print("题干：%s\n" % (q.get("stem") or "")[:70].replace("\n", " "))

    for kind in ("A", "B", "C"):
        prompt = build(kind, q.get("no") or TARGET_NO, q.get("qtype") or "计算题",
                       (q.get("stem") or "").strip()[:500] or "（未识别到印刷题干）")
        print("-- prompt %s --" % kind)
        for i in range(ROUNDS):
            try:
                d = ocr._call_vlm(crop[0], crop[1], prompt)
            except Exception as exc:
                print("   第 %d 次失败：%s" % (i + 1, exc))
                continue
            ans = str(d.get("answer") or "").replace("\n", " ⏎ ")
            hit = "1/(x+1)" in ans.replace(" ", "")
            print("   %d) %s answer=%s" % (i + 1, "✔读出分式" if hit else "✘仍丢分式", ans[:96]))
            if d.get("layout"):
                print("      结构：%s" % str(d["layout"])[:90])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
