# -*- coding: utf-8 -*-
"""教师答案页 → 会话题库。

## 这个模块解决什么

置信度里的「答案匹配度」占 25% 权重，但它需要一个**基准**：学生写的这个答案，
和标准答案比对得上吗？真实作业几乎必然不在内置题库里，于是这一维长期
测不出、被重归一化剔除——五因子实际只有四因子在工作。

教师手上恰好有那个基准：答案页。所以入口分成两个——教师先传答案页建题库，
再传学生页批改；有题库就逐题比对得分，没题库就让大模型自行判分、
答案匹配度这一维留空（不是记 0 分，那等于凭空扣掉 25 分）。

## 分值从哪来

优先用卷面印的分值（`printed_max_score`）；卷面没印的按题型默认值推定，
并把 `score_source` 标成 `default` 让界面写明「系统推定，教师可改」。
推定值绝不能冒充卷面值——教师看到「6 分」时必须知道这 6 分是谁定的。

## 题库是会话级的

题库跟着浏览器会话走，不落盘、不跨用户。公开链接上一个教师传的答案页，
不该成为另一个人作业的判分基准。
"""
import difflib
import re
import time
import uuid
from collections import OrderedDict


# ---------------------------------------------------------------------------
# 分值推定
# ---------------------------------------------------------------------------
# 卷面没印分值时按题型给的默认分。数字取自常见的初中作业本 / 周周清卷：
# 选择填空一题 2 分、翻译 4 分、解答 6 分是最普遍的排布。
# 它们**只是推定**，界面必须标注来源，教师可改。
DEFAULT_SCORES = {
    "选择题": 2,
    "填空题": 2,
    "判断题": 2,
    "翻译题": 4,
    "简答题": 4,
    "计算题": 6,
    "解答题": 6,
    "作文题": 15,
}
FALLBACK_SCORE = 4        # 题型也认不出时的兜底


def default_score(qtype: str) -> int:
    return DEFAULT_SCORES.get((qtype or "").strip(), FALLBACK_SCORE)


# ---------------------------------------------------------------------------
# Rubric 拆步
# ---------------------------------------------------------------------------
# 客观题拆步没有意义：选择题答案要么对要么错，硬拆成「题意理解 / 方法选择 /
# 结论正确」三步，会让一道 2 分的选择题拿到 1.3 分这种无法向学生解释的结果。
# 主观题才需要过程分——这正是「步骤级证据链」要服务的场景。
_OBJECTIVE = {"选择题", "填空题", "判断题"}

# 主观题的拆步模板：(步骤名, 占总分比例)。比例之和恒为 1。
_STEPS_SOLVE = (("方法选择", 0.3), ("运算执行", 0.4), ("结论正确", 0.3))
_STEPS_TRANSLATE = (("要点完整", 0.5), ("语言准确", 0.5))
_STEPS_ESSAY = (("内容切题", 0.3), ("结构完整", 0.3), ("语言准确", 0.4))
_STEPS_SHORT = (("要点完整", 0.6), ("表述准确", 0.4))

_STEP_TEMPLATES = {
    "计算题": _STEPS_SOLVE,
    "解答题": _STEPS_SOLVE,
    "翻译题": _STEPS_TRANSLATE,
    "作文题": _STEPS_ESSAY,
    "简答题": _STEPS_SHORT,
}


def rubric_for_item(qtype: str, max_score: float) -> list:
    """按题型与分值生成该题的 Rubric（结构与题库内题目的 rubric 一致）。

    客观题恒为单步「答案正确」，满分即题分；主观题按模板拆 2-3 步，
    比例分完后把舍入误差补回最后一步，保证各步之和**恰好**等于题分——
    差 0.5 分的 Rubric 会让「满分」永远拿不到。
    """
    total = float(max_score)
    if (qtype or "") in _OBJECTIVE or total <= 2:
        # 2 分及以下的题也不拆：拆出来每步不足 1 分，过程分没有表达力
        return [{"step": "答案正确", "max_score": total, "knowledge_point": "",
                 "desc": "作答与标准答案是否一致"}]

    template = _STEP_TEMPLATES.get((qtype or "").strip(), _STEPS_SOLVE)
    steps, used = [], 0.0
    for i, (name, ratio) in enumerate(template):
        if i == len(template) - 1:
            score = round(total - used, 1)          # 末步吃掉全部舍入误差
        else:
            score = round(total * ratio * 2) / 2    # 其余步按 0.5 分粒度取整
            used += score
        steps.append({"step": name, "max_score": score, "knowledge_point": "",
                      "desc": ""})
    return steps


