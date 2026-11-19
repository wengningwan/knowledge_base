"""按部署操作员标记采集单个文件真实的 PDF 模型往返。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from yuxi.config import get_runtime_dir
from yuxi.utils.logging_config import logger

_SCOPE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


async def pdf_model_capture_path(control, request_id: str) -> Path | None:
    """查找操作员为单个文件开启的采集位置。"""
    file_id = getattr(control, "file_id", None)
    attempt_id = getattr(control, "attempt_id", None)
    if not all(isinstance(value, str) and _SCOPE_ID.fullmatch(value) for value in (file_id, attempt_id, request_id)):
        return None
    try:
        root = get_runtime_dir() / "pdf-model-traces"
        async with asyncio.timeout(0.5):
            enabled = await asyncio.to_thread((root / "enabled" / file_id).is_file)
        if enabled:
            return root / "requests" / file_id / attempt_id / request_id
    except (OSError, TimeoutError) as exc:
        logger.warning(f"[pdf_model_capture] request_id={request_id} marker_check_failed error={type(exc).__name__}")
    return None


async def capture_pdf_model_exchange(
    path: Path, manifest: dict, images: list[bytes], mime: str, result: dict, wait_seconds: float
) -> None:
    """共享模型槽释放后，独立保存调用记录且限制主流程等待。"""
    try:
        async with asyncio.timeout(wait_seconds):
            await asyncio.to_thread(_write_exchange, path, manifest, images, mime, result)
    except TimeoutError:
        logger.warning(f"[pdf_model_capture] request_id={path.name} write_wait_timeout")
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(f"[pdf_model_capture] request_id={path.name} write_failed error={type(exc).__name__}")


def _write_exchange(path: Path, manifest: dict, images: list[bytes], mime: str, result: dict) -> None:
    """先写原始图像，再发布完整的请求及回复。"""
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "bin")
    image_files = []
    for index, data in enumerate(images, 1):
        name = f"image-{index:02d}.{suffix}"
        (path / name).write_bytes(data)
        image_files.append({"file": name, "mime": mime, "bytes": len(data)})
    _write_json(path / "request.json", {**manifest, "images": image_files})
    _write_json(path / "response.json", {**result, "recorded_at": datetime.now(UTC).isoformat()})


def _write_json(path: Path, payload: dict) -> None:
    """完整文件原子可见，并保留无效 Unicode 的 JSON 转义供排查。"""
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", errors="backslashreplace")
    temp.replace(path)
