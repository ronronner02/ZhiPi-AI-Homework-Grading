"""飞书集成连通性自检：凭据 → 鉴权 → 字段比对 → 真实推送 / 写表 → 回读 → 清理。

为什么要有这个脚本：飞书两个集成点（互动卡片、多维表格台账）在异常时都**故意**
降级 demo 模式，界面照常出图。这是为了保证现场不中断，但副作用是——凭据配错了
也看不出来，界面长得一模一样。答辩前必须有一条能把"到底是真的还是演示的"问死的路径。

分层设计：读操作默认执行，写操作（发群消息、写表）必须显式开关。
公开演示前误发一屋子人的群，或者往真实台账里灌测试数据，都不该是默认行为。

用法：
    py -3 tools/check_feishu.py                   # 只读自检（鉴权 + 字段比对）
    py -3 tools/check_feishu.py --push            # 额外真发一张卡片到群
    py -3 tools/check_feishu.py --write           # 额外真写记录，写完回读校验
    py -3 tools/check_feishu.py --write --cleanup # 写入并回读后删除，表里不留痕
    py -3 tools/check_feishu.py --all             # --push --write --cleanup

退出码 0 表示全部通过，1 表示有失败项。
"""
import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Windows 控制台默认 GBK，飞书返回的中文报错会直接抛 UnicodeEncodeError，
# 把真正的失败原因盖成一个编码异常。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import os

import requests

# 刻意 import app 而不是自己再写一遍 .env 加载：要测的正是"应用自己能不能看到这些
# 凭据"。复制一份加载逻辑，就等于测了个平行宇宙里的实现。
import app as demo_app  # noqa: E402  （副作用：按 app.py 的规则加载 .env）
from pipeline import feishu  # noqa: E402

# 多维表格字段类型码 → 可读名（只列本模块会碰到的）
FIELD_TYPE = {
    1: "多行文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期",
    7: "复选框", 11: "人员", 13: "电话号码", 15: "超链接",
    17: "附件", 18: "单向关联", 19: "查找引用", 20: "公式",
    21: "双向关联", 22: "地理位置", 23: "群组",
    1001: "创建时间", 1002: "最后更新时间", 1003: "创建人", 1004: "修改人",
    1005: "自动编号",
}

# build_base_records 写入的 8 个字段 → 期望的字段类型码集合。
# 数字列写字符串、或文本列写数字，飞书都会直接拒绝整批，而不是只跳过这一格。
EXPECTED = {
    "学生":     {1, 3},
    "题号":     {1, 3},
    "得分":     {2},
    "满分":     {2},
    "错因标签": {1, 3},
    "置信度":   {2},
    "分流状态": {1, 3},
    # 教师终审：文本或单选均可。单选时 build_base_records 只写短状态
    # （待终审 / 通过 / 驳回），与现场表选项对齐。
    "教师终审": {1, 3},
}

FAILURES = []
WARNINGS = []


