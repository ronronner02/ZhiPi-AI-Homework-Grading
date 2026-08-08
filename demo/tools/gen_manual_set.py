# -*- coding: utf-8 -*-
"""生成「人工走全流程」用的仿真拍照数据集 —— 我不对它跑任何断言。

与 gen_edge_cases.py 的分工：
    gen_edge_cases  造畸形输入（炸弹、截断、越界标注），压测**校验层**；
    gen_manual_set  造**看起来像真实拍照**的作业图，压测**体验链路**。

后者的价值在于：畸形文件在第一道校验就被挡掉，走不到识别与批改；
而真实使用中最常见的其实是「能解码、但拍得不好」——歪、暗、模糊、反光、
手指遮挡。这类图会一路穿到多模态识别与置信度分流，是转写失败的真实来源，
也正是「多模态批改的主要失败发生在转写」这个技术主张要被验证的地方。

刻意不做的事：我不对生成结果跑断言、不看识别输出、不调参数让它好看。
我测过的数据必然带着「我知道它会怎么失败」的偏见，修完一定通过，
那不构成独立证据。这批交给人来点。

产出：
    <out>/photos/            仿真拍照 PNG/JPG，直接在网页上传即可
    <out>/CHECKLIST.md       逐张该看什么、什么算通过、什么算缺陷
    <out>/answer_key.json    每张图的真实作答与人工判分（**先别看**，点完再对）

用法：
    py -3 tools/gen_manual_set.py <输出目录> [--seed 20260806] [--count 14]
"""
import argparse
import io
import json
import random
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
except ImportError:                                    # pragma: no cover
    print("需要 Pillow：py -3 -m pip install pillow")
    raise SystemExit(2)

import gen_sample_images as gsi


# ---------------------------------------------------------------- 拍照劣化

def deg_rotate(img, rng, deg):
    """整体旋转：手持拍照几乎不可能完全水平。用纸色填充空白角。"""
    return img.rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=gsi.PAPER)


def deg_perspective(img, rng, strength=0.06):
    """轻微透视：从斜上方拍，纸面呈梯形。"""
    w, h = img.size
    dx = int(w * strength)
    # 上边窄、下边宽，模拟俯拍
    coeffs = _perspective_coeffs(
        [(0, 0), (w, 0), (w, h), (0, h)],
        [(dx, 0), (w - dx, 0), (w, h), (0, h)])
    return img.transform((w, h), Image.PERSPECTIVE, coeffs,
                         resample=Image.BICUBIC, fillcolor=gsi.PAPER)


def _perspective_coeffs(src, dst):
    """解 8 个透视变换系数（最小二乘）。纯标准库实现，不引入 numpy。"""
    matrix = []
    for (sx, sy), (dx, dy) in zip(src, dst):
        matrix.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        matrix.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    b = []
    for (sx, sy) in src:
        b += [sx, sy]
    # 高斯消元
    n = 8
    a = [row[:] + [b[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[piv] = a[piv], a[col]
        if abs(a[col][col]) < 1e-12:
            return (1, 0, 0, 0, 1, 0, 0, 0)
        for r in range(n):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= f * a[col][c]
    return tuple(a[i][n] / a[i][i] for i in range(n))


def deg_blur(img, rng, radius):
    return img.filter(ImageFilter.GaussianBlur(radius))


def deg_dark(img, rng, factor):
    return ImageEnhance.Brightness(img).enhance(factor)


def deg_low_contrast(img, rng, factor):
    return ImageEnhance.Contrast(img).enhance(factor)


def deg_shadow(img, rng):
    """斜向阴影：手或身体挡住部分光源，最常见的拍照缺陷之一。"""
    w, h = img.size
    overlay = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(overlay)
    d.polygon([(0, 0), (int(w * 0.55), 0), (int(w * 0.25), h), (0, h)], fill=150)
    overlay = overlay.filter(ImageFilter.GaussianBlur(int(w * 0.06)))
    dark = ImageEnhance.Brightness(img).enhance(0.55)
    return Image.composite(img, dark, overlay)


def deg_glare(img, rng):
    """反光高光块：塑料桌面 / 灯管直射，会把一片字盖掉。"""
    w, h = img.size
    overlay = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(overlay)
    cx, cy = int(w * rng.uniform(0.4, 0.75)), int(h * rng.uniform(0.2, 0.5))
    rw, rh = int(w * 0.28), int(h * 0.13)
    d.ellipse([cx - rw, cy - rh, cx + rw, cy + rh], fill=190)
    overlay = overlay.filter(ImageFilter.GaussianBlur(int(w * 0.05)))
    white = Image.new("RGB", (w, h), (255, 255, 255))
    return Image.composite(white, img, overlay)


def deg_noise(img, rng, amount=16):
    """暗光高 ISO 噪点。"""
    w, h = img.size
    px = img.load()
    for _ in range(int(w * h * 0.05)):
        x, y = rng.randrange(w), rng.randrange(h)
        r, g, b = px[x, y]
        n = rng.randint(-amount, amount)
        px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)),
                    max(0, min(255, b + n)))
    return img


