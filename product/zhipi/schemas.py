from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    access_code: str = Field(min_length=1, max_length=256)


class GradeRequest(StrictModel):
    submission_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    transcript: str | None = Field(default=None, max_length=12000)


class RecognizeRequest(StrictModel):
    submission_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ReviewRequest(StrictModel):
    submission_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    teacher_action: Literal["confirmed", "modified"] = "confirmed"
    final_score: float | None = Field(default=None, ge=0, le=1000)
    final_error_tags: list[str] | None = Field(default=None, max_length=20)
    final_comment: str | None = Field(default=None, max_length=500)
    final_dimension_scores: dict[str, float] | None = None


class UploadRequest(StrictModel):
    filename: str = Field(min_length=1, max_length=160)
    content_type: str = Field(default="application/octet-stream", max_length=80)
    data_base64: str = Field(min_length=4)
    student_name: str = Field(default="匿名学生", max_length=40)
    student_id: str | None = Field(default=None, max_length=64)
    class_id: str = Field(default="C001", max_length=64)
    question_id: str | None = Field(default=None, max_length=64)
    question_text: str | None = Field(default=None, max_length=4000)
    subject: str | None = Field(default=None, max_length=16)
    transcript: str | None = Field(default=None, max_length=12000)
    folder_id: str = Field(default="inbox", max_length=64)

    @model_validator(mode="after")
    def question_source_present(self):
        if not self.question_id and not (self.question_text and self.question_text.strip()):
            raise ValueError("必须选择题目，或填写题面原文")
        return self


class FolderCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=40)
