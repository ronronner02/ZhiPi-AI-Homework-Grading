"""第二模型 / 二次复批实测：耗时、分数稳定性、是否装得进超时预算。

为什么要有这个脚本：交叉验证与二次复批都**按设计静默失败**——拿不到结论就
跳过，主批改照常出结果。这是对的（增强项不该拖垮核心链路），但副作用是
配错模型、模型太慢、模型输出不稳，界面上全都看不出来，只是置信度因子悄悄
退回模型自报值。换第二模型后必须实测一遍，否则"配了"和"生效"是两件事。

重点看三件事：
1. 耗时 vs ZHIPI_CROSS_TIMEOUT —— 超预算就是永远失效；
2. 同一份作答重复批的分数离散度 —— 第二模型当裁判，它自己不稳的话，
   转红判定会随机漂，同一份作业两次演示得出不同分流，这是答辩硬伤；
3. 二次复批（同模型）耗时 —— 它在主链路上串行执行，直接加到每份的等待里。

用法：
    py -3 tools/bench_second_model.py            # 默认每项 3 次
    py -3 tools/bench_second_model.py --runs 5
    py -3 tools/bench_second_model.py --timeout 180   # 量真实耗时用大超时
"""
import argparse
import os
import statistics
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import app as demo_app  # noqa: E402  （副作用：按 app.py 的规则加载 .env）
from pipeline import grader  # noqa: E402

# 一份**有明确错误**的作答：第二个因式符号错了（应为 x-3），正确答案 x=2,3。
# 刻意不用全对的作答——全对时两个模型都给满分，分差恒为 0，
# 测不出裁判的分辨力，只能测出"它没崩"。
WRONG_TEXT = "x^2-5x+6=0  (x-2)(x+3)=0  x=2 or x=-3"


def stat_line(name, samples):
    if not samples:
        return "  %-22s 无有效样本" % name
    if len(samples) == 1:
        return "  %-22s %.1f" % (name, samples[0])
    return "  %-22s 均值 %.1f  最小 %.1f  最大 %.1f  极差 %.1f" % (
        name, statistics.mean(samples), min(samples), max(samples),
        max(samples) - min(samples))


# 全对的作答：用来抓「乱扣分」的裁判。
# 小模型最典型的失败不是慢，而是把满分卷判成低分（本项目实测 qwen3.5-2b
# 给过全对作答 2/10）。这种裁判比没有裁判更糟——它会把好卷误判成分歧、
# 强制转红，把人工复核队列灌满噪声。
RIGHT_TEXT = "x^2-5x+6=0  (x-2)(x-3)=0  x=2 or x=3"


def probe_candidate(model, q, budget, runs, timeout):
    """拿一个候选模型跑「错卷 + 对卷」，返回耗时与判分表现。"""
    creds = dict(grader.llm_credentials_2())
    creds["model"] = model
    row = {"model": model, "times": [], "wrong": [], "right": [], "errors": 0}
    for _ in range(runs):
        for text, key in ((WRONG_TEXT, "wrong"), (RIGHT_TEXT, "right")):
            t = time.time()
            try:
                data = grader._call_llm(creds, grader._build_prompt(q, text),
                                        timeout=timeout)
                _, total, _ = grader._parse_llm_steps(q, data)
                row["times"].append(time.time() - t)
                row[key].append(total)
            except Exception:
                row["errors"] += 1
    return row


