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
from .models import Addendum, Attachment, AuditEvent, Card

ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_CARD_UPLOADS = 100 * 1024 * 1024

def audit(actor, action, target="", metadata=None, request=None):
    ip = request.META.get("REMOTE_ADDR") if request else None
    return AuditEvent.objects.create(actor=actor, action=action, target_id=str(target), metadata=metadata or {}, ip_address=ip)

def storage_status():
    root = settings.CARD_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(root)
        return {"available": True, "free_bytes": usage.free, "total_bytes": usage.total, "root": str(root)}
    except OSError:
        return {"available": False, "free_bytes": None, "total_bytes": None, "root": str(root)}

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

def verify_card_integrity(card):
    if card.status == Card.Status.QUARANTINED: folder = settings.CARD_ROOT / ".quarantine" / card.folder_name
    else: folder = card_directory(card)
    markdown_path = folder / f"{card.local_date:%y%m%d}_diary.md"
    try:
        if sha256_bytes(markdown_path.read_bytes()) != card.checksum: return "conflict"
        for item in card.attachments.all():
            path = folder / item.stored_name
            if not path.is_file() or sha256_bytes(path.read_bytes()) != item.checksum: return "conflict"
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
    lines.extend(["", "## What I did today", "", data.get("activities", "").strip() or "_No response_", "", "## Reflection", "", data.get("journal", "").strip() or "_No response_", ""])
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
    body = body.strip()
    if not body: raise ValidationError("Addendum cannot be empty.")
    now = timezone.now()
    filename = f"{card.local_date:%y%m%d}_addendum_{now:%Y%m%dT%H%M%SZ}.md"
    payload = f"---\ncard_id: {json.dumps(str(card.id))}\ncreated_at: {json.dumps(now.isoformat())}\n---\n\n# Addendum\n\n{body}\n".encode()
    atomic_write(card_directory(card) / filename, payload)
    item = Addendum.objects.create(card=card, body=body, filename=filename, checksum=sha256_bytes(payload))
    audit(actor, "card.addendum_created", card.id, request=request)
    return item

@transaction.atomic
def quarantine_card(card, actor, request=None):
    if card.status == Card.Status.QUARANTINED: return
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
    source = settings.CARD_ROOT / ".quarantine" / card.folder_name
    target = settings.CARD_ROOT / str(card.local_date.year) / card.folder_name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    card.status = Card.Status.SUBMITTED; card.quarantined_at = None; card.purge_after = None
    card.save(update_fields=["status", "quarantined_at", "purge_after", "updated_at"])
    audit(actor, "card.restored", card.id, request=request)

@transaction.atomic
def purge_card(card):
    if card.status != Card.Status.QUARANTINED or not card.purge_after or card.purge_after > timezone.now():
        raise ValidationError("Card is not eligible for final purge.")
    source = settings.CARD_ROOT / ".quarantine" / card.folder_name
    staging = settings.CARD_ROOT / ".purging" / card.folder_name
    staging.parent.mkdir(parents=True, exist_ok=True)
    if source.exists(): os.replace(source, staging)
    card_id = str(card.id)
    try:
        card.delete()
        audit(None, "card.purged", card_id)
    except Exception:
        if staging.exists(): os.replace(staging, source)
        raise
    shutil.rmtree(staging, ignore_errors=True)