# ---------------------------------------------------------------------------
# 题库存储（会话级）
# ---------------------------------------------------------------------------

BANK_MAX = 10             # 单会话最多留几套题库，超出淘汰最旧的


def _ensure(session: dict) -> "OrderedDict":
    banks = session.get("banks")
    if not isinstance(banks, OrderedDict):
        banks = OrderedDict()
        session["banks"] = banks
    return banks


def create(session: dict, name: str, subject: str, questions: list,
           page_ids: list = None) -> dict:
    """把识别出来的教师页题目存成一套题库，返回题库记录。"""
    banks = _ensure(session)
    bank_id = "BK-" + uuid.uuid4().hex[:10]
    bank = {
        "bank_id": bank_id,
        "name": (name or "").strip()[:40] or "未命名题库",
        "subject": subject or "",
        "created": time.time(),
        "page_ids": list(page_ids or []),
        "questions": questions,
    }
    banks[bank_id] = bank
    while len(banks) > BANK_MAX:
        banks.popitem(last=False)
    return bank


def get(session: dict, bank_id: str) -> dict | None:
    return _ensure(session).get(bank_id)


def remove(session: dict, bank_id: str) -> bool:
    return _ensure(session).pop(bank_id, None) is not None


def listing(session: dict) -> list:
    """题库清单（不含题目正文，供下拉框用）。最近建的排前面。"""
    banks = _ensure(session)
    out = []
    for bank in banks.values():
        out.append({
            "bank_id": bank["bank_id"],
            "name": bank["name"],
            "subject": bank["subject"],
            "question_count": len(bank["questions"]),
            "total_score": round(sum(q["max_score"] for q in bank["questions"]), 1),
            "created": bank["created"],
        })
    out.reverse()
    return out


def build_questions(pages: list) -> list:
    """把若干页教师页识别结果合并成题库题目列表。

    pages: [{"page_id":…, "questions":[{no, stem, standard_answer, qtype,
             printed_max_score}, …]}, …]，按页序给。
    题号在页与页之间连续重排（qid 用连续序号），因为对齐靠的是题干文本，
    题号只用于显示——学生页的题号可能与教师页不一致（版式不同），
    拿它当主键会把「第 5 题」对到另一道「第 5 题」上。
    """
    out = []
    for page in pages:
        for q in page.get("questions") or []:
            printed = q.get("printed_max_score")
            qtype = q.get("qtype") or "简答题"
            if isinstance(printed, (int, float)) and printed > 0:
                max_score, source = float(printed), "printed"
            else:
                max_score, source = float(default_score(qtype)), "default"
            out.append({
                "qid": "Q%d" % (len(out) + 1),
                "no": q.get("no") or "",
                "stem": q.get("stem") or "",
                "standard_answer": q.get("standard_answer") or "",
                "qtype": qtype,
                "max_score": max_score,
                "score_source": source,        # printed=卷面印的 / default=系统推定
                "rubric": rubric_for_item(qtype, max_score),
                "page_id": page.get("page_id") or "",
                "page_no": page.get("page_no") or 1,
            })
    return out


# ---------------------------------------------------------------------------
# 学生页 ↔ 题库对齐
# ---------------------------------------------------------------------------
# 为什么不按题号对齐：语文的教师页和学生页根本不是同一份版式（教师页是
# 答案册，学生页是练习册），题号各排各的；英语周周清里同一个「1.」在
# 「单词」「短语」「句式」三个板块各出现一次。按题号对齐会稳定地对错行，
# 而且错得毫无征兆——分数照出，证据链照给，只是比错了题。
#
# 题干文本是两边唯一真正共享的东西，所以按它的相似度对齐，并且要求
# 达到阈值才认；认不上的题**明说没对上**，宁可这一题不计入答案匹配度。

MATCH_THRESHOLD = 0.55    # 低于此相似度视为没对上
_PUNCT_RE = re.compile(r"[\s，。；：、？！,.;:?!·—…（）()【】\[\]「」“”\"'’‘_＿]+")