def deg_finger(img, rng):
    """手指遮住页面一角 —— 拍作业时极其常见。"""
    w, h = img.size
    d = ImageDraw.Draw(img)
    fx, fy = int(w * 0.86), int(h * 0.80)
    d.ellipse([fx, fy, fx + int(w * 0.22), fy + int(h * 0.28)],
              fill=(196, 158, 132))
    d.ellipse([fx + 12, fy + 10, fx + int(w * 0.20), fy + int(h * 0.24)],
              fill=(214, 176, 150))
    return img


def deg_crop(img, rng, frac=0.12):
    """拍歪导致边缘被裁掉一部分内容。"""
    w, h = img.size
    return img.crop((int(w * frac), 0, w, h))


def deg_desk(img, rng, pad=0.10):
    """把作业纸放到桌面背景上 —— 真实照片不会只有纸。"""
    w, h = img.size
    pw, ph = int(w * (1 + pad * 2)), int(h * (1 + pad * 2))
    canvas = Image.new("RGB", (pw, ph), gsi.DESK)
    canvas.paste(img, (int(w * pad), int(h * pad)))
    return canvas


# 每项：(用例名, 描述, 劣化管道, 期望的人工判断)
# 期望列刻意写成「该看什么」而不是「一定会怎样」——我没测过，不能预言结果。
CASES = [
    ("clean_neat", "工整清晰，正对拍摄", [],
     "应识别准确、置信度高、判绿自动通过"),
    ("desk_bg", "放在桌面上拍，四周有背景", [("desk", {})],
     "背景不应干扰识别；若识别到桌面纹理即为缺陷"),
    ("slight_tilt", "轻微歪斜 3°", [("rotate", {"deg": 3}), ("desk", {})],
     "小角度倾斜应不影响识别"),
    ("tilt_12", "明显歪斜 12°", [("rotate", {"deg": 12}), ("desk", {})],
     "大角度倾斜可能拉低转写置信；关键看是否**如实降低**而非虚高"),
    ("perspective", "斜上方俯拍，纸面梯形", [("perspective", {}), ("desk", {})],
     "透视变形下的识别表现"),
    ("blur_light", "轻微手抖模糊", [("blur", {"radius": 1.1})],
     "轻模糊应仍可识别"),
    ("blur_heavy", "严重失焦", [("blur", {"radius": 3.0})],
     "重模糊应触发低置信 → 转人工，而不是硬猜一个答案"),
    ("dark", "光线不足", [("dark", {"factor": 0.5}), ("noise", {"amount": 20})],
     "暗光下的识别；置信度应下降"),
    ("low_contrast", "铅笔淡写 + 低对比", [("low_contrast", {"factor": 0.45})],
     "淡笔迹是真实痛点，看是否漏字"),
    ("shadow", "斜向阴影遮挡", [("shadow", {}), ("desk", {})],
     "阴影区的字是否被漏掉"),
    ("glare", "灯光反射高光块", [("glare", {})],
     "高光盖住的字应被标为无法辨认〔?〕，而不是编造内容"),
    ("finger", "手指遮住右下角", [("finger", {}), ("desk", {})],
     "被遮内容不应被凭空补全"),
    ("edge_cropped", "拍歪导致左侧被裁", [("crop", {"frac": 0.14})],
     "内容缺失时是否如实反映"),
    ("worst_case", "歪 + 暗 + 模糊 + 噪点叠加", [
        ("rotate", {"deg": -8}), ("dark", {"factor": 0.58}),
        ("blur", {"radius": 1.6}), ("noise", {"amount": 22}), ("desk", {})],
     "最差情况：**必须**判红转人工。若给出高置信的具体分数，即为严重缺陷"),
]

