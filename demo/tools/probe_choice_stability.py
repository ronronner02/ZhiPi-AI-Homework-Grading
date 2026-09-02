# -*- coding: utf-8 -*-
"""一次性实验：模型对同一道潦草选择题的读数稳不稳定。

背景：A/B 联调发现 hardwork_117 第 1 题学生写的是 D，整页识别读成 B，裁图
放大复识**又**读成 B，自报 confidence 1.00。这直接否掉了「按模型自报置信度
挑出可疑题」的思路——读错时它同样满分自信。

那还剩一条路：模型的错误是否**不稳定**？如果同一张裁图读 5 次会出现 B/D 分
歧，那「独立多读、不一致才报警」就是可用的信号，代价是每题多几次调用；如果
5 次全是 B，说明这是模型的确定性错误，任何自洽性检测都抓不到它，只能靠教师
复核——那就该把力气花在「让教师一眼看到选择题转写并快速改」上，而不是继续
加检测层。

    py -3 tools/probe_choice_stability.py

消耗：1 次整页识别 + 5 次局部复识。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402  （副作用：加载 deploy/.env）
from pipeline import ocr  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

IMG = Path(r"E:\希沃智教π\测评数据\测评数据\数学\学生页\hardwork_117.jpg")
TRUTH = "D"          # 人工看图确认的答案
ROUNDS = 5


def main():
    if not ocr.vlm_configured():
        print("未配置 ZHIPI_VLM_API_KEY")
        return 1
    raw = ocr.normalize_orientation(IMG.read_bytes(), "image/jpeg")

    data = ocr.recognize_vlm(raw, "image/jpeg")
    q1 = None
    for q in data.get("questions") or []:
        if str(q.get("no", "")).strip() in ("1", "1.", "一"):
            q1 = q
            break
    if not q1:
        print("整页识别里没找到第 1 题，实验中止")
        return 1
    print("整页识别第 1 题：qtype=%s answer=%r bbox=%s"
          % (q1.get("qtype"), q1.get("answer"), q1.get("bbox")))
    if not q1.get("bbox"):
        print("第 1 题没有 bbox，无法裁图")
        return 1

    crop = ocr._crop_region(raw, q1["bbox"], ocr._CROP_PAD)
    if not crop:
        print("裁图失败")
        return 1
    print("裁图 %d KB，独立复识 %d 次（人工事实：%s）\n" % (len(crop[0]) // 1024, ROUNDS, TRUTH))

    prompt = ocr.REFINE_PROMPT_HEAD % ("1", q1.get("qtype") or "选择题",
                                       (q1.get("stem") or "").strip()[:500] or "（未识别到印刷题干）")
    seen = {}
    for i in range(ROUNDS):
        try:
            d = ocr._call_vlm(crop[0], crop[1], prompt)
        except Exception as exc:
            print("  第 %d 次调用失败：%s" % (i + 1, exc))
            continue
        ans = str(d.get("answer") or "").strip()
        seen[ans] = seen.get(ans, 0) + 1
        print("  第 %d 次：answer=%-8r legible=%-5s confidence=%s"
              % (i + 1, ans, d.get("legible"), d.get("confidence")))

    print("\n分布：%s" % seen)
    if len(seen) > 1:
        print("→ 读数不稳定：多读取一致性可以作为报警信号。")
    elif TRUTH in seen:
        print("→ 稳定读对。")
    else:
        print("→ %d 次稳定读成同一个错误答案。自洽性检测抓不到这类错误，"
              "别再加检测层，改为让教师一眼看见并能一键改。" % ROUNDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