def run_candidates(q, budget, runs, timeout, models):
    print("\n" + "=" * 66)
    print("4 · 候选第二模型横评（错卷应约 5/10，对卷应 10/10）")
    print("=" * 66)
    print("  预算 %d s。每个候选跑 %d 轮 × 2 份（错卷 + 对卷）。\n" % (budget, runs))
    rows = []
    for m in models:
        row = probe_candidate(m, q, budget, runs, timeout)
        rows.append(row)
        if not row["times"]:
            print("  %-32s 全部失败" % m)
            continue
        slow = max(row["times"])
        fits = "装得进" if slow <= budget else "超预算"
        print("  %-32s 最慢 %5.1fs [%s]  错卷 %s  对卷 %s%s" % (
            m, slow, fits,
            "/".join(str(s) for s in row["wrong"]) or "—",
            "/".join(str(s) for s in row["right"]) or "—",
            "  失败%d次" % row["errors"] if row["errors"] else ""))

    print("\n" + "-" * 66)
    print("可用候选（装得进预算 且 判分合理）")
    print("-" * 66)
    good = []
    for r in rows:
        if not r["times"] or max(r["times"]) > budget or r["errors"]:
            continue
        # 判分合理：错卷不给满分也不给 0，对卷必须接近满分。
        # 对卷判低分的裁判会把好卷误判成分歧，直接否掉。
        mx = q["max_score"]
        ok_wrong = all(0 < s < mx for s in r["wrong"])
        ok_right = all(s >= mx * 0.8 for s in r["right"])
        if ok_wrong and ok_right:
            good.append(r)
    if good:
        good.sort(key=lambda r: max(r["times"]))
        for r in good:
            print("  %-32s 最慢 %.1fs" % (r["model"], max(r["times"])))
        print("\n  建议：ZHIPI_LLM_MODEL_2=%s" % good[0]["model"])
    else:
        print("  无候选同时满足「装得进 %d s 预算」与「判分合理」。" % budget)
        print("  说明这个网关上没有又快又靠谱的裁判，两条路：")
        print("    · 调大 ZHIPI_CROSS_TIMEOUT，接受黄/红件变慢；")
        print("    · 留空 ZHIPI_LLM_MODEL_2，让交叉验证明确「未启用」，")
        print("      而不是配了却每次静默超时。")
    return rows


