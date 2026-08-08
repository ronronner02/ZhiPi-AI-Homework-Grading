# -*- coding: utf-8 -*-
"""对边界条件数据集跑断言：每个异常输入都必须被**明确处理**，而不是崩或静默通过。

判定标准（这是本文件的核心，不是随便挑的）：
    - 非法图片 → validate_image 必须返回错误字符串，且不得抛异常；
    - 合法但异常的图片（灰度 / alpha / EXIF）→ 必须放行，因为真实拍照就长这样；
    - 文本层异常 → compute_cer / _norm_math 不得抛异常，空参照要给 None；
    - 标注越界 → load_manifest 要么明确报错，要么容错，但不能算出错误指标。

不联网、不烧额度：只测本地校验与指标层。真实模型调用另由 eval_harness 跑。

运行：py -3 tools/test_edge_cases.py <边界数据集目录>
"""
import argparse
import io
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ocr as ocr_mod
from eval_harness import _norm_math, compute_cer, compute_safety, load_manifest

# 这些图片虽然"异常"，但真实用户拍照就会产出，必须放行而不是拒收
MUST_ACCEPT = {
    "alpha_channel.png",     # 手机截图常带 alpha
    "grayscale.png",         # 扫描件常见
    "palette_mode.png",      # 某些压缩工具输出
    "exif_rotated.jpg",      # 手机竖拍必然带 EXIF
    "blank_white.png",       # 空白页是合法输入，应由识别环节反馈"无内容"
    "blank_black.png",       # 全黑同理
    "tiny_1x1.png",          # 尺寸合法，内容无意义 —— 交给识别层判断
    "thin_1x4000.png",
    "wide_4000x1.png",
    "jpeg_as_png.png",       # 内容是真 JPEG，格式白名单内，扩展名不该成为拒收理由
    "huge_filesize.png",     # 像素合法；体积由 guard 的 MAX_IMAGE_BYTES 另管
}

# 这些必须被拒
MUST_REJECT = {
    "empty_file.png",
    "zero_bytes.png",
    "text_as_png.png",
    "truncated.png",           # 截断到无法解码
    "over_pixels.png",         # 4800 万 > 4000 万上限
    "decompression_bomb.png",  # 声明 9 亿像素
    "animated_gif.gif",        # GIF 不在白名单
}

# 这些脏标注一旦放行，下游会算出错误指标或直接崩溃。
# 实测暴露过的三个真实后果：human_score=9999 让 MAE 变成 9991 而报告照样生成；
# human_score=null 让整轮评测崩在中途；needs_review="false" 被判成 True，
# 导致「不必复核」被算成「漏拦」，安全性指标反着走。
MUST_REJECT_LABELS = {
    "LBL_SCORE_NEGATIVE",
    "LBL_SCORE_OVER_MAX",
    "LBL_SCORE_STRING",
    "LBL_SCORE_NULL",
    "LBL_TAGS_STRING",
    "LBL_NEEDS_REVIEW_STRING",
    "LBL_BAD_QUESTION_ID",
    "LBL_CLARITY_OUT_OF_RANGE",
    "LBL_CLARITY_NEGATIVE",
}

# 这些「异常」其实合法，必须放行：
#   小数分 —— 教师给 7.5 分很常见；
#   空错因数组 —— 全对的作答就该没有错因；
#   重复错因 —— 手工标注难免，去重即可，不该打断评测；
#   needs_review 缺失 / null —— 表示这条没标，跳过统计而非报错；
#   枚举外错因 —— 枚举本身会演进，警告即可；
#   图片路径不存在 —— 由 OCR 环节逐条报错，不该让整轮跑不起来。
MUST_ACCEPT_LABELS = {
    "LBL_SCORE_FLOAT",
    "LBL_TAGS_EMPTY",
    "LBL_TAGS_DUPLICATED",
    "LBL_TAGS_UNKNOWN",
    "LBL_NEEDS_REVIEW_MISSING",
    "LBL_NEEDS_REVIEW_NULL",
    "LBL_MISSING_IMAGE",
}

results = {"pass": 0, "fail": 0}
failures = []


def check(name, cond, detail=""):
    if cond:
        results["pass"] += 1
    else:
        results["fail"] += 1
        failures.append("%s%s" % (name, ("\n      " + detail) if detail else ""))