def _norm(text: str) -> str:
    """归一题干：去空白与标点。两页扫描出来的标点差异极大，留着只会拉低相似度。"""
    return _PUNCT_RE.sub("", str(text or ""))


def _similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _coverage(needle: str, haystack: str) -> float:
    """needle 有多大比例的字符能在 haystack 里按序匹配上（0-1）。两串都要先 _norm。

    与 _similarity 的分工：ratio 是**对称**的（分母是两边长度之和），
    衡量「这两段是不是同一段文字」；覆盖率是**单向**的，衡量「这一段是不是
    写在那一段里面」。短串被长串包含时覆盖率接近 1 而 ratio 只有 0.3，
    两边粒度不一致的场景全靠这个区分开「包含」与「不相干」。

    autojunk=False 是必须的：默认启发式会把长文本里出现频率超过 1% 的字符
    当作垃圾忽略，而中文题干里「的」「则」「等」正好都超标，
    一开着就会把本来匹配上的部分判掉。
    """
    if not needle or not haystack:
        return 0.0
    sm = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    return sum(blk.size for blk in sm.get_matching_blocks()) / len(needle)


# 学生页把一道题切得比教师页短时的兜底判据（「包含匹配」）。
#
# 实测英语完形填空：教师页 Q12 是整句「Jasper is 12, too. He is a 8 boy and
# he is good to other」，学生页只读出「is a 8 boy」这一小段。它确确实实是
# 那道题，但对称的 ratio 只有 0.3——按 MATCH_THRESHOLD 判就是「题库中无此题」，
# 分值和标准答案一起丢掉。
#
# 打折是为了让真·相似匹配永远优先：一道题若同时有 0.9 的 ratio 匹配和 0.95 的
# 包含匹配，该选前者。折后分数进同一个贪心池，不需要另开一套分配逻辑。
#
# 防噪声靠**唯一性**而不是靠把长度门槛抬高：真实片段可以很短（实测
# 「is a 8 boy」归一化后只有 7 个字符），抬门槛会把真的挡在外面。而短片段
# 真正的危险是「在好几道题干里都能凑出高覆盖」——那种匹配没有区分度，
# 落到哪道题纯看运气。所以只认**唯一**落在一道题库题里的片段，
# 多于一道就整个放弃：宁可这道题报「题库中无此题」让教师看见，
# 也不要把它悄悄配到错的题上，拿别人的标准答案判分。
_CONTAIN_COVERAGE = 0.75
_CONTAIN_MIN_LEN = 6
_CONTAIN_PENALTY = 0.9


# 页面亲和：一个题库常常是多张教师页合并的，而学生页是一页一批。
#
# 实测（英语 6 张教师页合并成 111 题的题库，逐张学生页对齐）：87 道题里有
# 10 道对到了**别页**的题库题上，其中 8 道指向同一张 page_103——那一页有大量
# 很短的题干（「Can I help you?」），短题干和别页的长题干很容易越过 0.55。
# 例如 page_16 的「He always 9 , "Can I help(帮助) you?"」与 page_103 的
# 「Can I help you?」相似度 0.667，而两者的标准答案完全不同。
#
# 这种错配比「题库中无此题」危险得多：界面上它显示为**已匹配**，教师看不出
# 这道题是拿另一道题的标准答案判的分。
#
# 修法是两轮对齐：第一轮先看这张学生页主要落在哪张教师页上，第二轮给那一页的
# 候选加一点分再重排。加成而不是硬性限定在主页内，是因为学生页真的可能跨两张
# 教师页（双面、拼接、上一页续下来的题）——硬限定会把那些正确的跨页匹配砍掉。
# 同理，主页判不出来（命中太少、或分散在多页上）时整个不启用。
_PAGE_AFFINITY_BONUS = 0.12
_PAGE_AFFINITY_MIN_HITS = 2
_PAGE_AFFINITY_MIN_SHARE = 0.5


