"""pipeline 包：作业批改流水线。

包含四个环节：
- ocr        转写识别（Demo 为 mock 转写，预留真实 OCR 接口）
- grader     Rubric 逐步批改（规则引擎 + 可选真实 LLM 分支）
- confidence 置信度加权计算与红黄绿分流（详细设计方案 §9.7）
- analytics  班级学情聚合（知识点错误率、错因分布、讲评建议）
"""
