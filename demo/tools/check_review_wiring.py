# -*- coding: utf-8 -*-
"""端到端验证：识别层的元信号有没有真的走到批改层。

这条链上曾经断了一整段——识别侧算出了 overflow / attribution_confidence /
refined / legible_hint，服务端 _clean_page_questions 建好了接收位，批改端写好了
消费逻辑，唯独前端 app.js 在拼送批载荷时按一份手写的六字段清单重建对象，把它们
全丢了。三处各自看都是对的，合起来等于没做。

所以这个脚本刻意**按前端真实发出的字段清单**构造请求（与 app.js 的 gradeItem
保持一致），跑完整 upload → recognize → grade 链路，检查：

  1. 识别返回里带没带这些字段
  2. 批改结果里有没有 needs_review / review_flags
  3. 有可疑题时，绿灯有没有被压成黄灯

它验的是**接线**，不是准确率——准确率看 tools/ab_recognize.py。

    py -3 tools/check_review_wiring.py
"""
import base64
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402  （副作用：按 app.py 的规则加载 .env）
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DATA = Path(r"E:\希沃智教π\测评数据\测评数据\数学\学生页")
CASES = {
    # 越界作答：验的是归属字段有没有走通
    "89_student_2": _DATA / "89_student_2.jpg",
    # 分式与潦草字：这一页会触发多次复识改写，验的是「读不稳 → 不自动通过」
    "hardwork_117": _DATA / "hardwork_117.jpg",
}
IMG = CASES[sys.argv[1]] if len(sys.argv) > 1 and sys.argv[1] in CASES \
    else CASES["89_student_2"]
MAX_EDGE = 1800          # 与前端 compressImage / run_real_eval 对齐

# 前端 app.js gradeItem() 回传的字段清单。两边必须一致——这里多写一个字段，
# 测出来的「通了」在真实浏览器里就是假的。
FRONTEND_FIELDS = ("no", "subject", "stem", "answer", "printed_max_score", "bbox",
                   "qtype", "overflow", "attribution_confidence",
                   "refined", "refine_changed", "refine_reason", "legible_hint")

FAIL = []


def check(cond, label):
    print("  %s %s" % ("ok  " if cond else "FAIL", label))
    if not cond:
        FAIL.append(label)


def shrink(raw):
    with Image.open(io.BytesIO(raw)) as im:
        if max(im.size) <= MAX_EDGE and len(raw) < 6_000_000:
            return raw
        k = MAX_EDGE / max(im.size)
        out = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                       Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def main():
    client = TestClient(demo_app.app)
    code = os.environ.get("ZHIPI_ACCESS_CODE", "").strip()
    if code:
        client.cookies.set("zhipi_pass", code)
    client.get("/api/demo/config")

    print("\n[1] 上传与识别")
    up = client.post("/api/upload-pages", json={
        "image_base64": base64.b64encode(shrink(IMG.read_bytes())).decode(),
        "mime": "image/jpeg", "filename": IMG.name, "role": "student"})
    check(up.status_code == 200, "上传返回 200（%d）" % up.status_code)
    page_id = up.json()["pages"][0]["page_id"]

    rec = client.post("/api/recognize-image", json={"page_id": page_id})
    check(rec.status_code == 200, "识别返回 200（%d）" % rec.status_code)
    r = rec.json()
    qs = r.get("questions") or []
    check(bool(qs), "识别出题目（%d 题）" % len(qs))
    print("     引擎=%s staged=%s 复识=%d 题"
          % (r.get("engine"), r.get("staged"), len(r.get("refined") or [])))

    present = {f for q in qs for f in FRONTEND_FIELDS if q.get(f) not in (None, "", False)}
    print("     识别结果里出现的元字段：%s" % (sorted(present) or "（无）"))
    # 逐题转写也打出来：这条链路走的是前端 compressImage 同款的 1800px 压缩图，
    # 与 tools/ab_recognize.py 直接喂原图不同，识别质量可能有差。
    for q in qs:
        print("     %-4s %-4s%s %s"
              % (q.get("no") or "?", q.get("qtype") or "",
                 " 改写" if q.get("refine_changed") else "",
                 (q.get("answer") or "(空)").replace("\n", " ⏎ ")[:66]))

    print("\n[2] 按前端真实载荷送批")
    payload = [{f: q.get(f) for f in FRONTEND_FIELDS} for q in qs]
    for it in payload:                      # 与 gradeItem 一致：answer 兜底空串
        it["answer"] = it.get("answer") or ""
    gr = client.post("/api/grade-page", json={
        "page_id": page_id, "questions": payload,
        "subject": r.get("subject") or "数学", "clarity": r.get("clarity", 75)})
    check(gr.status_code == 200, "批改返回 200（%d）" % gr.status_code)
    if gr.status_code != 200:
        print(gr.text[:500])
        return 1
    g = gr.json()

    print("\n[3] 待复核字段")
    check("needs_review" in g, "结果里有 needs_review 字段")
    check("review_flags" in g, "结果里有 review_flags 字段")
    print("     总分 %s/%s  置信度 %s  分流 %s  needs_review=%s"
          % (g.get("total_score"), g.get("max_score"), g.get("confidence"),
             g.get("status"), g.get("needs_review")))
    for f in g.get("review_flags") or []:
        print("     待复核：第 %s 题 · %s" % (f.get("no"), f.get("reason")))
    if g.get("needs_review"):
        check(g.get("status") != "green", "有待复核题时不得绿灯自动通过")
        check("复核" in (g.get("teacher_note") or ""), "教师备注里点名了待复核题")
    else:
        print("     （这一页没有可疑题，下面用注入的方式验证压绿灯与点名）")

    print("\n[4] 注入一道「模型说看不清」的题，验证后置防线真的落到 HTTP 结果上")
    # 单元测试只证明 _review_flags 这个函数判得对，证明不了「判出来之后有没有人
    # 用它」——防线断在 grade_page 里同样是一行 ok 都不会红的。这里改一个字段
    # 重走一次真实批改，看分流与备注是否随之改变。
    injected = [dict(it) for it in payload]
    injected[0]["legible_hint"] = False
    gr2 = client.post("/api/grade-page", json={
        "page_id": page_id, "questions": injected,
        "subject": r.get("subject") or "数学", "clarity": r.get("clarity", 75)})
    check(gr2.status_code == 200, "注入后批改返回 200（%d）" % gr2.status_code)
    if gr2.status_code == 200:
        g2 = gr2.json()
        print("     置信度 %s  分流 %s  needs_review=%s"
              % (g2.get("confidence"), g2.get("status"), g2.get("needs_review")))
        check(g2.get("needs_review") is True, "注入后 needs_review=True")
        check(g2.get("status") != "green", "注入后不再绿灯（%s）" % g2.get("status"))
        check(any(f.get("reason") == "字迹辨认不出"
                  for f in g2.get("review_flags") or []), "review_flags 里给出了原因")
        check("复核" in (g2.get("teacher_note") or ""), "教师备注点名了待复核题")
        print("     备注：%s" % (g2.get("teacher_note") or "")[-60:])

    print("\n" + "=" * 60)
    if FAIL:
        print("失败 %d 项：%s" % (len(FAIL), "；".join(FAIL)))
        return 1
    print("接线验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
