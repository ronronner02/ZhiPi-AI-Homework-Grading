# -*- coding: utf-8 -*-
"""eval_harness 关键公式的单元自检。

为什么要单独有这个文件：`--selftest` 验证的是「全流程跑得通」，
但它用的是内置模拟数据，某些分支（比如漏拦 miss）恰好取不到样本。
指标算错会直接写进答辩材料，所以每条公式的分支都要被显式构造覆盖，
不能靠模拟数据碰运气。

运行：py -3 tools/test_eval_harness.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_harness import _norm_math, compute_cer, compute_safety, _is_retriable

fails = []


def ck(name, got, want):
    """相等断言。"""
    if got != want:
        fails.append("%s\n    期望 %r\n    实际 %r" % (name, want, got))


def ok(name, cond, detail=""):
    """真值断言。与 ck 分开，避免把「条件」误传成「期望值」。"""
    if not cond:
        fails.append("%s%s" % (name, ("\n    " + detail) if detail else ""))


def rec(item_id, needs_review, status):
    """构造一条最小记录，只含 compute_safety 关心的字段。"""
    return {"item_id": item_id, "needs_review": needs_review,
            "ai_flagged": status in ("yellow", "red"), "status": status}


# ---------------------------------------------------------------- 分流安全性四象限
recs = [
    rec("A", True,  "red"),      # hit
    rec("B", True,  "yellow"),   # hit
    rec("C", True,  "green"),    # miss ← 最严重
    rec("D", False, "yellow"),   # false_alarm
    rec("E", False, "green"),    # pass_ok
    rec("F", False, "green"),    # pass_ok
]
sf = compute_safety(recs)
ck("四象限计数", sf["counts"],
   {"hit": 2, "miss": 1, "false_alarm": 1, "pass_ok": 2})
ck("该看的总数 = hit+miss", sf["need_review"], 3)
# 关键：分母是 3（该看的），不是 6（全体）。若用全体会得 0.1667，好看但失真
ck("漏拦率 = 1/3", sf["miss_rate"], round(1 / 3, 4))
ck("召回 = 2/3", sf["recall"], round(2 / 3, 4))
ck("误拦率 = 1/3（分母是不必看的 3 份）", sf["false_alarm_rate"], round(1 / 3, 4))
ck("漏拦条目被点名", sf["missed_items"], ["C"])

# 黄和红都算「送到教师面前」
ck("红算拦住", compute_safety([rec("X", True, "red")])["counts"]["hit"], 1)
ck("黄算拦住", compute_safety([rec("X", True, "yellow")])["counts"]["hit"], 1)
ck("绿算漏拦", compute_safety([rec("X", True, "green")])["counts"]["miss"], 1)

# 未标注 needs_review 时必须跳过，而不是当成 False 静默算出漂亮数字
no_flag = compute_safety([{"item_id": "A", "status": "green"}])
ck("无标注 → 跳过", no_flag["evaluated"], False)
ck("无标注 → n=0", no_flag["n"], 0)

# 全部不需复核时，漏拦率无定义（分母 0），不能返回 0.0 假装完美
allsafe = compute_safety([rec("A", False, "green"), rec("B", False, "green")])
ck("无人需复核 → 漏拦率 None", allsafe["miss_rate"], None)
ck("无人需复核 → 召回 None", allsafe["recall"], None)
ck("无人需复核 → 误拦率 = 0", allsafe["false_alarm_rate"], 0.0)

# 混入未标注条目时，只统计标了的
mixed = compute_safety([rec("A", True, "green"), {"item_id": "B", "status": "green"}])
ck("混合时只统计已标注", mixed["n"], 1)
ck("混合时漏拦率 = 1/1", mixed["miss_rate"], 1.0)

# ---------------------------------------------------------------- 记号归一 CER
r = compute_cer("x^2-5x+6=0\nx1=2, x2=3", "x²-5x+6=0\nx₁=2, x₂=3")
ck("上下标等价 → 记号归一 0", r["cer_math"], 0.0)
ck("上下标等价 → 去空白口径仍记错（预注册口径不变）", r["cer"] > 0, True)

r = compute_cer("解：x^2=4", "x²=4")
ck("引导词+上标 → 记号归一 0", r["cer_math"], 0.0)

r = compute_cer("（x-2）（x+3）＝0", "(x-2)(x+3)=0")
ck("全角符号 → 记号归一 0", r["cer_math"], 0.0)

# 真实错误必须仍被记为错，否则这个口径就成了粉饰工具
r = compute_cer("(x-2)(x+3)=0", "(x-2)(x-3)=0")
ck("加号误认成减号 → 仍记错", r["cer_math"] > 0, True)
r = compute_cer("x=5", "x=3")
ck("数值错误 → 仍记错", r["cer_math"], round(1 / 3, 4))
r = compute_cer("i had", "I had")
ck("大小写不折叠 → 仍记错", r["cer_math"] > 0, True)

ck("空参照 → None", compute_cer("abc", "")["cer_math"], None)
ck("归一化幂等", _norm_math(_norm_math("解：x²=4")), _norm_math("解：x²=4"))

# ---------------------------------------------------------------- 重试分类
class _E(Exception):
    pass


def exc(msg):
    return _E(msg)


ck("断连可重试", _is_retriable(exc("Connection aborted, RemoteDisconnected")), True)
ck("读超时可重试", _is_retriable(exc("Read timed out. (read timeout=150)")), True)
ck("503 可重试", _is_retriable(exc("503 Service Unavailable")), True)
ck("429 可重试", _is_retriable(exc("429 Too Many Requests")), True)
ck("401 不重试", _is_retriable(exc("401 Unauthorized")), False)
ck("404 不重试", _is_retriable(exc("404 model_not_found")), False)
ck("JSON 错不重试", _is_retriable(exc("Expecting value: line 1 column 1")), False)

# ---------------------------------------------------------------- 脏标注必须被拦下
# 这三条都是实测边界数据集时真实暴露出来的缺陷，不是假想：
#   human_score=9999 曾把 MAE 拉到 9991，报告照样生成；
#   human_score=null 曾让整轮评测崩在中途；
#   needs_review="false" 被 bool() 判成 True，漏拦率反着算。
import json as _json
import tempfile as _tmp
from pathlib import Path as _P

from eval_harness import load_manifest, make_record

_Q = {"Q001": {"question_id": "Q001", "max_score": 10, "subject": "数学",
               "rubric": [{"step_id": 1, "step": "s", "max_score": 10,
                           "knowledge_point": "k"}]}}


def _load(item, questions=_Q):
    d = _P(_tmp.mkdtemp(prefix="zhipi_t_"))
    (d / "manifest.json").write_text(_json.dumps([item], ensure_ascii=False),
                                     encoding="utf-8")
    try:
        load_manifest(d, questions)
        return None
    except SystemExit as exc:
        return str(exc)


_ok = {"item_id": "A", "question_id": "Q001", "ocr_text_human": "x=2",
       "human_score": 8, "human_error_tags": [], "human_grader": "T1"}

ck("合法标注放行", _load(_ok), None)


def _rej(name, override, keyword, drop=None):
    item = dict(_ok)
    item.update(override)
    for k in (drop or ()):
        item.pop(k, None)
    msg = _load(item)
    ok("必须拒收：%s" % name, msg is not None and keyword in msg,
       "返回 %r（期望含 %r）" % (msg, keyword))


_rej("负分", {"human_score": -5}, "不能为负")
_rej("超满分", {"human_score": 9999}, "超过该题满分")
_rej("null 分", {"human_score": None}, "必须是数字")
_rej("字符串分", {"human_score": "8"}, "必须是数字")
_rej("布尔当分数", {"human_score": True}, "必须是数字")
_rej("错因非数组", {"human_error_tags": "计算错误"}, "必须是数组")
_rej("needs_review 字符串", {"needs_review": "false"}, "必须是布尔")
_rej("needs_review 数字", {"needs_review": 1}, "必须是布尔")
# null 与「字段缺失」同义：表示这条没标，合法
ck("needs_review=null 放行（等同未标注）", _load(dict(_ok, needs_review=None)), None)
# 枚举外标签只警告不拒收（错因枚举可能演进），重复标签自动去重
ck("枚举外错因标签放行",
   _load(dict(_ok, human_error_tags=["不存在的错因标签"])), None)
ck("重复错因标签放行",
   _load(dict(_ok, human_error_tags=["计算错误", "计算错误"])), None)
_rej("clarity 越界", {"clarity": 999}, "0-100")
_rej("clarity 负数", {"clarity": -1}, "0-100")
_rej("题目不存在", {"question_id": "Q_NOPE"}, "不存在于题库")
_rej("缺必填字段", {}, "缺少必填字段", drop=["human_grader"])

# 边界值本身合法：0 分与满分都要放行
ck("0 分放行", _load(dict(_ok, human_score=0)), None)
ck("满分放行", _load(dict(_ok, human_score=10)), None)
ck("clarity=0 放行", _load(dict(_ok, clarity=0)), None)
ck("clarity=100 放行", _load(dict(_ok, clarity=100)), None)
ck("needs_review=False 放行", _load(dict(_ok, needs_review=False)), None)
ck("小数分放行", _load(dict(_ok, human_score=7.5)), None)
# 不传题库时跳过满分/题目校验，但类型与负数校验仍生效
ok("无题库时仍拦负分", _load(dict(_ok, human_score=-1), None) is not None)
ok("无题库时不查题目是否存在",
   _load(dict(_ok, question_id="Q_NOPE"), None) is None)

# make_record 对 null 分数要给可读错误，而不是 TypeError
_ai = {"total_score": 8, "max_score": 10, "confidence": 80,
       "status": "yellow", "mode": "llm", "error_tags": []}
try:
    make_record(dict(_ok, human_score=None), _Q["Q001"], _ai)
    ok("make_record 对 null 分数报错", False, "竟然没报错")
except ValueError as exc:
    ok("make_record 对 null 分数给可读错误", "无法转成数字" in str(exc), str(exc))
except Exception as exc:
    ok("make_record 对 null 分数给可读错误", False,
       "抛的是 %s 而非 ValueError" % type(exc).__name__)

# needs_review 非布尔时不得进入统计（宁可不算，也不猜）
r = make_record(dict(_ok, needs_review="false"), _Q["Q001"], _ai)
ok("needs_review 字符串不进统计", "needs_review" not in r,
   "却被纳入了：%r" % r.get("needs_review"))
r = make_record(dict(_ok, needs_review=True), _Q["Q001"], _ai)
ok("needs_review 真布尔进统计", r.get("needs_review") is True)
ok("needs_review=True 时 ai_flagged 由 status 决定", r.get("ai_flagged") is True)
# status=green 时 ai_flagged 必须为 False，否则漏拦永远统计不到
r = make_record(dict(_ok, needs_review=True), _Q["Q001"], dict(_ai, status="green"))
ok("status=green → ai_flagged=False", r.get("ai_flagged") is False)

# ---------------------------------------------------------------- 结果
if fails:
    print("FAIL %d 项：\n" % len(fails))
    for f in fails:
        print("  " + f)
    raise SystemExit(1)
print("eval_harness 公式自检全部通过（分流安全性四象限 / 记号归一 CER / 重试分类）")
