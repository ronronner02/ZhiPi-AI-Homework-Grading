from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import re
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings


class UploadError(ValueError):
    pass


_DATA_URL = re.compile(r"^data:[^;,]+;base64,", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FORMAT = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


def clean_filename(value: str) -> str:
    name = Path(_CONTROL.sub("", value or "")).name.strip()
    return name[:160] or "upload"


class FileStorage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.files_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def decode_base64(self, payload: str) -> bytes:
        text = _DATA_URL.sub("", (payload or "").strip(), count=1)
        # 先按编码长度拦截，避免超大字符串在 b64decode 时再复制一份内存。
        encoded_limit = ((self.settings.max_image_bytes + 2) // 3) * 4 + 16
        if len(text) > encoded_limit:
            raise UploadError(
                "图片过大，单张上限 %d MB"
                % (self.settings.max_image_bytes // 1024 // 1024)
            )
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise UploadError("图片数据不是合法 Base64") from None
        if not data:
            raise UploadError("不能上传空文件")
        if len(data) > self.settings.max_image_bytes:
            raise UploadError(
                "图片过大，单张上限 %d MB"
                % (self.settings.max_image_bytes // 1024 // 1024)
            )
        return data

    def save_image(self, workspace_id: str, filename: str, payload: str) -> dict:
        original = clean_filename(filename)
        raw = self.decode_base64(payload)
        try:
            with Image.open(io.BytesIO(raw)) as probe:
                fmt = (probe.format or "").upper()
                width, height = probe.size
                frames = int(getattr(probe, "n_frames", 1))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise UploadError("无法解析图片，请上传完整的 JPG / PNG / WebP 文件") from None

        if fmt not in _FORMAT:
            raise UploadError("暂不支持该图片格式，请上传 JPG / PNG / WebP")
        if frames != 1:
            raise UploadError("不支持动图，请先导出为单张静态图片")
        if width < 16 or height < 16:
            raise UploadError("图片尺寸过小，无法识别作业内容")
        if width * height > self.settings.max_image_pixels:
            raise UploadError("图片总像素过大，请压缩后重试")
        if max(width, height) / min(width, height) > 50:
            raise UploadError("图片长宽比异常，请重新裁剪后上传")

        # 完整解码并重新编码，既验证截断文件，也去除 EXIF 定位信息和尾随载荷。
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                has_alpha = image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                )
                output = io.BytesIO()
                if has_alpha:
                    image.convert("RGBA").save(output, format="PNG", optimize=True)
                    content_type, extension = "image/png", ".png"
                else:
                    image.convert("RGB").save(
                        output, format="JPEG", quality=92, optimize=True, progressive=True
                    )
                    content_type, extension = "image/jpeg", ".jpg"
        except (OSError, Image.DecompressionBombError):
            raise UploadError("图片文件不完整或已损坏，请重新上传") from None

        normalized = output.getvalue()
        digest = hashlib.sha256(normalized).hexdigest()
        file_id = "FIL-" + uuid.uuid4().hex
        relative = Path(workspace_id) / digest[:2] / f"{file_id}{extension}"
        target = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in target.parents:
            raise UploadError("存储路径非法")
        target.parent.mkdir(parents=True, exist_ok=True)

        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                temp.write(normalized)
                temp.flush()
                os.fsync(temp.fileno())
                temp_name = temp.name
            os.replace(temp_name, target)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

        return {
            "file_id": file_id,
            "original_name": original,
            "content_type": content_type,
            "sha256": digest,
            "size_bytes": len(normalized),
            "width": width,
            "height": height,
            "storage_path": relative.as_posix(),
            "absolute_path": target,
        }

    def resolve(self, relative_path: str) -> Path | None:
        candidate = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if root not in candidate.parents or not candidate.is_file():
            return None
        return candidate

    def delete(self, relative_path: str) -> None:
        path = self.resolve(relative_path)
        if path:
            path.unlink(missing_ok=True)