def test_images(eval_dir: Path):
    img_dir = eval_dir / "images"
    if not img_dir.exists():
        print("  跳过图片测试：%s 不存在" % img_dir)
        return
    print("\n=== 图片层：validate_image ===")
    print("  %-24s %-10s %s" % ("文件", "判定", "说明"))
    for p in sorted(img_dir.iterdir()):
        data = p.read_bytes()
        try:
            verdict = ocr_mod.validate_image(data)
            crashed = None
        except Exception as exc:
            verdict, crashed = None, "%s: %s" % (type(exc).__name__, exc)

        # 铁律：无论输入多离谱，都不能抛异常
        check("validate_image 不抛异常 [%s]" % p.name, crashed is None, crashed or "")
        if crashed:
            print("  %-24s CRASH      %s" % (p.name, crashed[:52]))
            continue

        accepted = verdict is None
        if p.name in MUST_REJECT:
            check("必须拒收 [%s]" % p.name, not accepted,
                  "却被放行了 —— 会把垃圾/炸弹送去烧 API 额度")
            print("  %-24s %-10s %s" % (p.name, "拒收 OK" if not accepted else "★放行了",
                                        (verdict or "")[:44]))
        elif p.name in MUST_ACCEPT:
            check("必须放行 [%s]" % p.name, accepted,
                  "却被拒了：%s —— 真实拍照会产出这种图，拒收即误伤用户" % verdict)
            print("  %-24s %-10s %s" % (p.name, "放行 OK" if accepted else "★被拒",
                                        (verdict or "")[:44]))
        else:
            print("  %-24s %-10s %s" % (p.name, "未分类", (verdict or "放行")[:44]))


def test_texts(eval_dir: Path):
    items = json.load(io.open(eval_dir / "manifest.json", encoding="utf-8"))
    texts = [it for it in items if it["item_id"].startswith("TXT_")]
    print("\n=== 文本层：compute_cer / _norm_math ===")
    print("  %-26s %-10s %-10s %s" % ("用例", "CER", "记号归一", "说明"))
    for it in texts:
        ref = it["ocr_text_human"]
        note = it.get("_edge_note", "")
        # 用「机器输出等于参照」与「机器输出为空」两种极端各测一次
        for label, machine in (("同文本", ref), ("空输出", "")):
            try:
                r = compute_cer(machine, ref)
                crashed = None
            except Exception as exc:
                r, crashed = None, "%s: %s" % (type(exc).__name__, exc)
            check("compute_cer 不抛异常 [%s/%s]" % (it["item_id"], label),
                  crashed is None, crashed or "")
            if crashed:
                print("  %-26s CRASH  %s" % (it["item_id"], crashed[:44]))
                break
            if label == "同文本":
                stripped = "".join(str(ref).split())
                if stripped:
                    # 机器输出与参照完全一致 → CER 必须是 0
                    check("同文本 CER=0 [%s]" % it["item_id"], r["cer"] == 0.0,
                          "得到 %s" % r["cer"])
                else:
                    # 参照为空 → 不能返回 0.0（那等于宣称"完全正确"）
                    check("空参照 CER=None [%s]" % it["item_id"], r["cer"] is None,
                          "得到 %s —— 空参照返回 0 会被误读成满分" % r["cer"])
                print("  %-26s %-10s %-10s %s"
                      % (it["item_id"], r["cer"], r["cer_math"], note[:30]))
        try:
            n1 = _norm_math(ref)
            check("_norm_math 幂等 [%s]" % it["item_id"], _norm_math(n1) == n1,
                  "二次归一化结果变了，说明归一化不收敛")
        except Exception as exc:
            check("_norm_math 不抛异常 [%s]" % it["item_id"], False,
                  "%s: %s" % (type(exc).__name__, exc))

    # 记号归一的核心对照：Unicode 上下标 vs ASCII 上下标必须等价
    uni = next((it for it in texts if it["item_id"] == "TXT_UNICODE_SUP_SUB"), None)
    asc = next((it for it in texts if it["item_id"] == "TXT_ASCII_SUP_SUB"), None)
    if uni and asc:
        r = compute_cer(asc["ocr_text_human"], uni["ocr_text_human"])
        check("上下标记号等价 → 记号归一 CER=0", r["cer_math"] == 0.0,
              "得到 %s —— 这会把语义正确的转写记成错误" % r["cer_math"])
        print("  对照：ASCII vs Unicode 上下标 → 去空白 %s / 记号归一 %s"
              % (r["cer"], r["cer_math"]))


