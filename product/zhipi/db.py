from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class Database:
    """每次操作使用短连接，支持 FastAPI 线程池和多个 uvicorn worker。"""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """显式关闭短连接；sqlite 的连接上下文只提交事务，并不会 close。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS questions (
                    workspace_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, question_id)
                );

                CREATE TABLE IF NOT EXISTS folders (
                    workspace_id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    is_system INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, folder_id),
                    UNIQUE (workspace_id, name)
                );

                CREATE TABLE IF NOT EXISTS files (
                    workspace_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, file_id)
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    workspace_id TEXT NOT NULL,
                    submission_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    question_id TEXT,
                    student_id TEXT,
                    student_name TEXT NOT NULL,
                    class_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    question_title TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    ocr_clarity REAL NOT NULL,
                    file_id TEXT,
                    folder_id TEXT,
                    ai_result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, submission_id),
                    FOREIGN KEY (workspace_id, question_id)
                        REFERENCES questions(workspace_id, question_id),
                    FOREIGN KEY (workspace_id, file_id)
                        REFERENCES files(workspace_id, file_id),
                    FOREIGN KEY (workspace_id, folder_id)
                        REFERENCES folders(workspace_id, folder_id)
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    workspace_id TEXT NOT NULL,
                    submission_id TEXT NOT NULL,
                    teacher_action TEXT NOT NULL,
                    ai_score REAL NOT NULL,
                    final_score REAL NOT NULL,
                    max_score REAL NOT NULL,
                    final_error_tags TEXT,
                    final_comment TEXT NOT NULL,
                    final_dimension_scores TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, submission_id),
                    FOREIGN KEY (workspace_id, submission_id)
                        REFERENCES submissions(workspace_id, submission_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    request_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage_counters (
                    workspace_id TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    day TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workspace_id, bucket, day)
                );

                CREATE INDEX IF NOT EXISTS idx_submissions_workspace_created
                    ON submissions(workspace_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_submissions_workspace_state
                    ON submissions(workspace_id, state);
                CREATE INDEX IF NOT EXISTS idx_reviews_workspace_updated
                    ON reviews(workspace_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_workspace_created
                    ON audit_logs(workspace_id, created_at DESC);
                """
            )
            current = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if current and int(current["value"]) > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前程序，拒绝用旧代码打开")
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    def seed_builtin(
        self,
        workspace_id: str,
        questions: Iterable[dict],
        submissions: Iterable[dict],
        grade: Callable[[dict, dict], dict],
    ) -> None:
        now = utc_now()
        question_map = {q["question_id"]: q for q in questions}
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO folders(workspace_id,folder_id,name,is_system,created_at) "
                "VALUES(?,?,?,?,?)",
                (workspace_id, "inbox", "待批作业", 1, now),
            )
            for question in question_map.values():
                conn.execute(
                    "INSERT INTO questions(workspace_id,question_id,payload,created_at,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,question_id) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=excluded.updated_at",
                    (workspace_id, question["question_id"], _json(question), now, now),
                )
            for submission in submissions:
                question = question_map.get(submission.get("question_id"))
                if not question:
                    continue
                result = grade(question, submission)
                result.update(
                    {
                        "submission_id": submission["submission_id"],
                        "question_id": question["question_id"],
                        "question_title": question["title"],
                        "question_text": question["question_text"],
                        "subject": question["subject"],
                        "student_id": submission.get("student_id"),
                        "student_name": submission.get("student_name") or "匿名学生",
                        "class_id": submission.get("class_id") or "C001",
                        "ocr_text": (submission.get("ocr") or {}).get("text", ""),
                        "source": "builtin",
                    }
                )
                conn.execute(
                    """
                    INSERT INTO submissions(
                        workspace_id,submission_id,source,state,question_id,student_id,
                        student_name,class_id,subject,question_title,question_text,ocr_text,
                        ocr_clarity,file_id,folder_id,ai_result,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(workspace_id,submission_id) DO UPDATE SET
                        ai_result=excluded.ai_result,updated_at=excluded.updated_at
                    """,
                    (
                        workspace_id,
                        submission["submission_id"],
                        "builtin",
                        "graded",
                        question["question_id"],
                        submission.get("student_id"),
                        submission.get("student_name") or "匿名学生",
                        submission.get("class_id") or "C001",
                        question["subject"],
                        question["title"],
                        question["question_text"],
                        (submission.get("ocr") or {}).get("text", ""),
                        float((submission.get("ocr") or {}).get("clarity", 0)),
                        None,
                        "inbox",
                        _json(result),
                        now,
                        now,
                    ),
                )

    def ready(self) -> bool:
        try:
            with self.connection() as conn:
                row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            return bool(row and int(row["value"]) == SCHEMA_VERSION)
        except (OSError, sqlite3.Error, ValueError):
            return False

    def list_questions(self, workspace_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM questions WHERE workspace_id=? ORDER BY question_id",
                (workspace_id,),
            ).fetchall()
        return [_loads(row["payload"], {}) for row in rows]

    def get_question(self, workspace_id: str, question_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM questions WHERE workspace_id=? AND question_id=?",
                (workspace_id, question_id),
            ).fetchone()
        return _loads(row["payload"], {}) if row else None

    def list_submissions(self, workspace_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE workspace_id=? "
                "ORDER BY CASE source WHEN 'upload' THEN 0 ELSE 1 END, created_at DESC, submission_id",
                (workspace_id,),
            ).fetchall()
        return [self._submission(row) for row in rows]

    def get_submission(self, workspace_id: str, submission_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE workspace_id=? AND submission_id=?",
                (workspace_id, submission_id),
            ).fetchone()
        return self._submission(row) if row else None

    @staticmethod
    def _submission(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["ai_result"] = _loads(result.get("ai_result"), None)
        return result

    def create_file_and_submission(self, workspace_id: str, file_row: dict, submission: dict) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO files(workspace_id,file_id,original_name,content_type,sha256,
                    size_bytes,width,height,storage_path,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    workspace_id,
                    file_row["file_id"],
                    file_row["original_name"],
                    file_row["content_type"],
                    file_row["sha256"],
                    file_row["size_bytes"],
                    file_row["width"],
                    file_row["height"],
                    file_row["storage_path"],
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO submissions(workspace_id,submission_id,source,state,question_id,
                    student_id,student_name,class_id,subject,question_title,question_text,
                    ocr_text,ocr_clarity,file_id,folder_id,ai_result,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    workspace_id,
                    submission["submission_id"],
                    "upload",
                    submission["state"],
                    submission.get("question_id"),
                    submission.get("student_id"),
                    submission["student_name"],
                    submission["class_id"],
                    submission.get("subject", ""),
                    submission.get("question_title", ""),
                    submission.get("question_text", ""),
                    submission.get("ocr_text", ""),
                    float(submission.get("ocr_clarity", 0)),
                    file_row["file_id"],
                    submission.get("folder_id") or "inbox",
                    _json(submission["ai_result"]) if submission.get("ai_result") else None,
                    now,
                    now,
                ),
            )

    def update_submission_result(
        self, workspace_id: str, submission_id: str, state: str, ocr_text: str, result: dict
    ) -> bool:
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE submissions SET state=?,ocr_text=?,ai_result=?,updated_at=? "
                "WHERE workspace_id=? AND submission_id=?",
                (state, ocr_text, _json(result), utc_now(), workspace_id, submission_id),
            ).rowcount
        return changed == 1

    def update_recognition(
        self,
        workspace_id: str,
        submission_id: str,
        transcript: str,
        clarity: float,
        subject: str | None = None,
        question_text: str | None = None,
    ) -> bool:
        fields = ["state=?", "ocr_text=?", "ocr_clarity=?", "updated_at=?"]
        values: list = ["recognized", transcript, clarity, utc_now()]
        if subject:
            fields.append("subject=?")
            values.append(subject)
        if question_text:
            fields.append("question_text=?")
            values.append(question_text)
        values.extend([workspace_id, submission_id])
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                f"UPDATE submissions SET {','.join(fields)} "
                "WHERE workspace_id=? AND submission_id=?",
                values,
            ).rowcount
        return changed == 1

    def get_file(self, workspace_id: str, file_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE workspace_id=? AND file_id=?",
                (workspace_id, file_id),
            ).fetchone()
        return dict(row) if row else None

    def list_reviews(self, workspace_id: str) -> dict[str, dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE workspace_id=?", (workspace_id,)
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["final_error_tags"] = _loads(item.get("final_error_tags"), None)
            item["final_dimension_scores"] = _loads(item.get("final_dimension_scores"), None)
            result[item["submission_id"]] = item
        return result

    def upsert_review(self, workspace_id: str, review: dict, request_id: str = "") -> None:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO reviews(workspace_id,submission_id,teacher_action,ai_score,
                    final_score,max_score,final_error_tags,final_comment,final_dimension_scores,
                    created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id,submission_id) DO UPDATE SET
                    teacher_action=excluded.teacher_action,ai_score=excluded.ai_score,
                    final_score=excluded.final_score,max_score=excluded.max_score,
                    final_error_tags=excluded.final_error_tags,
                    final_comment=excluded.final_comment,
                    final_dimension_scores=excluded.final_dimension_scores,
                    updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    review["submission_id"],
                    review["teacher_action"],
                    review["ai_score"],
                    review["final_score"],
                    review["max_score"],
                    _json(review["final_error_tags"])
                    if review.get("final_error_tags") is not None
                    else None,
                    review.get("final_comment", ""),
                    _json(review["final_dimension_scores"])
                    if review.get("final_dimension_scores") is not None
                    else None,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO audit_logs(workspace_id,action,object_type,object_id,detail,request_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    "teacher_review",
                    "submission",
                    review["submission_id"],
                    _json(
                        {
                            "teacher_action": review["teacher_action"],
                            "ai_score": review["ai_score"],
                            "final_score": review["final_score"],
                        }
                    ),
                    request_id,
                    now,
                ),
            )

    def list_folders(self, workspace_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT f.*, COUNT(s.submission_id) AS count
                FROM folders f
                LEFT JOIN submissions s ON s.workspace_id=f.workspace_id
                    AND s.folder_id=f.folder_id
                WHERE f.workspace_id=?
                GROUP BY f.workspace_id,f.folder_id
                ORDER BY f.is_system DESC,f.created_at,f.name
                """,
                (workspace_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def take_quota(self, workspace_id: str, bucket: str, limit: int) -> bool:
        if limit <= 0:
            return True
        day = datetime.now(timezone.utc).date().isoformat()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO usage_counters(workspace_id,bucket,day,used) VALUES(?,?,?,0)",
                (workspace_id, bucket, day),
            )
            changed = conn.execute(
                "UPDATE usage_counters SET used=used+1 "
                "WHERE workspace_id=? AND bucket=? AND day=? AND used<?",
                (workspace_id, bucket, day, limit),
            ).rowcount
        return changed == 1

    def quota_snapshot(self, workspace_id: str, limits: dict[str, int]) -> dict:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT bucket,used FROM usage_counters WHERE workspace_id=? AND day=?",
                (workspace_id, day),
            ).fetchall()
        used = {row["bucket"]: row["used"] for row in rows}
        return {
            bucket: {
                "enabled": limit > 0,
                "limit": limit if limit > 0 else None,
                "used": used.get(bucket, 0),
                "remaining": max(0, limit - used.get(bucket, 0)) if limit > 0 else None,
            }
            for bucket, limit in limits.items()
        }

    def create_folder(self, workspace_id: str, folder_id: str, name: str) -> dict:
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO folders(workspace_id,folder_id,name,is_system,created_at) VALUES(?,?,?,?,?)",
                (workspace_id, folder_id, name, 0, now),
            )
        return {"folder_id": folder_id, "name": name, "is_system": 0, "count": 0}

    def reset_workspace_uploads(self, workspace_id: str) -> list[str]:
        """仅测试/开发重置：返回需要从磁盘删除的相对文件路径。"""
        with self.transaction(immediate=True) as conn:
            paths = [
                row["storage_path"]
                for row in conn.execute(
                    "SELECT storage_path FROM files WHERE workspace_id=?", (workspace_id,)
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM reviews WHERE workspace_id=? AND submission_id IN "
                "(SELECT submission_id FROM submissions WHERE workspace_id=? AND source='upload')",
                (workspace_id, workspace_id),
            )
            conn.execute(
                "DELETE FROM submissions WHERE workspace_id=? AND source='upload'", (workspace_id,)
            )
            conn.execute("DELETE FROM files WHERE workspace_id=?", (workspace_id,))
        return paths