def _greedy(pairs: list) -> dict:
    """贪心一对一分配：按分数降序，依次锁定还没被占用的两边。

    pairs 的元素是 (排序分, 学生题下标, 题库 qid, 真实相似度)。排序分可能带了
    页面亲和加成，而返回的始终是**真实相似度**——加成只该影响选谁，
    不该影响界面上显示的匹配度。
    """
    taken_s, taken_b = set(), set()
    hits = {}
    for _rank, si, qid, real in pairs:
        if si in taken_s or qid in taken_b:
            continue
        taken_s.add(si)
        taken_b.add(qid)
        hits[si] = (qid, round(real, 3))
    return hits


def _dominant_page(hits: dict, by_qid: dict) -> str:
    """第一轮命中里占多数的那张教师页；判不出主页时返回 ""。"""
    counts = {}
    for qid, _r in hits.values():
        pid = (by_qid.get(qid) or {}).get("page_id") or ""
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    if not counts:
        return ""
    total = sum(counts.values())
    pid, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if n < _PAGE_AFFINITY_MIN_HITS or n < total * _PAGE_AFFINITY_MIN_SHARE:
        return ""
    return pid


def align(student_questions: list, bank_questions: list) -> list:
    """把学生页认出的题对齐到题库题目。

    返回与 student_questions 等长的列表，每项：
        {"index":…, "qid": 命中题库题的 qid 或 None, "score": 相似度}

    用**贪心一对一**：所有候选配对按相似度降序，依次锁定还没被占用的两边。
    不做全局最优匹配（匈牙利算法）——题干相似度这种带噪声的相似度上，
    全局最优的收益远小于它带来的不可解释性：教师问「为什么第 3 题对到了
    第 5 题」，贪心能答「因为它俩相似度 0.81，是全场最高的一对」。

    题库由多张教师页合并时跑两轮，第二轮带页面亲和加成，理由见
    _PAGE_AFFINITY_BONUS 上方。
    """
    pairs = []
    for si, sq in enumerate(student_questions):
        s_raw = sq.get("stem", "")
        s_norm = _norm(s_raw)
        contain_hits = []
        for bq in bank_questions:
            b_raw = bq.get("stem", "")
            ratio = _similarity(s_raw, b_raw)
            if ratio >= MATCH_THRESHOLD:
                pairs.append((ratio, si, bq["qid"], ratio))
            elif len(s_norm) >= _CONTAIN_MIN_LEN:
                # 学生页把这道题切得比教师页短：ratio 上不去，但它整段都写在
                # 那道题库题里面。
                cov = _coverage(s_norm, _norm(b_raw))
                if cov >= _CONTAIN_COVERAGE:
                    contain_hits.append((cov, bq["qid"]))
        # 只认唯一落点。落到多道题库题里的片段没有区分度，整个放弃。
        if len(contain_hits) == 1:
            cov, qid = contain_hits[0]
            score = round(cov * _CONTAIN_PENALTY, 4)
            pairs.append((score, si, qid, score))
    pairs.sort(key=lambda t: (-t[0], t[1]))
    hits = _greedy(pairs)

    by_qid = {q["qid"]: q for q in bank_questions}
    main_page = _dominant_page(hits, by_qid)
    if main_page:
        boosted = [
            (rank + (_PAGE_AFFINITY_BONUS
                     if (by_qid.get(qid) or {}).get("page_id") == main_page else 0.0),
             si, qid, real)
            for rank, si, qid, real in pairs]
        boosted.sort(key=lambda t: (-t[0], t[1]))
        hits = _greedy(boosted)

    out = []
    for si, sq in enumerate(student_questions):
        qid, ratio = hits.get(si, (None, 0.0))
        out.append({"index": sq.get("index", si + 1), "qid": qid, "score": ratio})
    return out


