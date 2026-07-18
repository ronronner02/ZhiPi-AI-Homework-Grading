"""飞书协同集成模块。

对应详细设计方案 §13.2《飞书 AI 产品能力集成设计》，在"批改在系统、协同在飞书"
原则下，落地两个可现场演示的集成点：

- 集成点二（机器人互动卡片审核流转）：build_review_card / push_review_card
  AI 批改完成后，向任课教师推送一张飞书互动卡片，含作业名、红黄绿分流统计、
  待教师处理数量与"前往审核工作台"按钮。
- 集成点一（多维表格 AI 学情台账，主用飞书 AI 能力）：build_base_records / sync_to_base
  将逐题批改结果写入班级《学情台账》多维表格，供其 AI 字段捷径逐行自动生成
  一句话错因摘要与个性化学习建议。

演示优先：未配置飞书凭据时，两个入口均返回 demo 模式数据，展示"将要推送的卡片
内容"与"将要写入的表格记录"，保证无网络、无凭据也能现场演示（比赛规则第三条）；
配置凭据后自动切换真实推送 / 写表，任何异常自动降级 demo，保证 Demo 始终可跑。

本模块不硬编码任何真实凭据或 Webhook 地址，全部经环境变量注入。
"""
import os

import requests

# 飞书开放平台域名与端点（公有云）
FEISHU_OPEN_HOST = "https://open.feishu.cn"
# 自建应用鉴权：换取 tenant_access_token
TOKEN_URL = FEISHU_OPEN_HOST + "/open-apis/auth/v3/tenant_access_token/internal"
# 多维表格记录批量新增端点模板：
#   /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create

# 审核工作台跳转链接（占位）。真实部署时经 ZHIPI_REVIEW_CONSOLE_URL 注入本校地址。
DEFAULT_CONSOLE_URL = "https://zhipi.example.com/teacher/workbench"

# 分流状态中文标签（与 confidence.STATUS_LABEL 对齐）
STATUS_LABEL = {
    "green": "绿色 · 自动通过",
    "yellow": "黄色 · 教师确认",
    "red": "红色 · 人工批改",
}
# 分流状态对应的信号灯，用于卡片富文本
STATUS_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _console_url() -> str:
    """审核工作台跳转链接：优先取环境变量，否则用占位地址。"""
    return os.environ.get("ZHIPI_REVIEW_CONSOLE_URL", DEFAULT_CONSOLE_URL).strip() or DEFAULT_CONSOLE_URL


# ---------- 集成点二：机器人互动卡片审核流转 ----------

def build_review_card(class_analytics: dict, assignment_name: str = None) -> dict:
    """构造飞书互动消息卡片（msg_type=interactive）。

    对应设计方案 §13.2 集成点二：AI 批改完成后，飞书机器人向任课教师推送互动卡片，
    含作业名、红黄绿分流统计、待教师处理数量、"前往审核工作台"按钮（占位链接）。

    参数：
        class_analytics: analytics.aggregate() 的班级聚合结果（含 distribution、
                         student_count、average_score_pct、error_tag_distribution 等）。
        assignment_name: 作业名，缺省时用班级名拼装。
    返回：
        可直接投递飞书自定义机器人 Webhook 的卡片消息 JSON。
    """
    dist = class_analytics.get("distribution", {})
    green = dist.get("green", 0)
    yellow = dist.get("yellow", 0)
    red = dist.get("red", 0)
    # 待教师处理 = 黄色（需确认）+ 红色（需人工批改）；绿色为高置信度自动通过
    pending = yellow + red

    class_name = class_analytics.get("class_name") or class_analytics.get("class_id", "本班")
    if not assignment_name:
        assignment_name = "%s · 本次作业批改" % class_name

    student_count = class_analytics.get("student_count", 0)
    avg = class_analytics.get("average_score_pct", 0)

    # 概览正文（lark_md 富文本）
    summary = (
        "**作业：** %s\n"
        "**参与作答：** %d 人 ·  **平均得分率：** %s%%"
    ) % (assignment_name, student_count, avg)

    # 红黄绿分流统计
    dist_lines = (
        "**红黄绿分流统计**\n"
        "%s 绿色（自动通过）：**%d** 人\n"
        "%s 黄色（教师确认）：**%d** 人\n"
        "%s 红色（人工批改）：**%d** 人"
    ) % (STATUS_EMOJI["green"], green, STATUS_EMOJI["yellow"], yellow,
         STATUS_EMOJI["red"], red)

    # 待教师处理数量（醒目提示）
    pending_line = "⏳ **待教师处理：%d 人**（黄色确认 %d + 红色人工 %d）" % (pending, yellow, red)

    # 主要错因（取错因分布前三，供教师快速了解讲评重点）
    tags = class_analytics.get("error_tag_distribution", [])
    if tags:
        top = "、".join("%s（%d 次）" % (t["name"], t["count"]) for t in tags[:3])
        tag_line = "**主要错因：** " + top
    else:
        tag_line = "**主要错因：** 暂无"

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": dist_lines}},
        {"tag": "div", "text": {"tag": "lark_md", "content": pending_line}},
        {"tag": "div", "text": {"tag": "lark_md", "content": tag_line}},
        {"tag": "hr"},
        # "前往审核工作台"按钮（占位链接，真实部署经 ZHIPI_REVIEW_CONSOLE_URL 注入）
        {"tag": "action", "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "前往审核工作台"},
                "url": _console_url(),
                "type": "primary",
            }
        ]},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": "智批π · 教师可控 AI 批改 | 数据来源：班级学情聚合（设计方案 §13.2 集成点二）"}
        ]},
    ]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 作业批改完成 · 待审核提醒"},
            "template": "blue",
        },
        "elements": elements,
    }
    return {"msg_type": "interactive", "card": card}


