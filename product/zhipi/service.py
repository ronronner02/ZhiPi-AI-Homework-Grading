from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from . import analytics
from .config import ROOT_DIR, Settings
from .db import Database
from .grading import ERROR_TAGS, grade_seed, manual_result, normalize_llm_result
from .llm import LLMGrader, VLMRecognizer
from .schemas import FolderCreateRequest, UploadRequest
from .storage import FileStorage


class NotFoundError(LookupError):
    pass


class ConflictError(ValueError):
    pass


class QuotaError(RuntimeError):
    pass


_FOLDER_NAME = re.compile(r"^[\w\u4e00-\u9fff .·（）()_-]{1,40}$")


def _open_question(row: dict) -> dict:
    subject = row.get("subject") or "其他"
    return {
        "question_id": None,
        "subject": subject,
        "title": row.get("question_title") or "自定义题目",
        "question_text": row.get("question_text") or "",
        "standard_answer": "",
        "knowledge_points": [],
        "max_score": 15,
        "rubric": [
            {"step": "题意理解", "max_score": 3, "knowledge_point": ""},
            {"step": "方法选择", "max_score": 3, "knowledge_point": ""},
            {"step": "运算或语言执行", "max_score": 4, "knowledge_point": ""},
            {"step": "步骤与表达完整", "max_score": 3, "knowledge_point": ""},
            {"step": "最终结论", "max_score": 2, "knowledge_point": ""},
        ],
    }


