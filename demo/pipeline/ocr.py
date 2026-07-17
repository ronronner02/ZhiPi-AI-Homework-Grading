"""OCR 转写模块。

对应总体流程（详细设计方案 §7.1）中的
「OCR / 公式 / 图形识别」环节。

Demo 采用 mock 转写：直接读取内置作答数据里预置的转写文本与清晰度分，
模拟真实 OCR 的输出结构，使全流程在无任何外部依赖时也能离线跑通。
真实接入时，只需实现 recognize()，返回同样的 {"text", "clarity"} 结构即可，
其余批改、置信度、分流、学情逻辑无需改动。
"""


def mock_ocr(submission: dict) -> dict:
    """从内置作答数据取预置转写文本与清晰度分。

    参数：
        submission: 单份作答记录（来自 data/submissions.json）。
    返回：
        {"text": 转写文本, "clarity": 清晰度(0-100)}
    """
    ocr = submission.get("ocr", {})
    return {
        "text": ocr.get("text", ""),
        "clarity": float(ocr.get("clarity", 0)),
    }


def recognize(image_path: str) -> dict:
    """真实 OCR 接口（预留，Demo 未接入）。

    生产环境应在此调用 PaddleOCR / Pix2Text / 多模态大模型，
    先做图像预处理与版面分析，再对手写文字、公式、单位进行识别，
    返回与 mock_ocr 一致的 {"text": ..., "clarity": ...} 结构，
    以便下游批改流程保持不变。

    参数：
        image_path: 作业图片路径。
    """
    raise NotImplementedError("真实 OCR 未接入，Demo 使用 mock_ocr 读取预置转写")
