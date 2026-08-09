"""班级学情聚合模块。

对应总体流程（§7.1）中的「班级学情分析」环节，产出教师看板所需数据：
- 班级平均得分率与红黄绿分流占比；
- 各知识点错误率（用于薄弱点条形图）；
- 错因标签分布（§6.11 第二层标签）；
- 规则生成的下节课讲评建议（§15.3 思路）；
- 学生长期画像（aggregate_student：跨题聚合 + 模拟历史时间线 + 趋势结论）；
- 下节课讲评课件大纲（build_lecture_outline：Markdown 课件底稿，可粘贴至希沃白板 / 飞书文档）。

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
    # 参与作答**人数**按学生去重，不能等于结果条数：整份试卷按题拆分后，
    # 一个学生一张 10 道题的卷子会产生 10 条结果，若直接用条数，看板会显示
    # 「参与作答 10 人」——班级规模凭空翻十倍，红黄绿占比的分母也跟着错。
    # 结果里没有 student_id，只能按 student_name 去重。
    # 注意：没有姓名的提交不能被静默丢弃——上传件默认姓名为「上传作业」，
    # 真正空名的情形极少，但出现时把无名提交算作 1 个匿名学生桶而不是 0。
    names = {r.get("student_name") for r in results if r.get("student_name")}
    has_unnamed = any(not r.get("student_name") for r in results)
    student_n = len(names) + (1 if has_unnamed else 0)
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
        "student_count": student_n,
        # 批改份数单独给出：整份试卷拆题后它与人数不再相等，
        # 「20 人 / 共 86 份」比只报一个数更说得清看板在统计什么。
        "result_count": n,
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


# ---------- 学生长期画像（评委审计补充：跨题 + 历史时间线） ----------

def _trend_summary(hist_records: list, current_tags: list, current_pct: float) -> str:
    """规则生成一句话趋势结论：得分走势 + 错因演变（顽固 / 进步 / 新增）。

    口径：
    - 得分走势：本次平均得分率与最近一次历史作业得分率比较，±5 个百分点为阈值；
    - 顽固错因：从最近一次历史记录向前「连续」出现、且本次仍出现的错因，
      累计（含本次）≥ 3 次即判为顽固，建议专项补救；
    - 无顽固错因时，比较最近一次历史与本次的错因集合，给出进步 / 新增结论。
    """
    if not hist_records:
        if current_tags:
            return "暂无历史数据，本次画像为基线，重点关注「%s」。" % "、".join(current_tags[:2])
        return "暂无历史数据，本次作业未出现错因，画像基线良好。"

    last = hist_records[-1]
    parts = []

    # 1. 得分走势：与最近一次历史作业比较
    delta = current_pct - float(last.get("score_pct", 0))
    if delta >= 5:
        parts.append("得分率由上次 %.0f%% 提升至本次 %.1f%%，进步明显"
                     % (last.get("score_pct", 0), current_pct))
    elif delta <= -5:
        parts.append("得分率由上次 %.0f%% 下滑至本次 %.1f%%，需要重点关注"
                     % (last.get("score_pct", 0), current_pct))
    else:
        parts.append("得分率与上次基本持平（本次 %.1f%%）" % current_pct)

    # 2. 顽固错因：向前连续出现且本次仍出现（含本次 ≥ 3 次）
    persistent = []
    for tag in current_tags:
        streak = 0
        for rec in reversed(hist_records):
            if tag in rec.get("error_tags", []):
                streak += 1
            else:
                break
        if streak + 1 >= 3:
            persistent.append((tag, streak + 1))
    if persistent:
        txt = "、".join("「%s」已连续 %d 次出现" % (t, n) for t, n in persistent[:2])
        parts.append("%s，属顽固错因，建议专项补救" % txt)
    else:
        gone = [t for t in last.get("error_tags", []) if t not in current_tags]
        if gone and not current_tags:
            parts.append("本次未出现任何错因，「%s」等历史问题已解决，基本掌握"
                         % "、".join(gone[:2]))
        elif gone:
            parts.append("较上次减少「%s」等 %d 类错因，错因结构在好转"
                         % ("、".join(gone[:2]), len(gone)))
        elif current_tags:
            parts.append("本次新增错因「%s」，建议结合评语及时订正" % "、".join(current_tags[:2]))
        else:
            parts.append("本次未出现错因，继续保持")
    return "；".join(parts) + "。"


def aggregate_student(student_id: str, student_name: str, results: list, history: dict) -> dict:
    """聚合单个学生的长期画像（学生个人看板 / 家长报告数据源）。

    参数：
        student_id:   学生 ID（用于查 history 中的历史记录）。
        student_name: 学生姓名。批改结果里没有 student_id，只能按 result 中的
                      student_name 过滤，故需调用方同时传入姓名。
        results:      本次批改结果列表（grader 输出，须已附加 student_name / subject）。
        history:      data/history.json 加载结果（模拟历史数据，含 students 映射；
                      进入 timeline 时统一标注 simulated=true 以示区分）。

    口径：错因频次与薄弱知识点均以「Rubric 步骤」为最小统计单元，与班级聚合一致；
    weak_knowledge_points 只保留判错步骤数 > 0 的知识点（真正薄弱项）。
    """
    mine = [r for r in results if r.get("student_name") == student_name]

    score_pcts = []
    tag_stat = {}      # 错因标签 -> 判错步骤人次
    kp_stat = {}       # 知识点 -> [判错步骤数, 评估步骤总数]
    current_tags = []  # 本次出现的错因（去重保序）
    subjects = []      # 本次涉及学科（去重保序）
    for r in mine:
        if r.get("max_score"):
            score_pcts.append(r["total_score"] / r["max_score"] * 100)
        subj = r.get("subject", "")
        if subj and subj not in subjects:
            subjects.append(subj)
        for s in r.get("step_analysis", []):
            kp = s["knowledge_point"]
            stat = kp_stat.setdefault(kp, [0, 0])
            stat[1] += 1
            if not s["is_correct"]:
                stat[0] += 1
            tag = s.get("error_tag")
            if tag:
                tag_stat[tag] = tag_stat.get(tag, 0) + 1
                if tag not in current_tags:
                    current_tags.append(tag)

    avg = round(sum(score_pcts) / len(score_pcts), 1) if score_pcts else 0.0

    tag_freq = [{"name": k, "count": v} for k, v in tag_stat.items()]
    tag_freq.sort(key=lambda x: (-x["count"], x["name"]))

    weak = [
        {
            "name": name,
            "wrong": v[0],
            "total": v[1],
            "error_rate": round(v[0] / v[1] * 100, 1) if v[1] else 0.0,
        }
        for name, v in kp_stat.items() if v[0] > 0
    ]
    weak.sort(key=lambda x: (-x["error_rate"], -x["total"], x["name"]))

    # 时间线 = 历史记录（模拟，simulated=true）+ 本次批改摘要（合并为一条，simulated=false）
    students = history.get("students", {}) if isinstance(history, dict) else {}
    hist_records = students.get(student_id, {}).get("history", [])
    timeline = []
    for rec in hist_records:
        item = dict(rec)
        item["error_tags"] = list(rec.get("error_tags", []))
        item["simulated"] = True
        timeline.append(item)
    timeline.append({
        "assignment": "本次作业",
        "subject": "、".join(subjects),
        "score_pct": avg,
        "error_tags": list(current_tags),
        "simulated": False,
    })

    return {
        "student_id": student_id,
        "student_name": student_name,
        "submission_count": len(mine),
        "average_score_pct": avg,
        "error_tag_freq": tag_freq,
        "weak_knowledge_points": weak,
        "timeline": timeline,
        "trend_summary": _trend_summary(hist_records, current_tags, avg),
    }


# ---------- 讲评课件大纲（评委审计补充：证据链驱动的讲评稿） ----------

# 知识点 -> 5 分钟复测同型题题面建议（规则模板，覆盖 Demo 题库涉及的知识点）
RETEST_TEMPLATES = {
    "一元二次方程": "解方程：x² - 7x + 12 = 0（要求：写明解法选择、因式分解过程、两个根与完整结论）",
    "因式分解": "分解因式并解方程：x² - x - 6 = 0（要求：写出完整因式分解过程，两根都要求出）",
    "速度公式": "一列火车匀速行驶，通过路程 s = 300 m 用时 t = 15 s，求火车速度 v（要求：先写公式 v=s/t，再代入数值，结果带单位）",
    "单位换算": "把 54 km/h 换算成 m/s，写出换算过程（要求：写明换算关系，结果带单位）",
    "英语写作": "以 “My Sunday” 为题写一篇 60 词左右短文（要求：一般过去时，包含时间 / 活动 / 感受三要素）",
    "英语时态": "把 5 个含 have / go / play 的现在时句子改写为一般过去时（要求：动词过去式全部书写正确）",
    "英语词汇": "听写本单元 10 个易错单词（含 interesting / relaxed 等），并任选 2 个造句",
    "英语句法": "用 First / Then / After that 把 3 个简单句连成一段连贯的话",
}


def _find_examples(tag_name: str, results: list, anon: dict, limit: int = 2) -> list:
    """从批改结果中找含指定错因的典型错例（证据链：匿名学生 + 原文片段 + 判定理由）。

    每个学生同一错因只取一条；evidence 缺失（如步骤缺漏无原文可引）时退化为只引理由。
    """
    out = []
    for r in results:
        for s in r.get("step_analysis", []):
            if s.get("error_tag") == tag_name:
                who = anon.get(r.get("student_name", ""), "某学生")
                ev = (s.get("evidence") or "").strip()
                reason = s.get("reason", "")
                if ev:
                    out.append("- 典型错例（%s · %s）：作答「%s」——%s"
                               % (who, r.get("subject", ""), ev, reason))
                else:
                    out.append("- 典型错例（%s · %s）：%s"
                               % (who, r.get("subject", ""), reason))
                break
        if len(out) >= limit:
            break
    return out


def build_lecture_outline(class_analytics: dict, results: list) -> str:
    """生成下节课讲评课件大纲（Markdown 字符串）。

    产出为纯 Markdown 文本，可一键复制为课件底稿粘贴至希沃白板、飞书文档
    等备课环境（飞书文档粘贴自动识别标题层级；备课时可按一、二级标题拆分为页）。

    参数：
        class_analytics: aggregate() 的输出（可含 class_name）。
        results:         本次批改结果列表（grader 输出，须已附加 student_name / subject；
                         step_analysis 中的 evidence 字段用于挂典型错例证据链）。

    结构：
    一、共性问题讲评：错因分布前 3，每个错因挂 1-2 条典型错例
        （匿名学生 + 作答原文证据片段 + 判定理由）；
    二、薄弱知识点巩固：错误率前 3 的知识点；
    三、分层任务：红黄绿三层人数与各自任务；
    四、5 分钟复测建议：针对最薄弱知识点给出同型题题面建议。
    """
    class_name = class_analytics.get("class_name") or class_analytics.get("class_id", "")
    avg = class_analytics.get("average_score_pct", 0)
    dist = class_analytics.get("distribution", {})
    weak = class_analytics.get("weak_knowledge_points", [])
    tags = class_analytics.get("error_tag_distribution", [])

    # 学生匿名映射：按结果出现顺序编号为 学生A / 学生B / …（讲评时不点名）
    anon = {}
    for r in results:
        name = r.get("student_name", "")
        if name and name not in anon:
            idx = len(anon)
            anon[name] = ("学生%s" % chr(ord("A") + idx)) if idx < 26 else ("学生%d" % (idx + 1))

    lines = []
    lines.append("# 讲评课件大纲 · %s" % class_name)
    lines.append("")
    lines.append("> 本次作业平均得分率 %s%%，批改 %d 份作答。"
                 "本大纲由智批π根据批改证据链自动生成，可复制为课件底稿粘贴至希沃白板 / 飞书文档。"
                 % (avg, len(results)))
    lines.append("")
    lines.append("## 一、共性问题讲评")
    if tags:
        for i, tag in enumerate(tags[:3], 1):
            lines.append("")
            lines.append("### %d. 错因「%s」（%d 人次）" % (i, tag["name"], tag["count"]))
            examples = _find_examples(tag["name"], results, anon, limit=2)
            lines.extend(examples or ["- （本次无可引用的典型错例）"])
    else:
        lines.append("- 本次作业未出现集中错因，可做整体表扬与个别订正。")
    lines.append("")
    lines.append("## 二、薄弱知识点巩固")
    if weak:
        for w in weak[:3]:
            lines.append("- %s：错误率 %.0f%%（%d/%d 步判错），安排 1 道基础题 + 1 道变式题当堂巩固"
                         % (w["name"], w["error_rate"], w["wrong"], w["total"]))
    else:
        lines.append("- 本次无明显薄弱知识点。")
    lines.append("")
    lines.append("## 三、分层任务")
    lines.append("- 红色 · 人工批改（%d 份）：教师面批订正，重做错题并口述解题思路"
                 % dist.get("red", 0))
    lines.append("- 黄色 · 教师确认（%d 份）：完成错因对应的针对性练习，教师复核订正结果"
                 % dist.get("yellow", 0))
    lines.append("- 绿色 · 自动通过（%d 份）：完成 1 道拓展提升题，可担任小组讲解员帮扶同伴"
                 % dist.get("green", 0))
    lines.append("")
    lines.append("## 四、5 分钟复测建议")
    if weak:
        kp = weak[0]["name"]
        lines.append("针对最薄弱知识点「%s」出 1 道同型题当堂限时复测：" % kp)
        lines.append("")
        lines.append("> %s" % RETEST_TEMPLATES.get(
            kp, "围绕「%s」出 1 道与本次作业同型的基础题，当堂限时 5 分钟完成。" % kp))
    else:
        lines.append("本次无明显薄弱知识点，可安排综合小测巩固。")
    return "\n".join(lines) + "\n"
