import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Addendum, Attachment, AuditEvent, Card, TherapistComment, User

ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_CARD_UPLOADS = 100 * 1024 * 1024
CARD_VOLUME_FORMAT = "journalmax-card-volume"
CARD_VOLUME_SCHEMA_VERSION = 1

def audit(actor, action, target="", metadata=None, request=None):
    ip = request.META.get("REMOTE_ADDR") if request else None
    return AuditEvent.objects.create(actor=actor, action=action, target_id=str(target), metadata=metadata or {}, ip_address=ip)

def storage_status():
    root = settings.CARD_ROOT
    marker_name = getattr(settings, "CARD_VOLUME_MARKER", ".journalmax-volume.json")
    marker_path = root / marker_name
    marker_required = getattr(settings, "CARD_VOLUME_REQUIRE_MARKER", False)
    expected_id = getattr(settings, "CARD_VOLUME_ID", "")
    status = {
        "available": False,
        "state": "offline",
        "label": "OFFLINE",
        "reason": "Card storage is unavailable.",
        "root": str(root),
        "marker_name": marker_name,
        "marker_required": marker_required,
        "marker_present": False,
        "protected": False,
        "volume_id": "",
        "schema_version": None,
        "free_bytes": None,
        "used_bytes": None,
        "total_bytes": None,
        "used_percent": None,
    }
    try:
        if not root.exists():
            if marker_required:
                status["state"] = "missing"
                status["label"] = "MISSING"
                status["reason"] = "The configured card-volume path does not exist. The mount may be offline."
                return status
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            status["state"] = "invalid"
            status["label"] = "INVALID"
            status["reason"] = "The configured card-volume path is not a directory."
            return status

        marker = None
        if marker_path.exists():
            status["marker_present"] = True
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                status["state"] = "unrecognized"
                status["label"] = "UNRECOGNIZED"
                status["reason"] = f"The volume identity file {marker_name} is unreadable or invalid."
                return status
            if not isinstance(marker, dict) or marker.get("format") != CARD_VOLUME_FORMAT or marker.get("schema_version") != CARD_VOLUME_SCHEMA_VERSION or not marker.get("volume_id"):
                status["state"] = "unrecognized"
                status["label"] = "UNRECOGNIZED"
                status["reason"] = f"The volume identity file {marker_name} is not a supported Journalmax volume."
                return status
            status["protected"] = True
            status["volume_id"] = str(marker["volume_id"])
            status["schema_version"] = marker["schema_version"]
            if expected_id and status["volume_id"] != expected_id:
                status["state"] = "mismatch"
                status["label"] = "WRONG VOLUME"
                status["reason"] = "The mounted volume identity does not match DIARY_CARD_VOLUME_ID."
                return status
        elif marker_required:
            status["state"] = "unrecognized"
            status["label"] = "UNRECOGNIZED"
            status["reason"] = f"The expected {marker_name} identity file is missing. Writes are blocked."
            return status

        fd, probe = tempfile.mkstemp(prefix=".journalmax-write-probe-", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if os.path.exists(probe): os.unlink(probe)
        usage = shutil.disk_usage(root)
        status.update({
            "available": True,
            "state": "online" if marker else "local",
            "label": "ONLINE" if marker else "LOCAL DEV",
            "reason": "Recognized Journalmax card volume is online and writable." if marker else "Local unmarked development storage is online and writable.",
            "free_bytes": usage.free,
            "used_bytes": usage.used,
            "total_bytes": usage.total,
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        })
        return status
    except OSError as exc:
        status["state"] = "read-only" if getattr(exc, "errno", None) in {13, 30} else "offline"
        status["label"] = "READ ONLY" if status["state"] == "read-only" else "OFFLINE"
        status["reason"] = "The card volume is not writable." if status["state"] == "read-only" else "The card volume could not be reached."
        return status

def require_card_storage():
    status = storage_status()
    if not status["available"]:
        raise ValidationError(f"Card storage offline: {status['reason']}")
    return status

def safe_name(name):
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "attachment"
    return base[:180]

def sha256_bytes(data): return hashlib.sha256(data).hexdigest()

def valid_signature(content_type, data):
    if content_type == "image/jpeg": return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png": return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp": return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if content_type == "application/pdf": return data.startswith(b"%PDF-")
    if content_type in ("text/plain", "text/markdown"):
        try: data.decode("utf-8"); return b"\x00" not in data
        except UnicodeDecodeError: return False
    return False

def card_directory(card): return settings.CARD_ROOT / str(card.local_date.year) / card.folder_name

def card_storage_directory(card):
    if card.status == Card.Status.QUARANTINED:
        return settings.CARD_ROOT / ".quarantine" / card.folder_name
    return card_directory(card)

def comments_filename(card): return f"{card.local_date:%y%m%d}_therapist_comments.md"

def card_file_inventory(card, volume_status=None):
    """Return directory metadata without opening or reading any card file."""
    if not (volume_status or storage_status())["available"]:
        return {"available": False, "files": [], "file_count": 0, "total_size": 0}
    folder = card_storage_directory(card)
    files = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                    files.append({"name": entry.name, "size": size})
                except OSError:
                    files.append({"name": entry.name, "size": None})
    except OSError:
        return {"available": False, "files": [], "file_count": 0, "total_size": 0}
    files.sort(key=lambda item: item["name"].lower())
    return {"available": True, "files": files, "file_count": len(files), "total_size": sum(item["size"] or 0 for item in files)}

def verify_card_integrity(card, volume_status=None):
    if not (volume_status or storage_status())["available"]: return "offline"
    folder = card_storage_directory(card)
    markdown_path = folder / f"{card.local_date:%y%m%d}_diary.md"
    try:
        if sha256_bytes(markdown_path.read_bytes()) != card.checksum: return "conflict"
        for item in card.attachments.all():
            path = folder / item.stored_name
            if not path.is_file() or sha256_bytes(path.read_bytes()) != item.checksum: return "conflict"
        for item in card.addenda.all():
            path = folder / item.filename
            if not path.is_file() or sha256_bytes(path.read_bytes()) != item.checksum: return "conflict"
        if card.therapist_comments.exists():
            path = folder / comments_filename(card)
            if not card.comments_checksum or not path.is_file() or sha256_bytes(path.read_bytes()) != card.comments_checksum: return "conflict"
        return "verified"
    except OSError: return "missing"

def _yaml_scalar(value): return json.dumps(value, ensure_ascii=False)

def render_markdown(card, data):
    emotions = data.get("emotions", [])
    lines = ["---", "schema_version: 1", f"card_id: {_yaml_scalar(str(card.id))}", f"local_date: {_yaml_scalar(card.local_date.isoformat())}", f"submitted_at: {_yaml_scalar(timezone.now().isoformat())}", f"form_version: {card.form_version}", "emotions:"]
    for emotion in emotions:
        lines.extend([f"  - id: {_yaml_scalar(emotion.get('id', ''))}", f"    label: {_yaml_scalar(emotion.get('label', ''))}", f"    intensity: {int(emotion.get('intensity', 1))}"])
    lines.extend(["---", "", f"# Diary card — {card.local_date.isoformat()}", "", "## Significant emotions", ""])
    for emotion in emotions:
        lines.append(f"- **{emotion.get('label', '')}** — {int(emotion.get('intensity', 1))}/5")
        if emotion.get("note"): lines.append(f"  - {emotion['note']}")
    lines.append("")
    for field in data.get("custom", []):
        value = field.get("value")
        if isinstance(value, bool): value = "Yes" if value else "No"
        lines.extend([f"## {field.get('label', 'Additional response')}", "", str(value).strip() if value not in (None, "") else "_No response_", ""])
    return "\n".join(lines)

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

@transaction.atomic
def submit_card(card, data, uploads, actor, request=None):
    if card.status != Card.Status.DRAFT: raise ValidationError("Only drafts can be submitted.")
    if not data.get("emotions"): raise ValidationError("Choose at least one significant emotion.")
    require_card_storage()
    for emotion in data["emotions"]:
        try: emotion["intensity"] = max(1, min(5, int(emotion.get("intensity") or 3)))
        except (TypeError, ValueError): emotion["intensity"] = 3
    prefix = card.local_date.strftime("%y%m%d")
    card.folder_name = f"{prefix}_diary-card_{str(card.id)[:8]}"
    folder = card_directory(card)
    if folder.exists(): raise ValidationError("The card folder already exists.")
    folder.mkdir(parents=True)
    try:
        markdown = render_markdown(card, data).encode("utf-8")
        markdown_name = f"{prefix}_diary.md"
        atomic_write(folder / markdown_name, markdown)
        if sum(upload.size for upload in uploads) > MAX_CARD_UPLOADS: raise ValidationError("Attachments exceed the 100 MiB card limit.")
        for upload in uploads:
            if upload.size > MAX_FILE_SIZE or upload.content_type not in ALLOWED_TYPES:
                raise ValidationError(f"Unsupported attachment: {upload.name}")
            raw = upload.read()
            if not valid_signature(upload.content_type, raw): raise ValidationError(f"Attachment content does not match its declared type: {upload.name}")
            clean = safe_name(upload.name)
            stem, suffix = Path(clean).stem, Path(clean).suffix.lower()
            candidate, counter = f"{prefix}_{stem}{suffix}", 2
            while (folder / candidate).exists():
                candidate = f"{prefix}_{stem}_{counter}{suffix}"; counter += 1
            atomic_write(folder / candidate, raw)
            Attachment.objects.create(card=card, stored_name=candidate, original_name=clean, content_type=upload.content_type, size=len(raw), checksum=sha256_bytes(raw))
        card.status = Card.Status.SUBMITTED
        card.content_index = data
        card.draft_data = {}
        card.submitted_at = timezone.now()
        card.checksum = sha256_bytes(markdown)
        card.save()
        audit(actor, "card.submitted", card.id, request=request)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return card

@transaction.atomic
def add_addendum(card, body, actor, request=None):
    if card.status != Card.Status.SUBMITTED: raise ValidationError("Addenda require a submitted card.")
    require_card_storage()
    body = body.strip()
    if not body: raise ValidationError("Addendum cannot be empty.")
    now = timezone.now()
    filename = f"{card.local_date:%y%m%d}_addendum_{now:%Y%m%dT%H%M%SZ}.md"
    payload = f"---\ncard_id: {json.dumps(str(card.id))}\ncreated_at: {json.dumps(now.isoformat())}\n---\n\n# Addendum\n\n{body}\n".encode()
    atomic_write(card_directory(card) / filename, payload)
    item = Addendum.objects.create(card=card, body=body, filename=filename, checksum=sha256_bytes(payload))
    audit(actor, "card.addendum_created", card.id, request=request)
    return item

def render_therapist_comments(card, comments):
    lines = ["---", "schema_version: 1", "document_type: therapist_comments", f"card_id: {_yaml_scalar(str(card.id))}", "visibility: patient_and_therapist", "---", "", "# Therapist comments", ""]
    for index, comment in enumerate(comments, 1):
        lines.extend([f"## Comment {index:03d}", "", f"**{comment.author_name}** · {comment.created_at.isoformat()}", "", comment.body, ""])
    return "\n".join(lines)

@transaction.atomic
def add_therapist_comment(card, body, actor, request=None):
    card = Card.objects.select_for_update().get(pk=card.pk)
    if card.status != Card.Status.SUBMITTED: raise ValidationError("Comments require a submitted card.")
    if actor.role != User.Role.REVIEWER: raise ValidationError("Only a therapist can add comments.")
    require_card_storage()
    body = body.strip()
    if not body: raise ValidationError("Comment cannot be empty.")
    comment = TherapistComment.objects.create(card=card, reviewer=actor, author_name=actor.get_full_name().strip() or actor.username, body=body)
    comments = list(card.therapist_comments.all())
    payload = render_therapist_comments(card, comments).encode("utf-8")
    atomic_write(card_directory(card) / comments_filename(card), payload)
    card.comments_checksum = sha256_bytes(payload)
    card.save(update_fields=["comments_checksum"])
    audit(actor, "reviewer.comment_added", card.id, {"comment_id": str(comment.id)}, request)
    return comment

@transaction.atomic
def quarantine_card(card, actor, request=None):
    if card.status == Card.Status.QUARANTINED: return
    require_card_storage()
    source = card_directory(card)
    target = settings.CARD_ROOT / ".quarantine" / card.folder_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists(): raise ValidationError("Card files are missing; deletion stopped.")
    os.replace(source, target)
    card.status = Card.Status.QUARANTINED
    card.quarantined_at = timezone.now()
    card.purge_after = timezone.now() + timedelta(days=7)
    card.save(update_fields=["status", "quarantined_at", "purge_after", "updated_at"])
    audit(actor, "card.quarantined", card.id, request=request)

def restore_card(card, actor, request=None):
    require_card_storage()
    source = settings.CARD_ROOT / ".quarantine" / card.folder_name
    target = settings.CARD_ROOT / str(card.local_date.year) / card.folder_name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    card.status = Card.Status.SUBMITTED; card.quarantined_at = None; card.purge_after = None
    card.save(update_fields=["status", "quarantined_at", "purge_after", "updated_at"])
    audit(actor, "card.restored", card.id, request=request)

@transaction.atomic
def purge_card(card, actor=None, request=None, force=False):
    if card.status != Card.Status.QUARANTINED or not card.purge_after or (card.purge_after > timezone.now() and not force):
        raise ValidationError("Card is not eligible for final purge.")
    require_card_storage()
    source = settings.CARD_ROOT / ".quarantine" / card.folder_name
    staging = settings.CARD_ROOT / ".purging" / card.folder_name
    staging.parent.mkdir(parents=True, exist_ok=True)
    if source.exists(): os.replace(source, staging)
    card_id = str(card.id)
    try:
        card.delete()
        audit(actor, "card.purged", card_id, request=request)
    except Exception:
        if staging.exists(): os.replace(staging, source)
        raise
    shutil.rmtree(staging, ignore_errors=True)
