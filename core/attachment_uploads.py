"""Shared attachment policy and bounded-memory validation/storage."""
import codecs
import hashlib
import os
from pathlib import Path
import tempfile

from django.core.exceptions import ValidationError
from .models import AttachmentSettings

ALLOWED_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
    "video/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
}
ALIASES = {"audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "application/mp4": "video/mp4"}
FORMAT_LABEL = "JPEG, PNG, WebP, PDF, plain text, Markdown, MP4 video, MP3 and M4A audio"
ACCEPT = ",".join([*ALLOWED_TYPES, *ALIASES, ".jpg", ".jpeg", ".png", ".webp", ".pdf", ".txt", ".md", ".mp4", ".mp3", ".m4a"])


def policy_context(used=0):
    limits = AttachmentSettings.current()
    return {"attachment_policy": limits, "attachment_used_bytes": used,
            "attachment_used_mib": used / (1024 * 1024), "attachment_accept": ACCEPT,
            "attachment_formats": FORMAT_LABEL,
            "attachment_formats_short": ", ".join(extension[1:].upper() for extension in ALLOWED_TYPES.values())}


def normalized_type(upload):
    declared = (upload.content_type or "").split(";", 1)[0].strip().lower()
    kind = ALIASES.get(declared, declared)
    suffix = Path(upload.name).suffix.lower()
    # Browsers often omit a MIME type for Markdown and M4A. Content is still checked.
    if kind in ("", "application/octet-stream"):
        kind = next((mime for mime, extension in ALLOWED_TYPES.items() if extension == suffix), kind)
        if suffix == ".jpeg": kind = "image/jpeg"
    if suffix == ".m4a" and kind == "video/mp4": kind = "audio/mp4"
    return kind


def media_signature(kind, upload, head):
    if kind in ("video/mp4", "audio/mp4"):
        if len(head) < 16 or head[4:8] != b"ftyp": return False
        size = int.from_bytes(head[:4], "big")
        if not 16 <= size <= min(upload.size, len(head)) or size % 4: return False
        brands = [head[8:12]] + [head[i:i+4] for i in range(16, size, 4)]
        return b"qt  " not in brands and bool(set(brands) & {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4A ", b"M4V "})
    if kind == "audio/mpeg":
        if head.startswith(b"ID3"):
            if len(head) < 10 or head[3] not in (2, 3, 4) or any(n & 0x80 for n in head[6:10]): return False
            offset = 10 + sum(n << shift for n, shift in zip(head[6:10], (21, 14, 7, 0)))
            if head[3] == 4 and head[5] & 0x10: offset += 10
            if offset + 4 > upload.size: return False
            upload.seek(offset)
            head = upload.read(4)
        return (len(head) >= 4 and head[0] == 255 and head[1] & 0xE0 == 0xE0
                and (head[1] >> 3) & 3 != 1 and (head[1] >> 1) & 3 == 1
                and (head[2] >> 4) not in (0, 15) and (head[2] >> 2) & 3 != 3)
    return False


def prepare_uploads(uploads, existing_size, signature_check):
    limits = AttachmentSettings.current()
    if uploads and existing_size + sum(upload.size for upload in uploads) > limits.card_limit_bytes:
        raise ValidationError(f"Attachments exceed the {limits.card_limit_mib} MiB card limit (including existing attachments).")
    prepared = []
    for upload in uploads:
        if upload.size > limits.file_limit_bytes:
            raise ValidationError(f"{upload.name} exceeds the {limits.file_limit_mib} MiB per-file limit.")
        kind = normalized_type(upload)
        if kind not in ALLOWED_TYPES: raise ValidationError(f"Unsupported attachment: {upload.name}. Supported: {FORMAT_LABEL}.")
        upload.seek(0)
        head = upload.read(65536)
        if kind in ("text/plain", "text/markdown"):
            upload.seek(0)
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                for chunk in upload.chunks():
                    if b"\0" in chunk: raise UnicodeError()
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
                valid = True
            except UnicodeError: valid = False
        elif kind.startswith(("video/", "audio/")):
            valid = media_signature(kind, upload, head)
        else: valid = signature_check(kind, head)
        upload.seek(0)
        if not valid: raise ValidationError(f"Attachment content does not match its declared type: {upload.name}")
        prepared.append((upload, kind))
    return prepared


def atomic_write_upload(path, upload):
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as output:
            upload.seek(0)
            for chunk in upload.chunks():
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if size != upload.size: raise ValidationError("Attachment size changed during upload.")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return size, digest.hexdigest()
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def file_checksum(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