def push_review_card(class_analytics: dict, assignment_name: str = None) -> dict:
    """推送审核提醒卡片到飞书自定义机器人。

    读取环境变量 ZHIPI_FEISHU_WEBHOOK（自定义机器人 Webhook 地址）：
    - 已配置：用 requests POST 真实推送，返回飞书响应；
    - 未配置：返回 demo 模式数据，展示"将要推送的卡片内容"。
    任何网络 / 接口异常自动降级 demo，保证 Demo 现场始终可跑。

    参数与 build_review_card 一致；返回值统一带 mode 字段（demo / live）。
    """
    card_msg = build_review_card(class_analytics, assignment_name)
    webhook = os.environ.get("ZHIPI_FEISHU_WEBHOOK", "").strip()

    # 未配置 Webhook：返回演示模式，展示将推送的卡片内容
    if not webhook:
        return {
            "mode": "demo",
            "message": "未配置飞书 Webhook，以下为将推送的卡片内容",
            "card": card_msg,
        }

    # 已配置：真实推送到飞书自定义机器人
    try:
        resp = requests.post(webhook, json=card_msg, timeout=10)
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        return {
            "mode": "live",
            "message": "已推送至飞书自定义机器人",
            "status_code": resp.status_code,
            "response": body,
            "card": card_msg,
        }
    except Exception as exc:  # 网络 / 接口异常：降级 demo，保证 Demo 不中断
        return {
            "mode": "demo",
            "message": "飞书推送失败，已降级为演示模式：%s" % exc,
            "card": card_msg,
        }


# ---------- 集成点一：多维表格 AI 学情台账（主用飞书 AI 能力）----------

