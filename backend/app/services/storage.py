"""Validated file storage for documents and photos. Files live outside any static web root and
are only ever served through authenticated endpoints."""
import hashlib
import re
import uuid
from pathlib import Path

from ..database import STORAGE_DIR
from .errors import NotFoundError, ValidationError

DOC_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "txt", "csv", "xlsx", "docx",
                  "dwg", "dxf", "step", "stp", "igs", "iges", "sldprt", "sldasm"}
PHOTO_EXTENSIONS = {"png", "jpg", "jpeg"}
MAX_DOC_BYTES = 25 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "tif": "image/tiff", "tiff": "image/tiff", "txt": "text/plain", "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_MAGIC = {"pdf": (b"%PDF-",), "png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",),
          "jpeg": (b"\xff\xd8\xff",), "xlsx": (b"PK",), "docx": (b"PK",)}


def _root() -> Path:
    return STORAGE_DIR


def safe_name(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    return base[:120] or "file"


def save_upload(bucket: str, filename: str, data: bytes, *, allowed: set[str], max_bytes: int) -> dict:
    name = safe_name(filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in allowed:
        raise ValidationError(f"File type .{ext or '?'} is not allowed; use one of: "
                              + ", ".join(sorted(allowed)))
    if not data:
        raise ValidationError("The file is empty")
    if len(data) > max_bytes:
        raise ValidationError(f"File is too large (limit {max_bytes // (1024 * 1024)} MB)")
    magic = _MAGIC.get(ext)
    if magic and not any(data.startswith(m) for m in magic):
        raise ValidationError(f"File content does not look like a real .{ext} file")
    sha = hashlib.sha256(data).hexdigest()
    rel = Path(bucket) / sha[:2] / f"{uuid.uuid4().hex}.{ext}"
    dest = _root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"file_name": name, "stored_path": rel.as_posix(), "mime": MIME.get(ext, "application/octet-stream"),
            "size": len(data), "sha256": sha}


def read_stored(stored_path: str) -> bytes:
    p = (_root() / stored_path).resolve()
    if _root().resolve() not in p.parents or not p.is_file():
        raise NotFoundError("File not found")
    return p.read_bytes()
