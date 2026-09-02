# -*- coding: utf-8 -*-
"""整页批改链路的离线自检：题库、对齐、解析、痕迹渲染。

不联网、不调模型——这里验的是「模型返回值进来之后，程序有没有把它用对」，
那部分才是回归时最容易被悄悄改坏的。真实模型链路另有 tools/run_eval.py。

    py -3 tools/test_page_pipeline.py
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import bank, confidence as conf, marks, ocr, pagegrader, pagestore, pdfpage

FAIL = []


def check(cond, label):
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
        FAIL.append(label)


def eq(actual, expected, label):
    check(actual == expected, "%s（得到 %r，应为 %r）" % (label, actual, expected)
          if actual != expected else label)


print("\n[1] 分值推定与 Rubric 拆步")
eq(bank.default_score("选择题"), 2, "选择题默认 2 分")
eq(bank.default_score("解答题"), 6, "解答题默认 6 分")
eq(bank.default_score("不存在的题型"), bank.FALLBACK_SCORE, "未知题型走兜底分")

r = bank.rubric_for_item("选择题", 2)
eq(len(r), 1, "客观题不拆步")
eq(r[0]["max_score"], 2.0, "客观题满分即题分")

r = bank.rubric_for_item("解答题", 6)
check(len(r) == 3, "解答题拆 3 步")
eq(sum(s["max_score"] for s in r), 6.0, "解答题各步之和等于题分")

for qtype in bank.DEFAULT_SCORES:
    for total in (2, 3, 4, 5, 6, 7.5, 10, 12, 15):
        steps = bank.rubric_for_item(qtype, total)
        got = round(sum(s["max_score"] for s in steps), 2)
        if got != round(float(total), 2):
            check(False, "%s %g 分拆步之和 %g ≠ 题分" % (qtype, total, got))
            break
    else:
        continue
    break
else:
    check(True, "全部题型 × 分值组合，拆步之和恒等于题分")


print("\n[2] 题干相似度对齐（不按题号）")
teacher = bank.build_questions([{
    "page_id": "PG-t", "page_no": 1,
    "questions": [
        {"no": "1", "stem": "sin60°的值等于（　）", "standard_answer": "C",
         "qtype": "选择题", "printed_max_score": None},
        {"no": "2", "stem": "已知Rt△ABC中，∠C=90°，sinA=1/2，则∠A=___",
         "standard_answer": "30", "qtype": "填空题", "printed_max_score": None},
        {"no": "3", "stem": "求下列各式的值：√2sin30°·cos45°+cos60°",
         "standard_answer": "1", "qtype": "计算题", "printed_max_score": 6},
    ],
}])
eq(len(teacher), 3, "教师页建出 3 道题")
eq(teacher[2]["score_source"], "printed", "卷面印了分值就用卷面的")
eq(teacher[0]["score_source"], "default", "卷面没印按题型推定")
eq(teacher[0]["max_score"], 2.0, "选择题推定 2 分")

# 学生页题号完全不同（模拟教师页/学生页版式不一致），题干基本一致
student = [
    {"index": 1, "no": "三、2", "stem": "已知Rt△ABC中,∠C=90°,sinA=1/2,则∠A=____"},
    {"index": 2, "no": "三、1", "stem": "sin60°的值等于(  )"},
    {"index": 3, "no": "四", "stem": "这是一道教师页里根本没有的题：解方程 2x+1=7"},
]
aligned = bank.align(student, teacher)
eq(aligned[0]["qid"], "Q2", "学生第 1 题按题干对到教师页第 2 题（题号对不上也能对）")
eq(aligned[1]["qid"], "Q1", "学生第 2 题对到教师页第 1 题")
eq(aligned[2]["qid"], None, "题库里没有的题如实标记为未对上")
check(aligned[0]["score"] > 0.9, "同一道题的相似度高于 0.9（当前 %.2f）" % aligned[0]["score"])

# 一对一：两道学生题不能对到同一道教师题
dup = bank.align([{"index": 1, "stem": "sin60°的值等于（　）"},
                  {"index": 2, "stem": "sin60°的值等于（ ）"}], teacher)
check(dup[0]["qid"] != dup[1]["qid"] or dup[1]["qid"] is None,
      "两道极相似的学生题不会同时占用同一道教师题")


print("\n[2d] 页面亲和（题库由多张教师页合并）")
# 实测形态：page_103 上有一道很短的「Can I help you?」，与 page_16 上那道
# 「He always 9 , "Can I help(帮助) you?"」相似度 0.667，两者标准答案不同。
# 学生页明明是 page_16 的，这道却被配到 page_103 去了——界面显示「已匹配」，
# 教师看不出它是拿另一道题的答案判的分。
multi_bank = [
    {"qid": "A1", "no": "1", "stem": "1. A. I'm B. You're C. He's D. She's",
     "standard_answer": "A", "qtype": "选择题", "max_score": 2.0, "page_id": "p16"},
    {"qid": "A2", "no": "2", "stem": "2. A. middle B. English C. first D. good",
     "standard_answer": "B", "qtype": "选择题", "max_score": 2.0, "page_id": "p16"},
    {"qid": "A3", "no": "9", "stem": 'He always 9 , "Can I help(帮助) you?"',
     "standard_answer": "says", "qtype": "填空题", "max_score": 2.0, "page_id": "p16"},
    {"qid": "B1", "no": "13", "stem": "Can I help you?",
     "standard_answer": "Yes, please.", "qtype": "简答题", "max_score": 2.0,
     "page_id": "p103"},
]
stu = [{"index": 1, "stem": "1. A. I'm B. You're C. He's D. She's"},
       {"index": 2, "stem": "2. A. middle B. English C. first D. good"},
       {"index": 3, "stem": 'He always 9 , "Can I help you?"'}]
al3 = bank.align(stu, multi_bank)
eq([a["qid"] for a in al3], ["A1", "A2", "A3"],
   "第 3 道落回本页的 A3，而不是别页那道很短的 B1")
check(all(a["score"] <= 1.0 for a in al3),
      "返回的仍是真实相似度，没把亲和加成算进去")

# 主页判不出来时不启用：学生页真的跨两张教师页（双面、拼接）不能被误伤
cross = [{"index": 1, "stem": "1. A. I'm B. You're C. He's D. She's"},
         {"index": 2, "stem": "Can I help you?"}]
eq([a["qid"] for a in bank.align(cross, multi_bank)], ["A1", "B1"],
   "一页一半、另一页一半时，两边各自对上各自的（不启用亲和）")

# 单张教师页的题库：亲和逻辑不该改变任何结果
one_page = [q for q in multi_bank if q["page_id"] == "p16"]
eq([a["qid"] for a in bank.align(stu, one_page)], ["A1", "A2", "A3"],
   "单页题库的对齐结果不受影响")


print("\n[2c] 包含匹配（学生页把题切得比教师页短）")
# 实测英语完形填空：教师页是整句，学生页只读出其中一小段。它确确实实是那道题，
# 但对称的 ratio 只有 0.3——按阈值判就成了「题库中无此题」。
cloze_bank = [
    {"qid": "C1", "no": "8",
     "stem": "Jasper is 12, too. He is a 8 boy and he is good to other people.",
     "standard_answer": "nice", "qtype": "填空题", "max_score": 2.0},
    {"qid": "C2", "no": "10", "stem": 'or " 10 are you?" I like him very much.',
     "standard_answer": "How", "qtype": "填空题", "max_score": 2.0},
]
frag = [{"index": 1, "stem": "is a 8 boy"},
        {"index": 2, "stem": '" 10 are you?"'}]
check(bank._similarity(frag[0]["stem"], cloze_bank[0]["stem"]) < bank.MATCH_THRESHOLD,
      "片段与整句的对称相似度确实低于阈值（所以才需要包含判据）")
fa = bank.align(frag, cloze_bank)
eq([a["qid"] for a in fa], ["C1", "C2"], "片段靠包含匹配对上了各自那道题")

# 包含判据不能把不相干的东西也吸进来
noise = [{"index": 1, "stem": "下列对选文理解不正确的一项是"}]
eq(bank.align(noise, cloze_bank)[0]["qid"], None, "不相干的题干不会被包含匹配捞上")

# 太短的片段一律不参与：几个字符在任何长题干里都能凑出高覆盖
eq(bank.align([{"index": 1, "stem": "a 8"}], cloze_bank)[0]["qid"], None,
   "过短的片段不参与包含匹配")

# 没有区分度的片段整个放弃，而不是随机落到其中一道上。
# 这里两道题库题都完整包含「is a 8 boy」，且各自都太长、对称 ratio 都不到阈值，
# 所以只可能走包含匹配——正是唯一性判据该拦下的情形。
amb_bank = [
    cloze_bank[0],
    {"qid": "C4", "no": "12",
     "stem": "Tom is 13. He is a 8 boy and he likes reading books very much.",
     "standard_answer": "nice", "qtype": "填空题", "max_score": 2.0},
]
check(all(bank._similarity("is a 8 boy", b["stem"]) < bank.MATCH_THRESHOLD
          for b in amb_bank), "两道候选的对称相似度都低于阈值（确保只走包含匹配）")
eq(bank.align([{"index": 1, "stem": "is a 8 boy"}], amb_bank)[0]["qid"], None,
   "片段同时落在多道题库题里时不匹配（宁可报无此题，也不配到错的题上）")

# 真·相似匹配必须优先于包含匹配（折价的意义）
both = [{"index": 1, "stem": 'or " 10 are you?" I like him very much.'}]
eq(bank.align(both, cloze_bank)[0]["qid"], "C2",
   "同时存在高 ratio 与高覆盖时，取真正相似的那道")


print("\n[2b] 小问归并（教师页拆条 / 学生页整块）")# 真实形态：教师答案页把第 2 题按 (1)(2)(3)(4) 拆成四条逐条给答案，
# 学生练习册上第 2 题是一整块——学生也是连着做的，中间没有可切的边界。
# 一对一只能对上第一条，剩下三条会被误报成「题库有、本页没识别到」。
sub_bank = [
    {"qid": "B1", "no": "2(1)", "stem": "(1)sinA=1/2，则∠A=　　°；",
     "standard_answer": "30", "qtype": "填空题", "max_score": 2.0},
    {"qid": "B2", "no": "2(2)", "stem": "(2)cosA=√3/2，则∠A=　　°；",
     "standard_answer": "30", "qtype": "填空题", "max_score": 2.0},
    {"qid": "B3", "no": "2(3)", "stem": "(3)tanA=√3，则∠A=　　°；",
     "standard_answer": "60", "qtype": "填空题", "max_score": 2.0},
    {"qid": "B4", "no": "3", "stem": "已知抛物线的顶点坐标与对称轴，求它的解析式。",
     "standard_answer": "y=x²", "qtype": "解答题", "max_score": 8.0},
]
whole = [{"index": 1, "stem": "已知Rt△ABC中，∠C=90°，"
                              "(1)sinA=1/2，则∠A=　　°；"
                              "(2)cosA=√3/2，则∠A=　　°；"
                              "(3)tanA=√3，则∠A=　　°；"}]
al = bank.align(whole, sub_bank)
ab = bank.absorb_leftovers(whole, sub_bank, al)
hit_ids = {a["qid"] for a in al if a["qid"]}
absorbed_ids = {e[0]["qid"] for e in ab.get(0, [])}
# 断言写成「三条小问最终都归到这道整块题」，而不是「谁走命中、谁走归并」：
# 主命中在不在，取决于整块题干与第一条小问的相似度有没有擦过 0.55——
# 实测数学 83 卷是 0.580 擦线过，这里构造的短题干则没过。两种都对，
# 而且没过的那种正是界面上报「题库中无此题」的情形，恰恰是要修的。
eq(sorted(hit_ids | absorbed_ids), ["B1", "B2", "B3"],
   "三个小问最终全部归到这道整块题（一对一命中 + 归并）")
check("B4" not in absorbed_ids, "不相干的第 3 题不会被误吸进来")
check(all(cov >= bank._ABSORB_COVERAGE for _bq, cov in ab.get(0, [])),
      "归并的覆盖率都达到阈值")

# 覆盖率判据要能挡住「短题干在任何长题里都能凑出高覆盖」
eq(bank.absorb_leftovers(whole, [{"qid": "S1", "no": "1", "stem": "计算：",
                                  "standard_answer": "", "qtype": "解答题",
                                  "max_score": 2.0}], [{"index": 1, "qid": None,
                                                        "score": 0.0}]),
   {}, "过短的题干不参与归并")

# 一道题库题只归并到一处：两道学生题都抄了同一个小问时，分值不能算两次
two = [{"index": 1, "stem": "已知Rt△ABC中，(2)cosA=√3/2，则∠A=　　°；"},
       {"index": 2, "stem": "另一题里也写了(2)cosA=√3/2，则∠A=　　°；"}]
ab2 = bank.absorb_leftovers(two, [sub_bank[1]],
                            [{"index": 1, "qid": None, "score": 0.0},
                             {"index": 2, "qid": None, "score": 0.0}])
eq(sum(len(v) for v in ab2.values()), 1, "同一道题库题最多只归并到一处")

# 归并不能跨教师页：别页的题是独立一道题，不是这道题的小问。
# 实测栽过——page_16 的「He always 9 , "Can I help(帮助) you?"」正确命中本页题，
# 却把 page_103 上那道独立的短题「Can I help you?」当小问吸了进来。
xpage_bank = [
    {"qid": "P1", "no": "9", "stem": 'He always 9 , "Can I help(帮助) you?"',
     "standard_answer": "says", "qtype": "填空题", "max_score": 2.0,
     "page_id": "p16"},
    {"qid": "P2", "no": "13", "stem": "Can I help you?",
     "standard_answer": "Yes, please.", "qtype": "简答题", "max_score": 2.0,
     "page_id": "p103"},
]
xstu = [{"index": 1, "stem": 'He always ___ , "Can I help(帮助) you?"'}]
xal = bank.align(xstu, xpage_bank)
eq(xal[0]["qid"], "P1", "先确认一对一命中的是本页那道")
eq(bank.absorb_leftovers(xstu, xpage_bank, xal), {},
   "别页那道独立题不会被当成小问归并进来")

# 同页的才归并（把上面那道改成同页，就该被吸收）
same_page = [xpage_bank[0], {**xpage_bank[1], "page_id": "p16"}]
eq([e[0]["qid"] for e in
    bank.absorb_leftovers(xstu, same_page, bank.align(xstu, same_page)).get(0, [])],
   ["P2"], "同一张教师页上的才归并")

# 一对一没命中的学生题不能变成「磁铁」，把别页的题吸过来。
# 实测 page_29 的完形填空首句本页最像只有 0.341（够不着阈值），却吸走了
# page_103 上的题——约束按命中页取，它没有命中页，约束等于不存在。
magnet_bank = [
    {"qid": "M1", "no": "1", "stem": "1. A. I'm B. You're C. He's D. She's",
     "standard_answer": "A", "qtype": "选择题", "max_score": 2.0, "page_id": "p29"},
    {"qid": "M2", "no": "2", "stem": "2. A. middle B. English C. first D. good",
     "standard_answer": "B", "qtype": "选择题", "max_score": 2.0, "page_id": "p29"},
    # 别页的一道独立题，整段包含在下面那道学生题干里
    {"qid": "M3", "no": "5", "stem": "His father has three brothers",
     "standard_answer": "uncles", "qtype": "填空题", "max_score": 2.0,
     "page_id": "p103"},
]
magnet_stu = [
    {"index": 1, "stem": "1. A. I'm B. You're C. He's D. She's"},
    {"index": 2, "stem": "2. A. middle B. English C. first D. good"},
    # 这道一对一对不上任何题，正是「磁铁」：题干长，与题库任何一条的对称
    # ratio 都够不着阈值，但别页那条短题整段写在它里面。
    {"index": 3, "stem": "Sam has a big family. Charles and Janet are his 1. ___. "
                         "His father has three brothers and they are his uncles. "
                         "His mother has two sisters and they are his aunts. "
                         "Here are the photos of Sam's family on the wall."},
]
mal = bank.align(magnet_stu, magnet_bank)
eq(mal[2]["qid"], None, "先确认第 3 道一对一确实没命中")
check(bank._coverage(bank._norm(magnet_bank[2]["stem"]),
                     bank._norm(magnet_stu[2]["stem"])) >= bank._ABSORB_COVERAGE,
      "别页那条短题确实整段包含在它里面（没有页面约束就会被吸走）")
eq(bank.absorb_leftovers(magnet_stu, magnet_bank, mal), {},
   "没命中的题按主页兜底，不会把别页的题吸过来")

# 合并后的标准答案必须带题号，否则判分模型只能猜哪个答案配哪一问
merged = bank.merge_standard_answers([(sub_bank[1], 1.0), (sub_bank[2], 1.0)])
check("2(2)" in merged and "2(3)" in merged, "合并的标准答案带题号前缀")
eq(len(merged.splitlines()), 2, "每条小问答案各占一行")


print("\n[3] bbox 归一与防呆")
eq(ocr._normalize_bbox([100, 200, 300, 400]), [0.1, 0.2, 0.3, 0.4], "0-1000 整数坐标归一")
eq(ocr._normalize_bbox([0.1, 0.2, 0.3, 0.4]), [0.1, 0.2, 0.3, 0.4], "已是 0-1 小数则原样")
eq(ocr._normalize_bbox([300, 400, 100, 200]), [0.1, 0.2, 0.3, 0.4], "左右/上下颠倒会被修正")
eq(ocr._normalize_bbox([0, 0, 1000, 1000]), None, "整页大小的框判为没框准")
eq(ocr._normalize_bbox([10, 10, 12, 12]), None, "面积近乎为零的框丢弃")
eq(ocr._normalize_bbox("1,2,3,4"), None, "非数组丢弃")
eq(ocr._normalize_bbox([1, 2, 3]), None, "长度不是 4 丢弃")
eq(ocr._normalize_bbox([float("nan")] * 4), None, "NaN 丢弃")


print("\n[4] 题型归一")
eq(ocr._normalize_qtype("单选题"), "选择题", "「单选题」归到选择题")
eq(ocr._normalize_qtype("multiple choice"), "选择题", "英文说法归到选择题")
eq(ocr._normalize_qtype("", "", "C"), "选择题", "答案是单个字母时按选择题")
eq(ocr._normalize_qtype("", "", "30°"), "填空题", "答案是短词时按填空题")
eq(ocr._normalize_qtype("", "", "解：由题意得……\n所以 x=2"), "简答题", "多行答案按简答题")


print("\n[5] 整页判分解析（模型返回值的边界处理）")
items = [
    {"index": 1, "no": "1", "stem": "题一", "student_answer": "C", "max_score": 2,
     "qid": "Q1", "qtype": "选择题", "standard_answer": "C", "match_score": 0.95},
    {"index": 2, "no": "2", "stem": "题二", "student_answer": "", "max_score": 2,
     "qid": "Q2", "qtype": "填空题", "standard_answer": "30", "match_score": 0.9},
    {"index": 3, "no": "3", "stem": "题三", "student_answer": "解：x=2", "max_score": 6,
     "qid": None, "qtype": "", "standard_answer": "", "match_score": 0.0},
]
parsed = pagegrader._parse_questions(items, {"questions": [
    # 故意乱序 + 超界分数 + 漏掉第 3 题 + 多返回一条不存在的题
    {"index": 2, "score": 2, "verdict": "correct", "error_tag": "计算错误"},
    {"index": 1, "score": 99, "verdict": "correct", "knowledge_point": "特殊角三角函数"},
    {"index": 9, "score": 5, "verdict": "correct"},
]})
eq(len(parsed), 3, "返回条数恒等于题目数（漏题补齐、多余的丢弃）")
eq(parsed[0]["score"], 2.0, "超界分数被 clamp 到该题满分")
eq(parsed[0]["knowledge_point"], "特殊角三角函数", "知识点按 index 对齐，没有错位")


print("\n[5b] 无判分基准的题：折减置信度 + 压住绿灯")
# 上面 items 里第 3 题 qid=None、standard_answer 为空，占 6/10 分值。
# 实测踩过的坑：模型对这类题写「由于标准答案缺失，按正确处理」直接给满分，
# 一份没做完的卷子被判成 38/38 全对。
ub = pagegrader._unbased_flags(items, True)
eq([u["no"] for u in ub], ["3"], "只有没有标准答案的那道题算无基准")
eq(pagegrader._unbased_flags(items, False), [],
   "整页无题库时不算无基准（那时 answer_match 整维剔除，教师也知情）")

# 折减按分值占比：6/10 → 1 - 0.6*0.6 = 0.64
eq(pagegrader._discount_unbased(90.0, ub, 10.0), 57.6, "按分值占比折减置信度")
eq(pagegrader._discount_unbased(90.0, [], 10.0), 90.0, "没有无基准题则不折减")
eq(pagegrader._discount_unbased(90.0, ub, 0.0), 90.0, "满分为 0 时不折减（不做除零）")
check(pagegrader._discount_unbased(90.0, ub, 10.0) < conf.GREEN_THRESHOLD,
      "六成分值无基准时，原本绿灯的置信度被折到绿线以下")

# 分值占比小的时候折减也小，但绿灯仍要被后置防线压掉（见 grade_page）
small = pagegrader._unbased_flags(
    [{"index": 1, "no": "1", "max_score": 1, "standard_answer": ""},
     {"index": 2, "no": "2", "max_score": 99, "standard_answer": "x"}], True)
check(pagegrader._discount_unbased(90.0, small, 100.0) > conf.GREEN_THRESHOLD,
      "只有 1% 分值无基准时置信度几乎不动（靠分流那道防线兜）")
eq(parsed[1]["verdict"], "blank", "学生没作答的题一律判 blank")
eq(parsed[1]["score"], 0.0, "没作答就不能得分（模型给了 2 分也不认）")
eq(parsed[1]["error_tag"], "计算错误", "枚举内的错因标签保留")
eq(parsed[2]["score"], 0.0, "模型漏掉的题按 0 分且不报错")
eq(parsed[2]["verdict"], "wrong", "有作答但模型没给结论时按 wrong，不是 blank")
eq(parsed[2]["matched"], False, "没对上题库的题标记为未匹配")

bad_tag = pagegrader._parse_questions(items[:1], {"questions": [
    {"index": 1, "score": 1, "verdict": "partial", "error_tag": "自己编的标签"}]})
eq(bad_tag[0]["error_tag"], "", "枚举外的错因标签被丢弃")


print("\n[6] 答案匹配度（按题分加权，未对上的不计入）")
eq(pagegrader.answer_match(parsed), 50.0,
   "对上的两题：2/2 与 0/2 → 50 分（第 3 题未对上，不计入）")
eq(pagegrader.answer_match([{"matched": False, "score": 0, "max_score": 6}]), None,
   "一道都没对上时返回 None（该因子重归一化剔除，不是 0 分）")
weighted = pagegrader.answer_match([
    {"matched": True, "score": 2, "max_score": 2},     # 小题全对
    {"matched": True, "score": 0, "max_score": 15},    # 大题全错
])
check(weighted < 20, "按题分加权而非按题数：小题全对救不回大题全错（得 %.1f）" % weighted)


print("\n[7] 批改痕迹渲染（真实页图）")
from PIL import Image

canvas = Image.new("RGB", (1240, 1754), "white")
buf = io.BytesIO()
canvas.save(buf, format="JPEG")
page_bytes = buf.getvalue()

marked = marks.render(page_bytes, [
    {"index": 1, "no": "1", "score": 2, "max_score": 2, "verdict": "correct",
     "bbox": [0.1, 0.1, 0.45, 0.2], "answer_box": [0.30, 0.12, 0.40, 0.17]},
    {"index": 2, "no": "2", "score": 1, "max_score": 2, "verdict": "partial",
     "bbox": [0.1, 0.25, 0.45, 0.35], "answer_box": [0.30, 0.27, 0.40, 0.32]},
    {"index": 3, "no": "3", "score": 0, "max_score": 6, "verdict": "wrong",
     "bbox": None},                      # 没有坐标：退回右缘默认位置
    {"index": 4, "no": "4", "score": 0, "max_score": 6, "verdict": "blank",
     "bbox": None},
], header={"student": "张同学", "total": 3, "max": 16, "status": "yellow"})
check(len(marked) > 1000, "渲染出非空 JPEG（%d 字节）" % len(marked))
with Image.open(io.BytesIO(marked)) as out:
    eq(out.format, "JPEG", "输出是 JPEG")
    check(max(out.size) <= marks.MAX_EDGE, "长边压到 %d 以内（实际 %s）" % (marks.MAX_EDGE, out.size))
    rgb = out.convert("RGB")
    colors = {c for _n, c in rgb.getcolors(maxcolors=1 << 20)}
    check(any(abs(c[0] - marks.COLOR_RIGHT[0]) < 40 and c[1] < 90 and c[2] < 90
              for c in colors), "图上出现了对勾的红色")
    check(any(c[0] > 180 and 80 < c[1] < 180 and c[2] < 90 for c in colors),
          "图上出现了半勾的橙色（部分给分）")

    # 记号必须落在**作答框**旁边，而不是页边。第 1 题的作答框右缘在 40% 处，
    # 记号画在它右侧一小段内；跑到页面最右边就说明又退回「贴着题目/页边」了。
    w, h = out.size
    band = rgb.crop((0, int(0.10 * h), w, int(0.20 * h)))
    px = band.load()
    xs = [x for y in range(band.height) for x in range(band.width)
          if px[x, y][0] > 150 and px[x, y][1] < 110 and px[x, y][2] < 110]
    check(bool(xs), "第 1 题所在的横带上找得到红色记号")
    if xs:
        rel = max(xs) / float(w)
        check(rel < 0.62,
              "记号贴着作答（右缘 %.2f，作答框右缘 0.40），没有退到页边" % rel)

    # 不再有白底方框：早期版本给每处痕迹垫一块 82% 不透明的白牌，于是记号周围
    # 会出现一圈纯白的圆角矩形边界。这里验「记号附近没有成片的纯白硬边」——
    # 底图本来就是白纸，所以改验方框的**描边色**不成矩形：直接验红色像素数量
    # 远小于一个方框描边应有的量。
    reds = sum(n for n, c in rgb.getcolors(maxcolors=1 << 20)
               if c[0] > 150 and c[1] < 110 and c[2] < 110)
    check(reds < w * h * 0.01,
          "红色像素只占 %.3f%%：是记号本身，不是方框描边" % (100.0 * reds / (w * h)))

# 中文字体：页眉的学生名与「第N题」标注要用它，画不出中文会变成方块
font = marks._load_font(24)
check(getattr(font, "path", None) is not None or hasattr(font, "getbbox"),
      "找到了可用字体（%s）" % getattr(font, "path", "PIL 内置"))

# 对错记号必须是画出来的线，不是字符——中文字体普遍不含 ✓(U+2713)/✗(U+2717)，
# 用字符渲染会在每道题旁边留一个豆腐块。这里直接验「画出来的东西非空」。
from PIL import ImageDraw
for verdict, want in (("correct", "勾"), ("partial", "半勾"),
                      ("wrong", "叉"), ("blank", "空心圈")):
    probe = Image.new("RGB", (60, 60), "white")
    marks._draw_glyph(ImageDraw.Draw(probe), verdict, 5, 5, 48, (200, 0, 0))
    check(probe.getbbox() is not None, "%s 记号画得出来（不依赖字体）" % want)


print("\n[8] 页图暂存：会话隔离与淘汰")
pid = pagestore.put("sid-a", b"x" * 1000, "image/jpeg", {"page_no": 1})
check(pagestore.get("sid-a", pid) is not None, "本会话取得到自己的页")
check(pagestore.get("sid-b", pid) is None, "别的会话取不到（跨会话隔离）")
check(pagestore.set_marked("sid-a", pid, b"y" * 500), "能回填痕迹图")
eq(pagestore.get("sid-a", pid)["marked"], b"y" * 500, "回填的痕迹图取得回来")
check(not pagestore.set_marked("sid-b", pid, b"z"), "别的会话回填不了痕迹")
eq(pagestore.drop_session("sid-a"), 1, "重置清掉本会话页图")
check(pagestore.get("sid-a", pid) is None, "清掉之后取不到了")


print("\n[9] PDF 拆页（真实测评数据）")
pdf_path = Path(r"E:\希沃智教π\测评数据\测评数据\语文\教师页\杨老师—1.pdf")
if pdf_path.exists():
    raw = pdf_path.read_bytes()
    check(pdfpage.is_pdf(raw), "按文件头认出 PDF")
    check(not pdfpage.is_pdf(page_bytes), "JPEG 不会被误认成 PDF")
    pages, total = pdfpage.render_pages(raw)
    eq(total, 2, "杨老师—1.pdf 共 2 页")
    eq(len(pages), 2, "两页都渲染出来了")
    for no, blob in pages:
        with Image.open(io.BytesIO(blob)) as im:
            check(im.width > 800 and im.height > 800,
                  "第 %d 页渲染尺寸够识别（%s）" % (no, im.size))
    sizes = [len(b) for _n, b in pages]
    check(max(sizes) < 1_000_000, "单页 JPEG 控制在 1MB 内（最大 %d 字节）" % max(sizes))
else:
    print("  skip 测评数据不在预期路径，跳过 PDF 实测")


print("\n[10] 待复核判定（识别层报出的可疑题）")
# 只认「系统自己知道没读准」的信号。判得太松，整页都是黄灯，教师会当噪音忽略；
# 判得太严，读不准的题混在绿灯里自动通过——后者才是不可接受的那一侧。
def _flag(item):
    return pagegrader._review_flags([dict({"index": 1, "no": "1"}, **item)])

check(_flag({"student_answer": "B", "legible_hint": False}), "模型说看不清 → 报")
check(_flag({"student_answer": "〔?〕x+1"}), "转写含 〔?〕 → 报")
check(_flag({"student_answer": "5", "attribution_confidence": 0.5}), "归属把握 0.5 → 报")
check(_flag({"student_answer": "x", "refine_changed": True}), "两次读取不一致 → 报")
check(_flag({"student_answer": "", "refine_reason": "blank_with_stray"}),
      "复识后仍找不回作答 → 报")
check(_flag({"qtype": "选择题", "student_answer": "5/3"}), "选择题转写不是选项 → 报")

check(not _flag({"qtype": "选择题", "student_answer": "B"}), "选择题正常字母 → 不报")
check(not _flag({"qtype": "选择题", "student_answer": "（B）"}), "选择题带括号 → 不报")
check(not _flag({"qtype": "选择题", "student_answer": "BD"}), "选择题多选 → 不报")
check(not _flag({"student_answer": "x=3", "attribution_confidence": 0.95}), "把握高 → 不报")
check(not _flag({"student_answer": "x=3"}), "无归属把握字段 → 不报")
# 学生真没做的空题不该逐道报复核：一份空白卷会刷满整个复核清单，
# 真正读不准的那道反而被淹掉。只有复识确认「纸上有笔迹却没归属上」才报。
check(not _flag({"student_answer": ""}), "单纯空答 → 不报")
# bool 混进置信度字段时 True 会被 float() 悄悄变成 1.0，伪装成「把握满分」
check(not _flag({"student_answer": "x", "attribution_confidence": True}),
      "bool 型 attribution_confidence 不被当成 1.0")

flags = pagegrader._review_flags([
    {"index": 1, "no": "1", "student_answer": "", "refine_reason": "blank_with_stray"},
    {"index": 2, "no": "2", "student_answer": "x=3"},
    {"index": 3, "no": "3", "qtype": "选择题", "student_answer": "解：x"},
])
eq([f["no"] for f in flags], ["1", "3"], "多题只挑出可疑的两道")

# 可疑题要同时下调 OCR 清晰度：只压分流的话，这份作业在看板上仍是高清晰度样本
eq(pagegrader._adjust_clarity(90, []), 90.0, "无可疑题不扣分")
eq(pagegrader._adjust_clarity(90, [1]), 78.0, "一道可疑题扣 12")
eq(pagegrader._adjust_clarity(90, [1, 2, 3]), 54.0, "三道扣 36")
eq(pagegrader._adjust_clarity(90, [1] * 10), 50.0, "扣分不超过上限 40")
eq(pagegrader._adjust_clarity(0, [1]), 0.0, "清晰度 0 不会被扣成负数")


print("\n[11] 分式漏读的复识触发")
# 学生写 1/(b-a)、系统读成 b-a 是这类作业最隐蔽的错：长度正常、归属把握高、
# 没越界，其它触发条件一条都不亮，判错分还带着满置信度自动通过。
def _refine_reason(stem, answer):
    return ocr._needs_refine({"stem": stem, "answer": answer}, False)

eq(_refine_reason("约分：(2) (x-1)/(x^2-1)", "x"), "fraction_mismatch", "分式题只读出分母")
eq(_refine_reason("(3) (a-b)^2/(b-a)^3", "b-a"), "fraction_mismatch", "题干含分数线、答案没有")
eq(_refine_reason("(3) (a-b)^2/(b-a)^3", "1/(b-a)"), "", "已读出分式则不再复识")
# 多小问合并成一段时，一个小问的分数线会盖住另一个丢了分子的小问
eq(_refine_reason("约分：(1) 5ab/20a^2b；(2) (x-1)/(x^2-1)；(3) (a^2-b^2)/(a-b)",
                  "(1) x^3/8; (2) -m/5n; (3) b-a"), "fraction_mismatch",
   "三个小问只有两条分数线")
eq(_refine_reason("约分：(1) 5ab/20a^2b；(2) (x-1)/(x^2-1)；(3) (a^2-b^2)/(a-b)",
                  "(1) 1/(4a); (2) 1/(x+1); (3) (a-b)/(a+b)"), "",
   "三个小问三条分数线则放行")
eq(_refine_reason("化简整式 (a+b)^2", "a^2+2ab+b^2"), "", "整式题不误触发")
eq(_refine_reason("计算 2+3 的值", "5"), "short_answer", "非分式题仍按原有条件走")

# 选择题的复识结果必须仍是干净的选项字母，别把旁边的演算换进来
check(ocr._is_choice_answer("D"), "D 是合法选项转写")
check(ocr._is_choice_answer("（B）"), "带括号的选项")
check(ocr._is_choice_answer("A、C"), "多选")
check(not ocr._is_choice_answer("D\n(x+1)(x-1)"), "夹带演算的不算合法选项")
check(not ocr._is_choice_answer("1/(x+1)"), "算式不算合法选项")


print("\n" + "=" * 60)
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("全部通过")