PIPELINE = {
    "rotate": deg_rotate, "perspective": deg_perspective, "blur": deg_blur,
    "dark": deg_dark, "low_contrast": deg_low_contrast, "shadow": deg_shadow,
    "glare": deg_glare, "noise": deg_noise, "finger": deg_finger,
    "crop": deg_crop, "desk": deg_desk,
}


def human_score(sub, rubric):
    return sum(step["max_score"]
               for ann, step in zip(sub.get("step_annotations", []), rubric)
               if ann.get("is_correct"))


def main() -> int:
    ap = argparse.ArgumentParser(description="生成人工走查用的仿真拍照数据集")
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=20260806)
    ap.add_argument("--count", type=int, default=len(CASES))
    args = ap.parse_args()

    questions = {q["question_id"]: q
                 for q in gsi._load("questions.json")["questions"]}
    submissions = gsi._load("submissions.json")["submissions"]

    out = Path(args.out_dir)
    photos = out / "photos"
    photos.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    key = []
    cases = CASES[: max(1, args.count)]

    for idx, (name, desc, steps, expect) in enumerate(cases):
        # 轮换取不同学科的作答，避免整批都是同一道数学题
        sub = submissions[idx % len(submissions)]
        question = questions[sub["question_id"]]
        img = gsi.render_page(question, sub)

        for step_name, kw in steps:
            fn = PIPELINE[step_name]
            img = fn(img, rng, **kw)

        # 一半存 JPEG（真实手机拍照就是 JPEG，带压缩伪影）
        ext = "jpg" if idx % 2 else "png"
        fname = "%02d_%s.%s" % (idx + 1, name, ext)
        path = photos / fname
        if ext == "jpg":
            img.convert("RGB").save(path, "JPEG", quality=rng.randint(72, 88))
        else:
            img.save(path, "PNG", optimize=True)

        key.append({
            "file": fname,
            "case": name,
            "description": desc,
            "what_to_check": expect,
            "student_name": sub["student_name"],
            "question_id": sub["question_id"],
            "subject": question["subject"],
            "truth_text": sub["ocr"]["text"],
            "human_score": human_score(sub, question["rubric"]),
            "max_score": question["max_score"],
            "size_kb": path.stat().st_size // 1024,
        })
        print("生成 %-28s %s  %d KB" % (fname, desc, path.stat().st_size // 1024))

    io.open(out / "answer_key.json", "w", encoding="utf-8", newline="\n").write(
        json.dumps(key, ensure_ascii=False, indent=2))

    _write_checklist(out, key, args.seed)
    print()
    print("已生成 %d 张仿真拍照 → %s" % (len(key), photos))
    print("先读 CHECKLIST.md 逐张点，点完再翻 answer_key.json 对答案。")
    return 0


def _write_checklist(out: Path, key: list, seed: int):
    L = [
        "# 人工走查清单（仿真拍照集，seed=%d）" % seed,
        "",
        "**这批数据我没有测过**，也没有看过识别结果。生成器只负责造图，",
        "不对结果做任何断言——我测过的数据必然带着「我知道它会怎么失败」的偏见，",
        "修完一定通过，那不构成独立证据。你点出来的问题才是真问题。",
        "",
        "> 图片是程序合成的仿手写 + 仿拍照劣化，**不是真实学生笔迹**。",
        "> 它验证的是「链路对差图的反应」，不能用来给产品准确率背书。",
        "",
        "## 准备",
        "",
        "```bash",
        "cd demo",
        "# 需要真实多模态 Key，否则上传任意照片只会提示「只能识别内置样例」",
        "py -3 app.py            # http://127.0.0.1:8010",
        "```",
        "",
        "配置检查：01 屏的上传提示应是「支持 PNG / JPG / WebP…」；",
        "若显示「当前为离线演示模式」，说明 VLM Key 没生效，先修配置再走查。",
        "",
        "## 全链路四步（每张图都走一遍）",
        "",
        "1. **01 拍照提交** → 上传照片 → 看转写文本",
        "2. **02 批改结果** → 看总分、置信度、红黄绿分流、证据链",
        "3. **03 教师工作台** → 找到这份 → 改一次分或错因 → 提交终审",
        "4. **04 班级看板** → 确认这份计入了统计与错因分布",
        "",
        "## 每张图该看什么",
        "",
        "| # | 文件 | 拍照缺陷 | 重点看 |",
        "| --- | --- | --- | --- |",
    ]
    for i, k in enumerate(key, 1):
        L.append("| %d | `%s` | %s | %s |"
                 % (i, k["file"], k["description"], k["what_to_check"]))

    L += [
        "",
        "## 判定原则（比单张对错更重要）",
        "",
        "这套系统的核心主张是「**教师可控**」与「转写置信 × 评分置信分离」，",
        "所以走查时最该盯的不是「识别得准不准」，而是下面四条：",
        "",
        "1. **认不出要承认。** 模糊 / 反光 / 遮挡的部分应转写成 〔?〕 或明确留空，",
        "   并把置信度**如实压低**。最严重的缺陷是「看不清却编造出一个具体答案",
        "   还给高置信」——那会让老师误以为可以直接采信。",
        "2. **差图必须转人工。** `14_worst_case` 这类叠加劣化的，应落到红桶。",
        "   若它被判绿自动通过，是安全性缺陷，比分数算错严重得多。",
        "3. **教师修正必须生效。** 在 02 屏手动改转写文本后再提交批改，",
        "   分数应随修正后的文本变化。若改了没反应，说明走的是样例匹配、",
        "   而不是真实识别链路，「教师可控」就是空话。",
        "4. **终审要能回灌。** 03 屏改完一份后，同题其他未终审作答的置信度",
        "   应实时变化（设计方案 §9.7 的数据飞轮）。不变即回路断了。",
        "",
        "## 边界情况顺手一起点",
        "",
        "- 同一张图**连上传两次** → 第二次应正常，不应重复计数或串号",
        "- 上传后**立刻刷新页面** → 会话应还在（cookie 隔离），已上传件不丢",
        "- 点右上角**重置演示** → 应清空你自己的终审与上传，且不影响他人",
        "- **换一个浏览器**（或隐身窗口）打开 → 两个会话的终审记录必须互不污染",
        "- 上传一个**非图片文件**（随便找个 .txt 改名成 .png）→ 应给出可读提示，",
        "  而不是报 500 或界面卡死",
        "",
        "## 记录问题",
        "",
        "发现异常请记下：**哪张图、第几步、界面显示了什么、你期望是什么**。",
        "截图最好。这四项齐了才能定位，只说「识别不准」没法查。",
        "",
        "点完全部再打开 `answer_key.json` 对答案——里面有每张图的真实作答文本",
        "与人工判分。先看答案会影响你对置信度是否合理的判断。",
    ]
    io.open(out / "CHECKLIST.md", "w", encoding="utf-8", newline="\n").write(
        "\n".join(L) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