class ProductService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.storage = FileStorage(settings)
        self.llm = LLMGrader(settings)
        self.vlm = VLMRecognizer(settings)

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db.initialize()
        seed_dir = ROOT_DIR / "data"
        questions = json.loads((seed_dir / "questions.json").read_text(encoding="utf-8"))[
            "questions"
        ]
        submissions = json.loads(
            (seed_dir / "submissions.json").read_text(encoding="utf-8")
        )["submissions"]
        self.db.seed_builtin(self.settings.workspace_id, questions, submissions, grade_seed)

    def config(self) -> dict:
        return {
            "product": "智批π",
            "version": "1.0.0",
            "environment": self.settings.environment,
            "workspace_id": self.settings.workspace_id,
            "mode": "llm" if self.settings.llm_enabled else "mock",
            "llm_enabled": self.settings.llm_enabled,
            "vlm_enabled": self.settings.vlm_enabled,
            "access_code_required": bool(self.settings.access_code),
            "upload_policy": {
                "max_image_mb": self.settings.max_image_bytes // 1024 // 1024,
                "formats": ["JPG", "PNG", "WebP"],
            },
            "quota": self.db.quota_snapshot(
                self.settings.workspace_id,
                {"vlm": self.settings.vlm_daily_limit, "llm": self.settings.llm_daily_limit},
            ),
            "persistence": "sqlite+filesystem",
        }

    def questions(self) -> list[dict]:
        return self.db.list_questions(self.settings.workspace_id)

    def submissions(self) -> list[dict]:
        rows = self.db.list_submissions(self.settings.workspace_id)
        reviews = self.db.list_reviews(self.settings.workspace_id)
        output = []
        for row in rows:
            result = row.get("ai_result")
            review = reviews.get(row["submission_id"])
            item = {
                "submission_id": row["submission_id"],
                "source": row["source"],
                "state": row["state"],
                "student_id": row.get("student_id"),
                "student_name": row["student_name"],
                "class_id": row["class_id"],
                "question_id": row.get("question_id"),
                "question_title": row["question_title"],
                "question_text": row["question_text"],
                "subject": row["subject"],
                "ocr_text": row["ocr_text"],
                "ocr_clarity": row["ocr_clarity"],
                "file_id": row.get("file_id"),
                "file_url": f"/api/files/{row['file_id']}" if row.get("file_id") else None,
                "folder_id": row.get("folder_id"),
                "result": result,
                "reviewed": review is not None,
                "review": review,
            }
            if result:
                item.update(
                    {
                        "total_score": result.get("total_score"),
                        "max_score": result.get("max_score"),
                        "confidence": result.get("confidence"),
                        "status": result.get("status"),
                        "final_score": review["final_score"] if review else result.get("total_score"),
                    }
                )
            output.append(item)
        return output

    def result(self, submission_id: str) -> dict:
        row = self.db.get_submission(self.settings.workspace_id, submission_id)
        if not row:
            raise NotFoundError("作业不存在")
        if not row.get("ai_result"):
            raise ConflictError("作业尚未批改，请先补充学生作答转写")
        return row["ai_result"]

    def _question_for(self, row: dict) -> dict:
        if row.get("question_id"):
            question = self.db.get_question(self.settings.workspace_id, row["question_id"])
            if question:
                return question
        return _open_question(row)

    def grade(self, submission_id: str, transcript: str | None = None) -> dict:
        row = self.db.get_submission(self.settings.workspace_id, submission_id)
        if not row:
            raise NotFoundError("作业不存在")
        if row["source"] == "builtin" and transcript is None:
            return row["ai_result"]
        text = (transcript if transcript is not None else row.get("ocr_text") or "").strip()
        if not text:
            raise ConflictError("缺少学生作答转写；请先人工校正识别文本")
        question = self._question_for(row)
        if self.settings.llm_enabled:
            if not self.db.take_quota(
                self.settings.workspace_id, "llm", self.settings.llm_daily_limit
            ):
                raise QuotaError("今日真实批改额度已用尽，请明日再试或联系管理员")
            raw = self.llm.grade(question, text)
            result = normalize_llm_result(raw, question, text, float(row.get("ocr_clarity") or 0))
            state = "graded"
        else:
            result = manual_result(question, text, float(row.get("ocr_clarity") or 0))
            state = "manual_review"
        result.update(
            {
                "submission_id": submission_id,
                "question_id": row.get("question_id"),
                "question_title": row["question_title"],
                "question_text": row["question_text"],
                "subject": row["subject"],
                "student_id": row.get("student_id"),
                "student_name": row["student_name"],
                "class_id": row["class_id"],
                "ocr_text": text,
                "source": row["source"],
            }
        )
        self.db.update_submission_result(
            self.settings.workspace_id, submission_id, state, text, result
        )
        return result

    def upload(self, request: UploadRequest) -> dict:
        folder_ids = {row["folder_id"] for row in self.db.list_folders(self.settings.workspace_id)}
        if request.folder_id not in folder_ids:
            raise NotFoundError("目标文件夹不存在")
        question = None
        if request.question_id:
            question = self.db.get_question(self.settings.workspace_id, request.question_id)
            if not question:
                raise NotFoundError("所选题目不存在")
        file_row = self.storage.save_image(
            self.settings.workspace_id, request.filename, request.data_base64
        )
        submission_id = "UP-" + uuid.uuid4().hex[:16]
        base = {
            "submission_id": submission_id,
            "state": "awaiting_transcription",
            "question_id": request.question_id,
            "student_id": request.student_id,
            "student_name": request.student_name or "匿名学生",
            "class_id": request.class_id,
            "subject": (question or {}).get("subject") or request.subject or "其他",
            "question_title": (question or {}).get("title") or "自定义题目",
            "question_text": (question or {}).get("question_text") or request.question_text or "",
            "ocr_text": request.transcript or "",
            "ocr_clarity": 100 if request.transcript else 0,
            "folder_id": request.folder_id,
            "ai_result": None,
        }
        try:
            self.db.create_file_and_submission(self.settings.workspace_id, file_row, base)
        except Exception:
            self.storage.delete(file_row["storage_path"])
            raise
        result = None
        if request.transcript:
            result = self.grade(submission_id, request.transcript)
        return {
            "submission_id": submission_id,
            "file_id": file_row["file_id"],
            "file_url": f"/api/files/{file_row['file_id']}",
            "state": "graded" if result and result.get("mode") == "llm" else (
                "manual_review" if result else "awaiting_transcription"
            ),
            "result": result,
        }

    def file(self, file_id: str) -> tuple[Path, dict]:
        row = self.db.get_file(self.settings.workspace_id, file_id)
        if not row:
            raise NotFoundError("文件不存在")
        path = self.storage.resolve(row["storage_path"])
        if not path:
            raise NotFoundError("文件记录存在，但文件已丢失")
        return path, row

    def recognize(self, submission_id: str) -> dict:
        row = self.db.get_submission(self.settings.workspace_id, submission_id)
        if not row:
            raise NotFoundError("作业不存在")
        if not row.get("file_id"):
            raise ConflictError("这份作业没有可识别的原图")
        if not self.settings.vlm_enabled:
            raise ConflictError("未配置多模态识别模型；请人工填写学生作答转写")
        if not self.db.take_quota(
            self.settings.workspace_id, "vlm", self.settings.vlm_daily_limit
        ):
            raise QuotaError("今日图片识别额度已用尽，请明日再试或联系管理员")
        path, meta = self.file(row["file_id"])
        recognized = self.vlm.recognize(path.read_bytes(), meta["content_type"])
        # 已绑定题库题时保留教师题面与学科；自定义题才采用模型识别出的题面。
        subject = None if row.get("question_id") else recognized.get("subject")
        question_text = None if row.get("question_id") else recognized.get("question_text")
        self.db.update_recognition(
            self.settings.workspace_id,
            submission_id,
            recognized["text"],
            recognized["clarity"],
            subject,
            question_text,
        )
        return {"submission_id": submission_id, **recognized, "state": "recognized"}

    def review(self, payload: dict, request_id: str = "") -> dict:
        row = self.db.get_submission(self.settings.workspace_id, payload["submission_id"])
        if not row:
            raise NotFoundError("作业不存在")
        result = row.get("ai_result")
        if not result:
            raise ConflictError("作业尚未进入批改/人工终审阶段")
        max_score = float(result.get("max_score") or 0)
        ai_score = float(result.get("total_score") or 0)
        final_score = ai_score if payload.get("final_score") is None else float(payload["final_score"])
        if not 0 <= final_score <= max_score:
            raise ValueError(f"终审分数须在 0 到 {max_score:g} 之间")
        final_tags = payload.get("final_error_tags")
        if final_tags is not None:
            illegal = [tag for tag in final_tags if tag not in ERROR_TAGS]
            if illegal:
                raise ValueError("非法错因标签：" + "、".join(illegal))
            final_tags = list(dict.fromkeys(final_tags))
        final_dimensions = payload.get("final_dimension_scores")
        if final_dimensions is not None:
            allowed = {
                str(step.get("step")): float(step.get("max_score") or 0)
                for step in result.get("step_analysis") or []
            }
            cleaned = {}
            for key, raw in final_dimensions.items():
                if key not in allowed:
                    raise ValueError(f"未知评分维度：{key}")
                value = float(raw)
                if not 0 <= value <= allowed[key]:
                    raise ValueError(f"维度「{key}」分数须在 0 到 {allowed[key]:g} 之间")
                cleaned[key] = value
            final_dimensions = cleaned
        record = {
            "submission_id": payload["submission_id"],
            "teacher_action": payload.get("teacher_action", "confirmed"),
            "ai_score": ai_score,
            "final_score": final_score,
            "max_score": max_score,
            "final_error_tags": final_tags,
            "final_comment": payload.get("final_comment") or "",
            "final_dimension_scores": final_dimensions,
        }
        self.db.upsert_review(self.settings.workspace_id, record, request_id)
        return record

    def class_analytics(self, class_id: str = "C001") -> dict:
        rows = [
            row
            for row in self.db.list_submissions(self.settings.workspace_id)
            if row["class_id"] == class_id
        ]
        return analytics.aggregate(rows, self.db.list_reviews(self.settings.workspace_id), class_id)

    def students(self) -> list[dict]:
        seen = {}
        for row in self.db.list_submissions(self.settings.workspace_id):
            key = row.get("student_id") or row.get("student_name")
            if key:
                seen[key] = row.get("student_name") or key
        return [{"student_id": key, "student_name": name} for key, name in sorted(seen.items())]

    def student(self, student_id: str) -> dict:
        result = analytics.student_profile(
            student_id,
            self.db.list_submissions(self.settings.workspace_id),
            self.db.list_reviews(self.settings.workspace_id),
        )
        if not result:
            raise NotFoundError("学生不存在")
        return result

    def lecture_outline(self, class_id: str = "C001") -> str:
        data = self.class_analytics(class_id)
        lines = [
            f"# {class_id} 作业讲评提纲",
            "",
            f"- 已批改：{data['result_count']} 份",
            f"- 已终审：{data['reviewed_count']} 份",
            f"- 平均得分率：{data['average_score_pct']}%",
            "",
            "## 重点讲评",
        ]
        for index, item in enumerate(data["weak_knowledge_points"][:3], 1):
            lines.append(f"{index}. {item['name']}（错误率 {item['error_rate']}%）")
        if not data["weak_knowledge_points"]:
            lines.append("1. 暂无足够的知识点统计数据")
        lines.extend(["", "## 教学建议"])
        lines.extend(f"- {item}" for item in data["teaching_suggestions"])
        lines.extend(["", "## 课末复测", "- 选择最高频错因对应的同构题，限时 5 分钟完成并当堂订正。"])
        return "\n".join(lines)

    def folders(self) -> list[dict]:
        return self.db.list_folders(self.settings.workspace_id)

    def create_folder(self, request: FolderCreateRequest) -> dict:
        name = re.sub(r"\s+", " ", request.name).strip()
        if not _FOLDER_NAME.fullmatch(name):
            raise ValueError("文件夹名仅允许中英文、数字、空格和常见分隔符")
        if len(self.db.list_folders(self.settings.workspace_id)) >= 64:
            raise ConflictError("文件夹数量已达上限 64 个")
        try:
            return self.db.create_folder(
                self.settings.workspace_id, "FD-" + uuid.uuid4().hex[:12], name
            )
        except sqlite3.IntegrityError:
            raise ConflictError("已存在同名文件夹") from None

    def reset_uploads(self) -> int:
        paths = self.db.reset_workspace_uploads(self.settings.workspace_id)
        for path in paths:
            self.storage.delete(path)
        return len(paths)