def main():
    ap = argparse.ArgumentParser(description="第二模型实测")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=180,
                    help="量耗时用的上限，刻意放大以看到真实值")
    ap.add_argument("--candidates", default="",
                    help="逗号分隔的候选模型；给了就只跑横评，跳过前三节")
    args = ap.parse_args()

    q = demo_app.QUESTIONS[list(demo_app.QUESTIONS)[0]]
    c1 = grader.llm_credentials()
    c2 = grader.llm_credentials_2()
    budget = grader._cross_timeout()

    print("=" * 66)
    print("配置")
    print("=" * 66)
    print("  主批改模型      %s" % (c1["model"] if c1 else "未配置"))
    print("  第二模型        %s" % (c2["model"] if c2 else "未配置（交叉验证不启用）"))
    print("  二次复批开关    ZHIPI_DOUBLE_CHECK=%s（%s）" % (
        os.environ.get("ZHIPI_DOUBLE_CHECK", "1"),
        "关闭" if str(os.environ.get("ZHIPI_DOUBLE_CHECK", "1")).strip() == "0" else "启用"))
    print("  交叉验证预算    %d s" % budget)
    print("  主批改超时      %d s" % grader._llm_timeout())
    print("  题目            %s %s（满分 %d）" % (
        q["question_id"], q["title"], q["max_score"]))

    if args.candidates:
        models = [m.strip() for m in args.candidates.split(",") if m.strip()]
        run_candidates(q, budget, args.runs, args.timeout, models)
        return 0

    # ---------- 1. 主批改基线 ----------
    print("\n" + "=" * 66)
    print("1 · 主批改基线（每份必跑）")
    print("=" * 66)
    main_times, main_scores = [], []
    for i in range(args.runs):
        t = time.time()
        try:
            data = grader._call_llm(c1, grader._build_prompt(q, WRONG_TEXT))
            _, total, _ = grader._parse_llm_steps(q, data)
            dt = time.time() - t
            main_times.append(dt)
            main_scores.append(total)
            print("  第%d次  %5.1fs  得分 %s/%d" % (i + 1, dt, total, q["max_score"]))
        except Exception as exc:
            print("  第%d次  失败 %.1fs  %s" % (i + 1, time.time() - t, type(exc).__name__))
    print(stat_line("耗时", main_times))
    print(stat_line("得分", [float(s) for s in main_scores]))

    # ---------- 2. 二次复批（同模型，本次新启用） ----------
    print("\n" + "=" * 66)
    print("2 · 二次一致性复批（同模型，串行加在主批改之后）")
    print("=" * 66)
    if str(os.environ.get("ZHIPI_DOUBLE_CHECK", "1")).strip() == "0":
        print("  已关闭，跳过")
    else:
        first = {"total_score": main_scores[0] if main_scores else 2,
                 "error_tags": [], "max_score": q["max_score"]}
        cs_times, agreements = [], []
        for i in range(args.runs):
            t = time.time()
            try:
                r = grader.consistency_check(q, WRONG_TEXT, first, c1)
                dt = time.time() - t
                cs_times.append(dt)
                agreements.append(r["agreement"])
                print("  第%d次  %5.1fs  复批得分 %s  一致性 %.1f" % (
                    i + 1, dt, r["second_score"], r["agreement"]))
            except Exception as exc:
                print("  第%d次  失败 %.1fs  %s" % (i + 1, time.time() - t, type(exc).__name__))
        print(stat_line("耗时", cs_times))
        print(stat_line("一致性分", agreements))
        if cs_times:
            print("  → 单份总等待约 %.1fs（主批改 %.1f + 复批 %.1f）" % (
                statistics.mean(main_times or [0]) + statistics.mean(cs_times),
                statistics.mean(main_times or [0]), statistics.mean(cs_times)))

    # ---------- 3. 交叉验证（第二模型） ----------
    print("\n" + "=" * 66)
    print("3 · 双模型交叉验证（仅黄/红触发）")
    print("=" * 66)
    if not c2:
        print("  第二模型未配置，跳过")
    else:
        first_total = main_scores[0] if main_scores else 2
        cc_times, cc_scores, gaps, escs = [], [], [], []
        for i in range(args.runs):
            t = time.time()
            try:
                r = grader.cross_check(q, WRONG_TEXT, first_total, c2,
                                      timeout=args.timeout)
                dt = time.time() - t
                cc_times.append(dt)
                cc_scores.append(float(r["model2_score"]))
                gaps.append(r["gap"])
                escs.append(r["escalated"])
                print("  第%d次  %5.1fs  第二模型 %s  与首轮差 %.1f  转红=%s%s" % (
                    i + 1, dt, r["model2_score"], r["gap"], r["escalated"],
                    "" if dt <= budget else "   ← 超出 %ds 预算，实链路会失效" % budget))
            except Exception as exc:
                print("  第%d次  失败 %5.1fs  %s" % (
                    i + 1, time.time() - t, type(exc).__name__))
        print(stat_line("耗时", cc_times))
        print(stat_line("第二模型得分", cc_scores))
        print(stat_line("分差", gaps))

        print("\n" + "-" * 66)
        print("结论")
        print("-" * 66)
        if not cc_times:
            print("  第二模型全部调用失败 —— 交叉验证等于未启用")
        else:
            worst = max(cc_times)
            if worst <= budget:
                print("  [通过] 最慢 %.1fs，装得进 %ds 预算，实链路会真正生效" % (worst, budget))
            else:
                need = int(worst) + 5
                print("  [失效] 最慢 %.1fs 超出 %ds 预算 —— 实链路里必然 ReadTimeout" % (worst, budget))
                print("         被 except 静默吞掉，界面看不出来，置信度因子退回自报值。")
                print("         要么 ZHIPI_CROSS_TIMEOUT 调到 %d 以上（代价：黄/红件多等这么久），" % need)
                print("         要么换更快的第二模型。")
            # 稳定性：裁判自己漂，转红判定就不可复现
            if len(set(cc_scores)) > 1:
                thr = q["max_score"] * 0.15
                print("  [注意] 同一份作答第二模型给出 %s，转红阈值 %.1f 分。" % (
                    "/".join(str(int(s)) if s == int(s) else str(s) for s in cc_scores), thr))
                if len(set(escs)) > 1:
                    print("         转红判定在多次运行间不一致（%s）——同一份作业两次演示"
                          % "/".join(str(e) for e in escs))
                    print("         会得到不同分流结果，这一点答辩上很难解释。")
                else:
                    print("         但转红判定一致，暂未影响分流。")
            else:
                print("  [稳定] 第二模型多次给分一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
