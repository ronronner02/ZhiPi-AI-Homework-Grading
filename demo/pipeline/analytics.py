"""班级学情聚合模块。

对应总体流程（§7.1）中的「班级学情分析」环节，产出教师看板所需数据：
- 班级平均得分率与红黄绿分流占比；
- 各知识点错误率（用于薄弱点条形图）；
- 错因标签分布（§6.11 第二层标签）；
- 规则生成的下节课讲评建议（§15.3 思路）。

聚合口径：遍历每份作答的每个 Rubric 步骤，
以「步骤是否判错」为最小统计单元，天然反映过程级批改的诊断粒度。
"""


def _build_suggestions(weak: list, tags: list, dist: dict) -> list:
    """按规则生成讲评建议（§15.3）：薄弱知识点 + 主要错因 + 讲评顺序 + 分层辅导。"""
    if not weak:
        return ["暂无足够数据生成讲评建议。"]

    top = weak[:3]
    kp_txt = "、".join("%s（错误率 %.0f%%）" % (w["name"], w["error_rate"]) for w in top)
    suggestions = ["本次作业班级最薄弱的知识点为：%s。" % kp_txt]

    if tags:
        suggestions.append(
            "最集中的错因是「%s」（%d 人次），建议结合典型错例集中讲评。"
            % (tags[0]["name"], tags[0]["count"])
        )

    suggestions.append(
        "讲评顺序建议：先讲「%s」，再逐一突破其余薄弱点，课末安排 5 分钟复测。"
        % top[0]["name"]
    )
    suggestions.append(
        "分层辅导：红色 %d 人优先人工面批，黄色 %d 人教师确认后补充针对性练习，绿色 %d 人可自主订正。"
        % (dist["red"], dist["yellow"], dist["green"])
    )
    return suggestions


def aggregate(class_id: str, results: list) -> dict:
    """聚合一批批改结果，返回班级看板数据。

    参数：
        class_id: 班级 ID。
        results:  批改结果列表（grader.grade 的输出）。
    """
    n = len(results)
    dist = {"green": 0, "yellow": 0, "red": 0}
    score_pcts = []
    kp_stat = {}   # 知识点 -> [判错步骤数, 评估步骤总数]
    tag_stat = {}  # 错因标签 -> 出现人次

    for r in results:
        dist[r["status"]] = dist.get(r["status"], 0) + 1
        if r["max_score"]:
            score_pcts.append(r["total_score"] / r["max_score"] * 100)
        for s in r["step_analysis"]:
            kp = s["knowledge_point"]
            stat = kp_stat.setdefault(kp, [0, 0])
            stat[1] += 1
            if not s["is_correct"]:
                stat[0] += 1
            if s.get("error_tag"):
                tag_stat[s["error_tag"]] = tag_stat.get(s["error_tag"], 0) + 1

    # 知识点错误率，按错误率降序（并列时按评估次数、名称排序）
    weak = [
        {
            "name": name,
            "wrong": v[0],
            "total": v[1],
            "error_rate": round(v[0] / v[1] * 100, 1) if v[1] else 0.0,
        }
        for name, v in kp_stat.items()
    ]
    weak.sort(key=lambda x: (-x["error_rate"], -x["total"], x["name"]))

    # 错因分布，按人次降序
    tag_list = [{"name": k, "count": v} for k, v in tag_stat.items()]
    tag_list.sort(key=lambda x: (-x["count"], x["name"]))

    avg = round(sum(score_pcts) / len(score_pcts), 1) if score_pcts else 0.0

    return {
        "class_id": class_id,
        "student_count": n,
        "average_score_pct": avg,
        "distribution": {
            "green": dist["green"],
            "yellow": dist["yellow"],
            "red": dist["red"],
            "green_pct": round(dist["green"] / n * 100, 1) if n else 0.0,
            "yellow_pct": round(dist["yellow"] / n * 100, 1) if n else 0.0,
            "red_pct": round(dist["red"] / n * 100, 1) if n else 0.0,
        },
        "weak_knowledge_points": weak,
        "error_tag_distribution": tag_list,
        "teaching_suggestions": _build_suggestions(weak, tag_list, dist),
    }