# ---------------------------------------------------------------------------
# 小问归并
# ---------------------------------------------------------------------------
# 一对一对齐解决不了「两边题目粒度不同」这一类，而它在真实作业里是常态：
# 教师答案页按 2(1) 2(2) 2(3) 2(4) 逐条列出答案，学生练习页上第 2 题就是
# 一整块——学生也确实是连着做的，中间没有可切分的边界。
#
# 实测（数学 83 卷，教师页 12 题 / 学生页 8 题）：
#   学生页第 2 题 → 只对上 Q2(=2(1))，相似度 0.580，擦着 0.55 的线过；
#                    Q3 Q4 Q5 三条全部落空
#   学生页第 3 题 → 只对上 Q6(=3(1))；Q7 落空
# 一条短小问跟一条长整题比，SequenceMatcher 的 ratio 天然低（分母是两边
# 长度之和），所以这不是调阈值能救的——降阈值只会让 2(2) 和 2(3) 这种
# 只差一个函数名的小问互相对错行，分照出、证据链照给，只是比错了题。
#
# 归并判据不用 ratio 而用**覆盖率**：题库题干有多大比例的字符能在学生题干里
# 按序找到。短串被长串包含时覆盖率接近 1，而 ratio 只有 0.3——这正是
# 「小问写在整题里」与「两道不相干的题」之间的分界。
_ABSORB_COVERAGE = 0.70   # 题库题干被学生题干覆盖到这个比例，才认为它写在里面
_ABSORB_MIN_LEN = 6       # 太短的题干不参与：「计算：」这种在半页题里都能高覆盖


def absorb_leftovers(student_questions: list, bank_questions: list,
                     alignment: list) -> dict:
    """把一对一没对上的题库题，归并到「题干已经包含了它」的那道学生题上。

    返回 {学生题下标: [(题库题, 覆盖率), …]}，按题库原顺序排列。

    只处理 align() 之后剩下的题库题，所以不会与一对一结果打架。一道题库题
    最多归并到一处（取覆盖率最高的那道学生题），避免同一份分值被算两次。
    """
    taken = {a["qid"] for a in alignment if a["qid"]}
    leftovers = [bq for bq in bank_questions if bq["qid"] not in taken]
    if not leftovers:
        return {}

    by_qid = {q["qid"]: q for q in bank_questions}
    # 每道学生题一对一命中的是哪张教师页。归并**不能跨页**：别页的题是独立的
    # 一道题，不是这道题的小问。实测就栽在这里——page_16 学生页的
    # 「He always 9 , "Can I help(帮助) you?"」正确命中了本页 Q28，却把
    # page_103 上那道独立的短题「Can I help you?」当小问吸了进来（它确实整段
    # 包含在学生题干里），于是那道题的分值和标准答案被并进了不相干的一题。
    hit_page = {}
    for si, a in enumerate(alignment):
        if si < len(student_questions) and a.get("qid"):
            hit_page[si] = (by_qid.get(a["qid"]) or {}).get("page_id") or ""
    # 一对一没命中的学生题按本次对齐的主页兜底。没有这一条，约束对它们等于不存在
    # ——而恰恰是这种题最危险：它自己没有归属，却能靠高覆盖把别页的题吸过来。
    # 实测 page_29 的完形填空首句（本页最像只有 0.341，够不着阈值）就这样
    # 吸走了 page_103 上的题，界面显示它「对上了」page_103 的某道题。
    main_page = _dominant_page(
        {si: (a["qid"], a.get("score", 0.0))
         for si, a in enumerate(alignment) if a.get("qid")}, by_qid)

    s_norm = [_norm(sq.get("stem", "")) for sq in student_questions]
    absorbed = {}
    for bq in leftovers:
        b_norm = _norm(bq.get("stem", ""))
        if len(b_norm) < _ABSORB_MIN_LEN:
            continue
        best_si, best_cov = None, 0.0
        for si, sn in enumerate(s_norm):
            want = hit_page.get(si, main_page)
            if want and (bq.get("page_id") or "") != want:
                continue
            cov = _coverage(b_norm, sn)
            if cov > best_cov:
                best_si, best_cov = si, cov
        if best_si is not None and best_cov >= _ABSORB_COVERAGE:
            absorbed.setdefault(best_si, []).append((bq, round(best_cov, 3)))
    return absorbed


def merge_standard_answers(entries: list) -> str:
    """把归并到同一道学生题的多条标准答案拼成一段，带题号前缀。

    必须带前缀：合并之后判分模型看到的是「一道题、四个答案」，不标出
    哪个答案对应哪一问，它只能猜着配对，而配错的代价是学生被判错。

    entries: [(题库题, 覆盖率), …]，其中第一条通常是一对一命中的那道。
    """
    parts = []
    for bq, _cov in entries:
        ans = (bq.get("standard_answer") or "").strip()
        if not ans:
            continue
        no = (bq.get("no") or "").strip()
        parts.append(("%s %s" % (no, ans)).strip() if no else ans)
    return "\n".join(parts)