def build_base_records(grading_results: list, reviews: dict = None) -> list:
    """把逐题批改结果组装为飞书多维表格记录列表。

    对应设计方案 §13.2 集成点一（多维表格 AI 学情台账，主用飞书 AI 能力）。
    每条记录含 8 个字段：学生、题号、得分、满分、错因标签、置信度、分流状态、教师终审。

    多维表格中可对这些字段配置 AI 字段捷径，自动生成一句话错因摘要与个性化学习建议
    （对应设计方案 §13.2 集成点一）：
      · 「一句话错因摘要」AI 字段：以【错因标签】+【分流状态】为输入，
        一句话概括本题主要问题；
      · 「个性化学习建议」AI 字段：以【错因标签】+【得分/满分】为输入，
        给出一条可执行的补救建议。
    如此错因摘要与学习建议由飞书 AI 能力在表格内逐行生成，无需系统侧再调用大模型，
    正是"批改在系统、学情沉淀与 AI 加工在飞书"的落地。

    参数：
        grading_results: 批改结果列表（app.grade_all() 输出，含学生与题目信息）。
        reviews: 可选，submission_id -> 教师审核记录（app.REVIEWS）；用于填「教师终审」。
    返回：
        飞书多维表格 batch_create 所需的记录列表：[{"fields": {...}}, ...]。
    """
    reviews = reviews or {}
    records = []
    for r in grading_results:
        # 错因标签：多个标签用顿号连接，空则记"无"
        tags = r.get("error_tags") or []
        tag_text = "、".join(tags) if tags else "无"

        # 教师终审：已审核则展示动作与终分，否则标记"待终审"
        review = reviews.get(r.get("submission_id"))
        if review:
            action_label = "已修改" if review.get("teacher_action") == "modified" else "已确认"
            final = "%s %s/%s" % (action_label, review.get("final_score"), r.get("max_score"))
        else:
            final = "待终审"

        # 题号：题目 ID 与标题组合，便于台账内快速辨识题目
        question_no = "%s · %s" % (r.get("question_id", ""), r.get("question_title", ""))

        records.append({
            "fields": {
                "学生": r.get("student_name", ""),
                "题号": question_no,
                "得分": r.get("total_score", 0),
                "满分": r.get("max_score", 0),
                "错因标签": tag_text,
                "置信度": r.get("confidence", 0),
                "分流状态": STATUS_LABEL.get(r.get("status"), r.get("status", "")),
                "教师终审": final,
            }
        })
    return records


def _tenant_access_token(app_id: str, app_secret: str) -> str:
    """走飞书开放平台自建应用鉴权，换取 tenant_access_token（有效期约 2 小时）。"""
    resp = requests.post(
        TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError("获取 tenant_access_token 失败：%s" % data.get("msg"))
    return data["tenant_access_token"]


def sync_to_base(grading_results: list, reviews: dict = None) -> dict:
    """同步批改结果到飞书多维表格《学情台账》（设计方案 §13.2 集成点一）。

    读取环境变量：
        ZHIPI_FEISHU_APP_ID          自建应用 App ID
        ZHIPI_FEISHU_APP_SECRET      自建应用 App Secret
        ZHIPI_FEISHU_BASE_APP_TOKEN  多维表格 App Token（Base 的 app_token）
        ZHIPI_FEISHU_TABLE_ID        目标数据表 table_id
    四者齐备：走开放平台鉴权取 tenant_access_token，再调 batch_create 批量写入记录；
    任一缺失或任何异常（鉴权失败、频控、写表报错等）：自动降级 demo，
    返回"将要写入的记录"，保证 Demo 现场始终可跑。

    返回值统一带 mode 字段（demo / live）。
    """
    records = build_base_records(grading_results, reviews)

    app_id = os.environ.get("ZHIPI_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("ZHIPI_FEISHU_APP_SECRET", "").strip()
    app_token = os.environ.get("ZHIPI_FEISHU_BASE_APP_TOKEN", "").strip()
    table_id = os.environ.get("ZHIPI_FEISHU_TABLE_ID", "").strip()

    # 凭据不全：返回演示模式，展示将写入《学情台账》的记录
    if not (app_id and app_secret and app_token and table_id):
        return {
            "mode": "demo",
            "message": "未配置飞书多维表格凭据，以下为将写入《学情台账》的记录",
            "record_count": len(records),
            "records": records,
        }

    # 凭据齐备：走真实鉴权 + batch_create 写表
    try:
        token = _tenant_access_token(app_id, app_secret)
        url = "%s/open-apis/bitable/v1/apps/%s/tables/%s/records/batch_create" % (
            FEISHU_OPEN_HOST, app_token, table_id)
        resp = requests.post(
            url,
            json={"records": records},
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError("batch_create 返回错误：%s" % body.get("msg"))
        return {
            "mode": "live",
            "message": "已写入飞书多维表格《学情台账》",
            "record_count": len(records),
            "status_code": resp.status_code,
            "response": body,
        }
    except Exception as exc:  # 鉴权 / 写表异常：降级 demo，保证 Demo 不中断
        return {
            "mode": "demo",
            "message": "飞书写表失败，已降级为演示模式：%s" % exc,
            "record_count": len(records),
            "records": records,
        }
