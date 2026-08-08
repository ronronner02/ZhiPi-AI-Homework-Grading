# -*- coding: utf-8 -*-
"""用内置样例生成一个**临时的**评测集，只为验证 eval_harness 能跑通真实模型链路。

为什么需要它：eval_harness 的 --selftest 走的是 mock 批改，验证的是脚本自身的
算术逻辑；它证明不了「真实 VLM 识别 + 真实 LLM 批改」这条链路是通的。而真实
链路上的坑（网关超时、模型不返回 JSON、图片编码失败）恰恰只在真调用时才暴露。

**产出的目录禁止作为实测结果引用**：内置样例图是 gen_sample_images.py 程序合成的
仿手写，不是真实学生笔迹。它只回答一个问题——「管线通不通」，不回答「准不准」。

用法：
    py -3 tools/make_pipeline_check_set.py <输出目录> [--limit 3]
"""
import argparse
import io
import json
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def human_score(sub: dict, rubric: list) -> int:
    """从预置步骤标注推出人工总分：标为 correct 的步骤拿满该步分值。"""
    return sum(step["max_score"]
               for ann, step in zip(sub.get("step_annotations", []), rubric)
               if ann.get("is_correct"))


def human_tags(sub: dict) -> list:
    """人工错因标签集合（去重、稳定排序，便于逐次比对）。"""
    return sorted({ann["error_tag"]
                   for ann in sub.get("step_annotations", [])
                   if ann.get("error_tag")})


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成用于验证真实模型链路的临时评测集（非实测数据）")
    ap.add_argument("out_dir", help="输出目录")
    ap.add_argument("--limit", type=int, default=3,
                    help="取前 N 份作答（默认 3；每份约 2 次真实调用，别一次取太多）")
    args = ap.parse_args()

    data = json.load(io.open(BASE / "data" / "submissions.json", encoding="utf-8"))
    qs = json.load(io.open(BASE / "data" / "questions.json", encoding="utf-8"))
    rubrics = {q["question_id"]: q["rubric"] for q in qs["questions"]}

    out = Path(args.out_dir)
    (out / "images").mkdir(parents=True, exist_ok=True)

    manifest = []
    picked = data["submissions"][:max(1, args.limit)]
    for sub in picked:
        sid = sub["submission_id"]
        src = BASE / "data" / "sample_images" / (sid + ".png")
        if not src.exists():
            print("跳过 %s：找不到样例图 %s" % (sid, src))
            continue
        shutil.copy2(src, out / "images" / (sid + ".png"))
        rubric = rubrics[sub["question_id"]]
        score = human_score(sub, rubric)
        full = sum(step["max_score"] for step in rubric)
        manifest.append({
            "item_id": sid,
            "question_id": sub["question_id"],
            "image": "images/%s.png" % sid,
            # 预置转写即「人工校对转写」——对合成图来说它就是 ground truth
            "ocr_text_human": sub["ocr"]["text"],
            "human_score": score,
            "human_error_tags": human_tags(sub),
            "human_grader": "SYNTHETIC",   # 刻意标死，防止被误当成真实教师判分
            "clarity": int(round(float(sub["ocr"]["clarity"]))),
            # 得分率 <80% 视为「教师必须过目」，让漏拦率一节也能被走到。
            # 真实评测里这个字段必须由人逐份判断，不能用分数自动推。
            "needs_review": bool(full) and (score / full) < 0.8,
        })

    io.open(out / "manifest.json", "w", encoding="utf-8", newline="\n").write(
        json.dumps(manifest, ensure_ascii=False, indent=2))

    # 题库整份复制，保证 harness 不依赖 demo/data 的相对路径
    shutil.copy2(BASE / "data" / "questions.json", out / "questions.json")

    io.open(out / "DO_NOT_CITE.txt", "w", encoding="utf-8", newline="\n").write(
        "本目录由 tools/make_pipeline_check_set.py 生成，图片为程序合成的仿手写，\n"
        "不是真实学生笔迹。它只用于验证「真实 VLM + 真实 LLM」链路能否跑通，\n"
        "其 CER / 一致率 / kappa 全部不得作为实测指标引用。\n"
        "真实评测集请按 docs/07 的预注册方案，用真实作业照片与教师判分构建。\n")

    print("已生成临时评测集：%s" % out)
    print("  条目数：%d" % len(manifest))
    print("  提醒：这不是实测数据，仅验证管线连通性")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
