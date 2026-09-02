from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT_DIR, Settings
from .grading import ERROR_TAGS
from .llm import UpstreamError
from .schemas import (
    FolderCreateRequest,
    GradeRequest,
    LoginRequest,
    RecognizeRequest,
    ReviewRequest,
    UploadRequest,
)
from .service import ConflictError, NotFoundError, ProductService, QuotaError
from .storage import UploadError


logger = logging.getLogger("zhipi")
AUTH_COOKIE = "zhipi_auth"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """同时约束 Content-Length 与 chunked body，避免只检查请求头被绕过。"""

    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.limit:
            response = JSONResponse(
                {"detail": f"请求体过大，上限 {self.limit // 1024 // 1024} MB"},
                status_code=413,
            )
            return await response(scope, receive, send)

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise BodyTooLarge
            return message

        try:
            return await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            response = JSONResponse(
                {"detail": f"请求体过大，上限 {self.limit // 1024 // 1024} MB"},
                status_code=413,
            )
            return await response(scope, receive, send)


class AuthManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.key = (settings.secret_key or "dev-only-change-me").encode("utf-8")

    def token(self) -> str:
        signature = hmac.new(self.key, b"zhipi-teacher-v1", hashlib.sha256).hexdigest()
        return "v1." + signature

    def valid_token(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(str(token), self.token())

    def authorized(self, request: Request) -> bool:
        if not self.settings.access_code:
            return True
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return secrets.compare_digest(
                authorization.split(" ", 1)[1], self.settings.access_code
            )
        return self.valid_token(request.cookies.get(AUTH_COOKIE))


class LoginLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 300:
                hits.popleft()
            if len(hits) >= 10:
                return False, max(1, int(300 - (now - hits[0])))
            hits.append(now)
            if len(self._hits) > 2000:
                for old_key in list(self._hits)[:500]:
                    if not self._hits[old_key] or now - self._hits[old_key][-1] > 300:
                        self._hits.pop(old_key, None)
            return True, 0


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")[:64]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = ProductService(settings)
    auth = AuthManager(settings)
    login_limiter = LoginLimiter()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 多 worker 同时启动时，SQLite 的 schema/seed 会短暂争用写锁；
        # 只对明确的 locked 重试，其他初始化错误必须立即暴露。
        for attempt in range(5):
            try:
                service.initialize()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.25 * (attempt + 1))
        logger.info(
            "zhipi_started environment=%s mode=%s database=%s",
            settings.environment,
            "llm" if settings.llm_enabled else "mock",
            settings.database_path,
        )
        yield

    app = FastAPI(
        title="智批π · 教师可控作业批改系统",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.service = service
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_body_bytes)

    public_paths = {
        "/",
        "/healthz",
        "/readyz",
        "/api/auth/login",
        "/api/auth/status",
        "/docs",
        "/openapi.json",
    }

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "")
        if not _REQUEST_ID.fullmatch(request_id):
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        path = request.url.path
        is_public = path in public_paths or path.startswith("/static/")
        if not is_public and not auth.authorized(request):
            response = JSONResponse({"detail": "请先登录教师工作台"}, status_code=401)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "request id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request_id,
            request.method,
            path,
            response.status_code,
            elapsed,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = []
        for error in exc.errors()[:4]:
            location = ".".join(str(item) for item in error.get("loc", []) if item != "body")
            message = error.get("msg", "参数不合法")
            details.append(f"{location or '请求'}：{message}")
        return JSONResponse(
            {"detail": "请求参数不合法；" + "；".join(details)}, status_code=422
        )

    @app.exception_handler(NotFoundError)
    async def not_found(_request: Request, exc: NotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict(_request: Request, exc: ConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(UploadError)
    async def upload_error(_request: Request, exc: UploadError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(UpstreamError)
    async def upstream_error(_request: Request, exc: UpstreamError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(QuotaError)
    async def quota_error(_request: Request, exc: QuotaError):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "3600"})

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        logger.exception("unhandled_error request_id=%s", request.state.request_id)
        return JSONResponse(
            {
                "detail": "服务内部错误，请携带请求编号联系管理员",
                "request_id": request.state.request_id,
            },
            status_code=500,
        )

    static_dir = ROOT_DIR / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(
            (static_dir / "index.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": "1.0.0"}

    @app.get("/readyz")
    def readyz():
        if not service.db.ready() or not settings.files_dir.is_dir():
            raise HTTPException(status_code=503, detail="持久化存储尚未就绪")
        return {"status": "ready", "database": "ok", "storage": "ok"}

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {
            "required": bool(settings.access_code),
            "authenticated": auth.authorized(request),
        }

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response):
        allowed, retry = login_limiter.allow(_client_ip(request))
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="登录尝试过于频繁，请稍后重试",
                headers={"Retry-After": str(retry)},
            )
        if settings.access_code and not secrets.compare_digest(
            payload.access_code, settings.access_code
        ):
            raise HTTPException(status_code=401, detail="访问口令不正确")
        response.set_cookie(
            AUTH_COOKIE,
            auth.token(),
            max_age=12 * 3600,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
            path="/",
        )
        return {"status": "ok"}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(AUTH_COOKIE, path="/")
        return {"status": "ok"}

    @app.get("/api/config")
    @app.get("/api/demo/config")
    def config():
        return service.config()

    @app.get("/api/questions")
    def questions():
        return {"questions": service.questions()}

    @app.get("/api/submissions")
    def submissions():
        return {"submissions": service.submissions()}

    @app.post("/api/submissions/upload", status_code=201)
    def upload(payload: UploadRequest):
        return service.upload(payload)

    @app.get("/api/files/{file_id}")
    def file(file_id: str):
        if not re.fullmatch(r"FIL-[0-9a-f]{32}", file_id):
            raise NotFoundError("文件不存在")
        path, meta = service.file(file_id)
        return FileResponse(
            path,
            media_type=meta["content_type"],
            filename=meta["original_name"],
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post("/api/grade")
    def grade(payload: GradeRequest):
        return service.grade(payload.submission_id, payload.transcript)

    @app.post("/api/recognize")
    def recognize(payload: RecognizeRequest):
        return service.recognize(payload.submission_id)

    @app.get("/api/teacher/results")
    def teacher_results():
        return {
            "mode": "llm" if settings.llm_enabled else "mock",
            "results": service.submissions(),
            "error_tags_enum": list(ERROR_TAGS),
        }

    @app.post("/api/teacher/review")
    def review(payload: ReviewRequest, request: Request):
        record = service.review(payload.model_dump(), request.state.request_id)
        return {"status": record["teacher_action"], "review": record}

    @app.get("/api/analytics/class")
    def class_analytics(class_id: str = "C001"):
        return service.class_analytics(class_id)

    @app.get("/api/students")
    def students():
        return {"students": service.students()}

    @app.get("/api/analytics/student/{student_id}")
    def student(student_id: str):
        return service.student(student_id)

    @app.get("/api/lecture-outline")
    def lecture_outline(class_id: str = "C001"):
        return {"format": "markdown", "content": service.lecture_outline(class_id)}

    @app.get("/api/folders")
    def folders():
        return {"folders": service.folders()}

    @app.post("/api/folders", status_code=201)
    def create_folder(payload: FolderCreateRequest):
        return service.create_folder(payload)

    @app.post("/api/demo/reset")
    def reset():
        if settings.is_production:
            raise HTTPException(status_code=403, detail="生产环境禁止重置工作区数据")
        return {"status": "reset", "uploads_removed": service.reset_uploads()}

    return app


app = create_app()