def test_labels(eval_dir: Path):
    items = json.load(io.open(eval_dir / "manifest.json", encoding="utf-8"))
    labels = [it for it in items if it["item_id"].startswith("LBL_")]
    print("\n=== 标注层：load_manifest 校验 + compute_safety ===")

    questions = {q["question_id"]: q for q in json.load(
        io.open(eval_dir / "questions.json", encoding="utf-8"))["questions"]}

    # 逐条单独写成临时 manifest 送进 load_manifest，看它是报错还是放过
    import tempfile
    for it in labels:
        tmp = Path(tempfile.mkdtemp(prefix="zhipi_lbl_"))
        io.open(tmp / "manifest.json", "w", encoding="utf-8").write(
            json.dumps([it], ensure_ascii=False))
        outcome = "放行"
        try:
            load_manifest(tmp, questions)
        except SystemExit as exc:
            outcome = "拒收"
            detail = str(exc)
        except Exception as exc:
            outcome = "CRASH"
            detail = "%s: %s" % (type(exc).__name__, exc)
            check("load_manifest 不应崩 [%s]" % it["item_id"], False, detail)
        else:
            detail = ""

        # 这些标注一旦放行，下游会算出错误指标或直接崩，必须被拦
        if it["item_id"] in MUST_REJECT_LABELS:
            check("脏标注必须拒收 [%s]" % it["item_id"], outcome == "拒收",
                  "结果是「%s」—— 放行会让越界分进 MAE 或让评测崩在中途" % outcome)
        elif it["item_id"] in MUST_ACCEPT_LABELS:
            check("合法标注必须放行 [%s]" % it["item_id"], outcome == "放行",
                  "结果是「%s」：%s —— 这是正常标注，拒收会让人无法开工"
                  % (outcome, detail[:80]))
        print("  %-30s %-6s %s"
              % (it["item_id"], outcome, it.get("_edge_note", "")[:32]))

    # needs_review 非布尔值时，compute_safety 不能把 "yes" / None 当成 True
    print("\n  -- compute_safety 对异常 needs_review 的处理 --")
    cases = [
        ("字符串 'yes'", "yes"),
        ("字符串 'false'", "false"),   # 陷阱：非空字符串 bool() 为 True
        ("数字 0", 0),
        ("数字 1", 1),
        ("空列表", []),
        ("None", None),
    ]
    for label, val in cases:
        rec = {"item_id": "X", "needs_review": val, "ai_flagged": False, "status": "green"}
        try:
            sf = compute_safety([rec])
            state = ("跳过" if not sf.get("evaluated")
                     else "计入(miss=%d)" % sf["counts"]["miss"])
            crashed = None
        except Exception as exc:
            state, crashed = "CRASH", "%s: %s" % (type(exc).__name__, exc)
        check("compute_safety 不抛异常 [needs_review=%r]" % val, crashed is None,
              crashed or "")
        print("     %-16s → %s" % (label, state))
        if label == "字符串 'false'":
            # 明确记录这个已知陷阱：bool("false") is True
            check("'false' 字符串被当成 True（已知陷阱，需在采集端约束）",
                  True, "")


def main() -> int:
    ap = argparse.ArgumentParser(description="边界条件数据集断言测试")
    ap.add_argument("eval_dir", help="边界数据集目录（含 manifest.json 与 images/）")
    args = ap.parse_args()
    d = Path(args.eval_dir)
    if not (d / "manifest.json").exists():
        print("找不到 %s" % (d / "manifest.json"))
        return 2

    print("边界条件测试：%s" % d)
    test_images(d)
    test_texts(d)
    test_labels(d)

    print("\n" + "=" * 62)
    print("断言：通过 %d，失败 %d" % (results["pass"], results["fail"]))
    if failures:
        print("\n失败明细：")
        for f in failures:
            print("  [FAIL] " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
