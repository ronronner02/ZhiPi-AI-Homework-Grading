# -*- coding: utf-8 -*-
"""PDF 作业转页图。

真实作业不只有手机拍的照片：测评数据里语文、英语的整份作业就是扫描成 PDF
交上来的（每页一张整页扫描图，没有文字层）。识别链路只认图片字节，所以在
入口处把 PDF 摊成「一页一张图」，下游一行都不用改。
"""
import fitz

# 一次最多摊多少页。整份作业十几页很常见，但每页都要送多模态识别，
# 页数不设上限等于把一次误传变成几十次模型调用。
MAX_PAGES = 20

# 渲染 DPI。150 是识别手写的经验下限：再低连笔的字会糊成一团，
# 再高单页体积翻倍，识别质量却不再上升（扫描件本身就是 150-200 DPI）。
RENDER_DPI = 150

# 输出用 JPEG 而不是 PNG。实测这批真实 PDF 单页 150 DPI 是
# PNG 1.1-1.4MB / JPEG(88) 0.23-0.41MB——三到四倍差距。页图要在会话里
# 留着（批改痕迹要画回原图），PNG 会让内存占用成为第一个撑不住的地方；
# 而扫描件本来就是有损压缩过的照片，再存一遍无损没有意义。
JPEG_QUALITY = 88
PAGE_MIME = "image/jpeg"


def is_pdf(data: bytes) -> bool:
    """按文件头判断是不是 PDF，不看扩展名——扩展名是调用方给的，不可信。"""
    return bool(data) and data[:5] == b"%PDF-"


def render_pages(data: bytes, dpi: int = RENDER_DPI, max_pages: int = MAX_PAGES):
    """把 PDF 每页渲染成 JPEG 字节，返回 ([(页码从1起, jpeg_bytes), ...], 总页数)。

    抛 RuntimeError 表示「这份 PDF 用不了」，消息直接面向体验者。
    返回的页数可能少于总页数（超出 max_pages），由调用方决定怎么交代。
    """
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise RuntimeError("PDF 无法打开（文件可能损坏或加密）：%s" % exc)

    with doc:
        if doc.needs_pass:
            raise RuntimeError("这份 PDF 有打开密码，请先解除密码再上传。")
        total = doc.page_count
        if total <= 0:
            raise RuntimeError("这份 PDF 没有任何页面。")
        pages = []
        for i in range(min(total, max_pages)):
            pix = doc.load_page(i).get_pixmap(dpi=dpi)
            pages.append((i + 1, pix.tobytes("jpg", jpg_quality=JPEG_QUALITY)))
    return pages, total


def page_count(data: bytes) -> int:
    """只读页数，不渲染——前端选完文件就要显示「共 N 页」，没必要先渲染。"""
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return 0
