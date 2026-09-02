from __future__ import annotations

from .grading import final_error_tags


def aggregate(rows: list[dict], reviews: dict[str, dict], class_id: str) -> dict:
    distribution = {"green": 0, "yellow": 0, "red": 0}
    scores = []
    students = set()
    knowledge = {}
    tags = {}
    reviewed = 0

    for row in rows:
        result = row.get("ai_result")
        if not result:
            continue
        sid = row["submission_id"]
        review = reviews.get(sid)
        if review:
            reviewed += 1
        distribution[result.get("status", "red")] = distribution.get(
            result.get("status", "red"), 0
        ) + 1
        student_key = row.get("student_id") or row.get("student_name") or sid
        students.add(student_key)
        max_score = float(result.get("max_score") or 0)
        score = float(review["final_score"] if review else result.get("total_score") or 0)
        if max_score > 0:
            scores.append(score / max_score * 100)
        for step in result.get("step_analysis") or []:
            point = str(step.get("knowledge_point") or "").strip()
            if point:
                bucket = knowledge.setdefault(point, [0, 0])
                bucket[1] += 1
                if not step.get("is_correct"):
                    bucket[0] += 1
        for tag in final_error_tags(result, review):
            tags[tag] = tags.get(tag, 0) + 1

    graded_count = sum(distribution.values())
    weak = [
        {
            "name": name,
            "wrong": values[0],
            "total": values[1],
            "error_rate": round(values[0] / values[1] * 100, 1) if values[1] else 0,
        }
        for name, values in knowledge.items()
    ]
    weak.sort(key=lambda item: (-item["error_rate"], -item["total"], item["name"]))
    tag_rows = [{"name": name, "count": count} for name, count in tags.items()]
    tag_rows.sort(key=lambda item: (-item["count"], item["name"]))
    total = graded_count or 1

    suggestions = []
    if weak:
        suggestions.append(
            "优先讲评%s，并用一道同构题进行课末复测。"
            % "、".join(item["name"] for item in weak[:3])
        )
    if tag_rows:
        suggestions.append(
            "本次高频错因为「%s」，建议展示典型错例并让学生解释错误发生在哪一步。"
            % tag_rows[0]["name"]
        )
    if not suggestions:
        suggestions.append("当前可统计数据不足；请先完成批改或教师终审。")

    return {
        "class_id": class_id,
        "student_count": len(students),
        "result_count": graded_count,
        "reviewed_count": reviewed,
        "review_progress_pct": round(reviewed / graded_count * 100, 1) if graded_count else 0,
        "average_score_pct": round(sum(scores) / len(scores), 1) if scores else 0,
        "distribution": {
            **distribution,
            "green_pct": round(distribution["green"] / total * 100, 1),
            "yellow_pct": round(distribution["yellow"] / total * 100, 1),
            "red_pct": round(distribution["red"] / total * 100, 1),
        },
        "weak_knowledge_points": weak,
        "error_tag_distribution": tag_rows,
        "teaching_suggestions": suggestions,
    }


def student_profile(student_id: str, rows: list[dict], reviews: dict[str, dict]) -> dict | None:
    mine = [
        row
        for row in rows
        if row.get("student_id") == student_id or row.get("student_name") == student_id
    ]
    if not mine:
        return None
    tags = {}
    scores = []
    subjects = []
    for row in mine:
        result = row.get("ai_result")
        if not result:
            continue
        review = reviews.get(row["submission_id"])
        max_score = float(result.get("max_score") or 0)
        score = float(review["final_score"] if review else result.get("total_score") or 0)
        if max_score:
            scores.append(score / max_score * 100)
        if row.get("subject") and row["subject"] not in subjects:
            subjects.append(row["subject"])
        for tag in final_error_tags(result, review):
            tags[tag] = tags.get(tag, 0) + 1
    top_tags = sorted(tags.items(), key=lambda item: (-item[1], item[0]))
    return {
        "student_id": mine[0].get("student_id") or student_id,
        "student_name": mine[0].get("student_name") or student_id,
        "subjects": subjects,
        "submission_count": len(mine),
        "average_score_pct": round(sum(scores) / len(scores), 1) if scores else 0,
        "error_tag_frequency": [{"name": name, "count": count} for name, count in top_tags],
        "trend_summary": (
            "当前主要需要关注「%s」，建议按错因安排一次针对性复测。" % top_tags[0][0]
            if top_tags
            else "当前未发现集中错因，建议继续通过抽查验证掌握情况。"
        ),
    }