def head(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def ok(msg):
    print("  [ OK ] " + msg)


def bad(msg):
    print("  [FAIL] " + msg)
    FAILURES.append(msg)


def warn(msg):
    print("  [WARN] " + msg)
    WARNINGS.append(msg)


def info(msg):
    print("         " + msg)


def mask(value):
    """凭据只报长度与首尾，绝不整串打印——这份输出会被贴进群里和文档里。"""
    if not value:
        return "(未设置)"
    if len(value) <= 8:
        return "(已设置 %d 字符)" % len(value)
    return "%s…%s (%d 字符)" % (value[:4], value[-2:], len(value))


# ---------------------------------------------------------------- 步骤 1
def step_env():
    head("1 · 凭据加载")
    src = getattr(demo_app, "_DOTENV_SOURCE", None)
    if src:
        ok(".env 来源：%s" % src)
    else:
        warn("未从 .env 读取（ZHIPI_SKIP_DOTENV 已开启，或文件不存在），仅用进程环境变量")

    keys = {
        "ZHIPI_FEISHU_WEBHOOK": "自定义机器人 Webhook（集成点二）",
        "ZHIPI_FEISHU_APP_ID": "自建应用 App ID（集成点一）",
        "ZHIPI_FEISHU_APP_SECRET": "自建应用 App Secret（集成点一）",
        "ZHIPI_FEISHU_BASE_APP_TOKEN": "多维表格 App Token（集成点一）",
        "ZHIPI_FEISHU_TABLE_ID": "数据表 table_id（集成点一）",
        "ZHIPI_REVIEW_CONSOLE_URL": "卡片按钮跳转地址（可选）",
    }
    for key, desc in keys.items():
        val = os.environ.get(key, "").strip()
        if val:
            ok("%-28s %s  %s" % (key, mask(val), desc))
        elif key == "ZHIPI_REVIEW_CONSOLE_URL":
            info("%-28s 未设置，将用占位地址 %s" % (key, feishu.DEFAULT_CONSOLE_URL))
        else:
            bad("%s 未设置 —— %s 将降级 demo" % (key, desc))

    # Webhook 形态校验：把 App ID 之类误填进 Webhook 是很常见的手滑，
    # 而错误的 URL 会被 push_review_card 的 except 吞掉降级 demo。
    hook = os.environ.get("ZHIPI_FEISHU_WEBHOOK", "").strip()
    if hook and "open.feishu.cn/open-apis/bot/v2/hook/" not in hook:
        warn("Webhook 不像自定义机器人地址（应含 /open-apis/bot/v2/hook/）")
    return not FAILURES


# ---------------------------------------------------------------- 步骤 2
def step_token():
    head("2 · 自建应用鉴权（tenant_access_token）")
    app_id = os.environ.get("ZHIPI_FEISHU_APP_ID", "").strip()
    secret = os.environ.get("ZHIPI_FEISHU_APP_SECRET", "").strip()
    if not (app_id and secret):
        bad("缺 App ID / App Secret，跳过鉴权")
        return None
    try:
        token = feishu._tenant_access_token(app_id, secret)
    except Exception as exc:
        bad("鉴权失败：%s" % exc)
        info("对照：10003=app_id 不存在  10014=app_secret 错误  99991663=应用被停用")
        return None
    ok("已换取 tenant_access_token %s" % mask(token))
    return token


# ---------------------------------------------------------------- 步骤 3
def step_fields(token):
    """列字段并与 build_base_records 的 8 个字段逐个比对。

    这一步是整个脚本最有价值的部分：凭据全对、鉴权也过，但表里少一列或者
    「得分」被建成了文本列，batch_create 会整批失败，而 sync_to_base 会把它
    降级成 demo —— 界面上和"没配凭据"完全一样。
    """
    head("3 · 多维表格字段比对")
    if not token:
        bad("无 token，跳过字段比对")
        return None
    app_token = os.environ.get("ZHIPI_FEISHU_BASE_APP_TOKEN", "").strip()
    table_id = os.environ.get("ZHIPI_FEISHU_TABLE_ID", "").strip()
    if not (app_token and table_id):
        bad("缺 BASE_APP_TOKEN / TABLE_ID，跳过字段比对")
        return None

    url = "%s/open-apis/bitable/v1/apps/%s/tables/%s/fields" % (
        feishu.FEISHU_OPEN_HOST, app_token, table_id)
    try:
        resp = requests.get(url, headers={"Authorization": "Bearer " + token},
                            params={"page_size": 100}, timeout=15)
        body = resp.json()
    except Exception as exc:
        bad("字段列表请求失败：%s" % exc)
        return None

    if body.get("code") != 0:
        bad("字段列表返回错误 code=%s msg=%s" % (body.get("code"), body.get("msg")))
        code = body.get("code")
        if code == 91402:
            info("91402 NOTEXIST：app_token 或 table_id 不对（app_token 是 Base 的，不是文档的）")
        elif code in (1254005, 1254302, 99991672):
            info("权限问题：应用需开通 bitable:app 权限，且该 Base 要在「协作者」里加上这个应用")
        return None

    items = (body.get("data") or {}).get("items") or []
    actual = {}
    for it in items:
        actual[it.get("field_name")] = it.get("type")
    ok("表内共 %d 个字段：%s" % (len(actual), "、".join(actual.keys()) or "(空)"))

    all_good = True
    for name, want in EXPECTED.items():
        got = actual.get(name)
        if got is None:
            bad("缺字段「%s」—— 写入会整批失败并静默降级 demo" % name)
            all_good = False
        elif got not in want:
            bad("字段「%s」类型是 %s，需要 %s" % (
                name, FIELD_TYPE.get(got, got),
                " 或 ".join(FIELD_TYPE.get(w, str(w)) for w in sorted(want))))
            all_good = False
        else:
            ok("字段「%s」类型 %s，匹配" % (name, FIELD_TYPE.get(got, got)))

    extra = [n for n in actual if n not in EXPECTED]
    if extra:
        info("表内额外字段（不影响写入）：%s" % "、".join(extra))
    return all_good


# ---------------------------------------------------------------- 步骤 4
def sample_analytics():
    """取应用内真实的班级聚合结果，而不是手搓一个假 dict。

    手搓 dict 能让脚本跑通，但测不出"卡片渲染碰上真实数据会不会出问题"。
    """
    session = {"reviews": {}, "uploads": []}
    return demo_app._class_analytics(session)


def step_push(do_push):
    head("4 · 集成点二 · 互动卡片")
    analytics_data = sample_analytics()
    card = feishu.build_review_card(analytics_data, "自检 · 飞书连通性测试")
    ok("卡片构造成功，%d 个元素" % len(card["card"]["elements"]))
    info("标题：%s" % card["card"]["header"]["title"]["content"])

    if not do_push:
        info("未加 --push，跳过真实推送（会真往群里发消息）")
        return None

    result = feishu.push_review_card(analytics_data, "自检 · 飞书连通性测试")
    if result.get("mode") == "live":
        ok("真实推送成功，HTTP %s，飞书返回 %s" % (
            result.get("status_code"), json.dumps(result.get("response"), ensure_ascii=False)))
        return True
    bad("推送未走真实链路：%s" % result.get("message"))
    msg = str(result.get("message", ""))
    if "sign" in msg.lower() or "19021" in msg:
        info("19021 sign match fail：机器人开了签名校验，但本模块没实现签名")
    if "key words" in msg.lower() or "19024" in msg:
        info("19024：机器人设了关键词白名单，卡片文案里必须含该关键词")
    return False


# ---------------------------------------------------------------- 步骤 5
def step_write(token, do_write, do_cleanup):
    head("5 · 集成点一 · 多维表格写入")
    session = {"reviews": {}, "uploads": []}
    results = demo_app.session_results(session)
    records = feishu.build_base_records(results, session["reviews"])
    ok("记录构造成功，%d 条" % len(records))
    if records:
        info("首条：%s" % json.dumps(records[0]["fields"], ensure_ascii=False))

    if not do_write:
        info("未加 --write，跳过真实写入（会往真实台账里加行）")
        return None

    result = feishu.sync_to_base(results, session["reviews"])
    if result.get("mode") != "live":
        bad("写入未走真实链路：%s" % result.get("message"))
        return False
    ok("真实写入成功，%d 条记录" % result.get("record_count"))

    created = (((result.get("response") or {}).get("data")) or {}).get("records") or []
    rec_ids = [r.get("record_id") for r in created if r.get("record_id")]
    if not rec_ids:
        warn("返回里没有 record_id，无法回读校验")
        return True

    # 回读：写成功不等于写对了。飞书对类型不符的值有时会静默截断而非报错。
    app_token = os.environ.get("ZHIPI_FEISHU_BASE_APP_TOKEN", "").strip()
    table_id = os.environ.get("ZHIPI_FEISHU_TABLE_ID", "").strip()
    base = "%s/open-apis/bitable/v1/apps/%s/tables/%s/records" % (
        feishu.FEISHU_OPEN_HOST, app_token, table_id)
    hdr = {"Authorization": "Bearer " + token}
    try:
        rr = requests.get("%s/%s" % (base, rec_ids[0]), headers=hdr, timeout=15).json()
        if rr.get("code") == 0:
            got = ((rr.get("data") or {}).get("record") or {}).get("fields") or {}
            want = records[0]["fields"]
            drift = [k for k in want
                     if str(_plain(got.get(k))) != str(want[k])]
            if drift:
                warn("回读字段与写入值不一致：%s" % "、".join(drift))
                for k in drift:
                    info("  %s：写入 %r → 回读 %r" % (k, want[k], _plain(got.get(k))))
            else:
                ok("回读校验通过，8 个字段值与写入一致")
        else:
            warn("回读失败 code=%s msg=%s" % (rr.get("code"), rr.get("msg")))
    except Exception as exc:
        warn("回读异常：%s" % exc)

    if do_cleanup:
        try:
            dr = requests.post("%s/batch_delete" % base, headers=hdr,
                               json={"records": rec_ids}, timeout=15).json()
            if dr.get("code") == 0:
                ok("已清理本次写入的 %d 条测试记录，表内不留痕" % len(rec_ids))
            else:
                warn("清理失败 code=%s msg=%s，需手动删除 %d 行" % (
                    dr.get("code"), dr.get("msg"), len(rec_ids)))
        except Exception as exc:
            warn("清理异常：%s，需手动删除 %d 行" % (exc, len(rec_ids)))
    else:
        info("未加 --cleanup，%d 条测试记录保留在表里，可去飞书里肉眼确认" % len(rec_ids))
        info("record_id 首条：%s" % rec_ids[0])
    return True


def _plain(value):
    """多维表格回读时，文本字段可能是 [{'type':'text','text':'张明'}] 这种富文本结构。"""
    if isinstance(value, list):
        return "".join(v.get("text", "") if isinstance(v, dict) else str(v) for v in value)
    if isinstance(value, dict):
        return value.get("text", json.dumps(value, ensure_ascii=False))
    return value


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="飞书集成连通性自检")
    ap.add_argument("--push", action="store_true", help="真实推送卡片到群（会发消息）")
    ap.add_argument("--write", action="store_true", help="真实写入多维表格（会加行）")
    ap.add_argument("--cleanup", action="store_true", help="写入并回读后删除测试记录")
    ap.add_argument("--all", action="store_true", help="等价于 --push --write --cleanup")
    args = ap.parse_args()
    if args.all:
        args.push = args.write = args.cleanup = True

    step_env()
    token = step_token()
    step_fields(token)
    step_push(args.push)
    step_write(token, args.write, args.cleanup)

    head("自检结论")
    if FAILURES:
        print("  失败 %d 项：" % len(FAILURES))
        for f in FAILURES:
            print("    - " + f)
    if WARNINGS:
        print("  警告 %d 项：" % len(WARNINGS))
        for w in WARNINGS:
            print("    - " + w)
    if not FAILURES and not WARNINGS:
        print("  全部通过。")
    elif not FAILURES:
        print("  无失败项，但有警告，建议逐条看过。")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
