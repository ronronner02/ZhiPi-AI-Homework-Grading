# -*- coding: utf-8 -*-
"""三阶段识别 vs 单次识别的真实 A/B 对比（只测识别层，不批改）。

回答一个今天之前没人验证过的问题：**新加的三阶段链路在真实照片上到底赢没赢
单次调用**。三阶段（版面 → 归属 → 定向复识）把每页调用次数从 1 次抬到最多
8 次，失败面也大了三倍；如果它并没有比单次识别多认出题目、多归对答案，那
`ZHIPI_STAGED_RECOGNIZE=0` 关掉比继续调 prompt 划算得多。

只跑识别，不跑批改：批改层的分歧会掩盖识别层的差异，而四类问题里有三类
（潦草、越界、横置）本来就出在识别层。

    py -3 tools/ab_recognize.py                    # 跑全部用例
    py -3 tools/ab_recognize.py hardwork_117       # 只跑一例

结果写到 tools/ab_recognize_out/<用例>.json，摘要打到标准输出。
会真实消耗额度：单次模式 1 次调用/图，三阶段最多 8 次/图。
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 刻意 import app 而不是自己再读一遍 deploy/.env：要跑的正是「应用自己看到的
# 那套凭据」，另写一份加载逻辑就可能测出与线上不同的配置。
import app as demo_app  # noqa: E402  （副作用：按 app.py 的规则加载 .env）
from pipeline import ocr  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):        # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8")

DATA = Path(r"E:\希沃智教π\测评数据\测评数据")
OUT = Path(__file__).resolve().parent / "ab_recognize_out"

# 每条用例对应测评中暴露的一类问题，expect 是人工看图得到的事实，
# 用来判断模型输出对不对——不是让脚本自动判分，是给人看的对照。
CASES = {
    "89_student_2": {
        "path": "数学/学生页/89_student_2.jpg",
        "issue": "P3 越界作答归属",
        "expect": "三题实际都对；第 1、3 题答案写出了答题区，旧链路只批到第 2 题",
    },
    "hardwork_117": {
        "path": "数学/学生页/hardwork_117.jpg",
        "issue": "P1 潦草字与分式",
        "expect": "第 5 题 (2) 应为 1/(x+1)；第 6 题 (3) 应为 1/(b-a)",
    },
    "hardwork_127": {
        "path": "数学/学生页/hardwork_127.jpg",
        "issue": "P1 潦草选择题",
        "expect": "第 1 题（x、y 同扩 2 倍，值不变）学生写 D；系统曾读成 A",
    },
    "83_student_2": {
        "path": "数学/学生页/83_student_2.jpg",
        "issue": "P3 越界作答 + 分式",
        "expect": "第 1、3 题作答写出答题区但都对；第 6 题 cos D = 1/3 也对",
    },
    "courselearn_21": {
        "path": "数学/学生页/courselearn_21.jpg",
        "issue": "P4 横置图",
        "expect": "EXIF Orientation=8，正立后应为 2484×3508",
    },
}


class CallCounter:
    """统计一次识别里真实发生了几次模型调用。

    直接包住 ocr._call_vlm——三个阶段都从这里出去，包一层就能同时拿到次数和
    每次的耗时，比在三处各加计数器可靠。
    """

    def __init__(self):
        self.calls = []
        self._orig = ocr._call_vlm

    def __enter__(self):
        def wrapped(image_bytes, mime, prompt):
            t0 = time.time()
            head = prompt.strip().splitlines()[0][:24] if prompt.strip() else ""
            try:
                data = self._orig(image_bytes, mime, prompt)
                self.calls.append({"stage": head, "sec": round(time.time() - t0, 1),
                                   "ok": True, "bytes": len(image_bytes)})
                return data
            except Exception as exc:
                self.calls.append({"stage": head, "sec": round(time.time() - t0, 1),
                                   "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                                   "bytes": len(image_bytes)})
                raise
        ocr._call_vlm = wrapped
        return self

    def __exit__(self, *a):
        ocr._call_vlm = self._orig
        return False


def brief(questions):
    """把识别结果压成一行一题，便于两种模式肉眼对齐。"""
    rows = []
    for q in questions:
        rows.append({
            "no": q.get("question_no") or q.get("no") or "",
            "qtype": q.get("qtype") or "",
            "answer": (q.get("answer") or "")[:60],
            "bbox": "有" if q.get("bbox") else "无",
            "refined": bool(q.get("refined")),
            "legible_hint": q.get("legible_hint"),
        })
    return rows


def run_one(name, image_bytes, mime, staged):
    """跑一种模式，返回结果 + 观测到的调用序列。失败也要记下来，
    降级路径本身就是这次要验的东西之一。"""
    os.environ["ZHIPI_STAGED_RECOGNIZE"] = "1" if staged else "0"
    t0 = time.time()
    with CallCounter() as cc:
        try:
            if staged:
                data = ocr.recognize_vlm_staged(image_bytes, mime)
            else:
                data = ocr.recognize_vlm(image_bytes, mime)
            err = None
        except Exception as exc:
            data, err = {}, "%s: %s" % (type(exc).__name__, exc)
    # 直接用返回值里的 questions，不要再 normalize 一次：单次路径在
    # recognize_vlm 内部已经规整过，而 staged 路径走的是 _normalize_layout，
    # 再套一层 _normalize_questions 会按白名单重建 dict，把 refined /
    # legible_hint 这些只有三阶段才产出的字段丢掉——那是脚本读错，不是链路缺陷。
    questions = (data.get("questions") or []) if data else []
    return {
        "mode": "staged" if staged else "single",
        "error": err,
        "sec": round(time.time() - t0, 1),
        "vlm_calls": len(cc.calls),
        "calls": cc.calls,
        "subject": data.get("subject") if data else None,
        "question_count": len(questions),
        "questions": brief(questions),
        # 复识记录（三阶段独有）：每条含原因、改前、改后。判断「复识到底救没救回
        # 那道题」只能看它——questions 里只剩最终值，看不出中间发生过什么。
        "refine_records": (data.get("refined") or []) if data else [],
        "raw_keys": sorted(data.keys()) if data else [],
    }


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if not ocr.vlm_configured():
        print("未配置 ZHIPI_VLM_API_KEY，无法做真实对比。")
        return 1
    OUT.mkdir(exist_ok=True)
    print("模型：%s" % os.environ.get("ZHIPI_VLM_MODEL", "qwen-vl-max"))
    print("复识预算：%d 题/页\n" % ocr._refine_budget())

    for name, case in CASES.items():
        if only and only != name:
            continue
        src = DATA / case["path"]
        raw = src.read_bytes()
        # 走与上传入口一致的朝向归一，否则测的就不是线上那张工作图
        fixed = ocr.normalize_orientation(raw, "image/jpeg")
        rotated = fixed != raw

        print("=" * 66)
        print("%s  [%s]" % (name, case["issue"]))
        print("  人工事实：%s" % case["expect"])
        print("  朝向归一：%s（%d KB → %d KB）"
              % ("已旋转" if rotated else "无需旋转", len(raw) // 1024, len(fixed) // 1024))

        result = {"case": name, "issue": case["issue"], "expect": case["expect"],
                  "rotated": rotated, "runs": []}
        for staged in (True, False):
            r = run_one(name, fixed, "image/jpeg", staged)
            result["runs"].append(r)
            print("\n  -- %s --  %d 次调用 / %.1fs%s"
                  % (r["mode"], r["vlm_calls"], r["sec"],
                     "  ** 失败：%s" % r["error"] if r["error"] else ""))
            print("     识别到 %d 题" % r["question_count"])
            for row in r["questions"]:
                print("       %-6s %-4s bbox=%s%s%s  答案=%s"
                      % (row["no"], row["qtype"], row["bbox"],
                         " 复识" if row["refined"] else "",
                         " 不清晰" if row["legible_hint"] is False else "",
                         row["answer"] or "(空)"))
            for c in r["calls"]:
                if not c["ok"]:
                    print("     ! 调用失败 [%s] %s" % (c["stage"], c.get("error")))
            for rec in r["refine_records"]:
                print("     复识 %-4s [%-16s] %s  「%s」→「%s」conf=%.2f"
                      % (rec.get("no") or rec.get("index"), rec.get("reason"),
                         "改写" if rec.get("changed") else "维持原值",
                         (rec.get("before") or "(空)").replace("\n", "⏎")[:28],
                         (rec.get("after") or "(空)").replace("\n", "⏎")[:28],
                         rec.get("confidence", 0)))

        (OUT / ("%s.json" % name)).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
    print("明细已写入 %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
