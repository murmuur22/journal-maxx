import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path
import bleach
import markdown
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.db.models import Avg, Count
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST
from .decorators import role_required
from .forms import AddendumForm, DiaryForm, ReviewerForm
from .models import AuditEvent, Card, Emotion, FormDefinition, Invite, ReviewerMetadata, User
from .services import add_addendum, audit, card_directory, quarantine_card, restore_card, storage_status, submit_card, verify_card_integrity
from .security import encrypt_secret, generate_totp_secret, issue_recovery_codes, verify_second_factor, verify_totp

def liveness(request): return JsonResponse({"status": "ok"})

def home(request):
    if not request.user.is_authenticated: return redirect("login")
    return redirect({User.Role.PATIENT: "journal:today", User.Role.REVIEWER: "review:list", User.Role.ADMIN: "control:dashboard"}[request.user.role])

@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated: return redirect("home")
    if request.method == "POST":
        throttle_key = "login:" + hashlib.sha256(f"{request.META.get('REMOTE_ADDR')}:{request.POST.get('username', '').lower()}".encode()).hexdigest()
        attempts = cache.get(throttle_key, 0)
        if attempts >= 5: return render(request, "login.html", {"username": request.POST.get("username", ""), "throttled": True}, status=429)
        user = authenticate(request, username=request.POST.get("username", ""), password=request.POST.get("password", ""))
        if user and user.is_active:
            if user.role in (User.Role.REVIEWER, User.Role.ADMIN):
                if not user.totp_confirmed or not verify_second_factor(user, request.POST.get("totp", "")):
                    cache.set(throttle_key, attempts + 1, 900)
                    messages.error(request, "A valid authenticator code is required."); return render(request, "login.html", {"username": request.POST.get("username", "")})
            cache.delete(throttle_key); login(request, user); audit(user, "auth.login", request=request); return redirect("home")
        cache.set(throttle_key, attempts + 1, 900)
        messages.error(request, "Access denied. Check your credentials.")
    return render(request, "login.html")

@require_http_methods(["GET", "POST"])
def accept_invite(request, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    invite = get_object_or_404(Invite, token_hash=digest, used_at__isnull=True, expires_at__gt=timezone.now())
    secret_key = f"invite_totp_{invite.id}"
    secret = request.session.get(secret_key) or generate_totp_secret(); request.session[secret_key] = secret
    if request.method == "POST":
        password = request.POST.get("password", "")
        if len(password) < 12: messages.error(request, "Passphrase must be at least 12 characters.")
        elif invite.role in (User.Role.REVIEWER, User.Role.ADMIN) and not verify_totp(secret, request.POST.get("totp")):
            messages.error(request, "Authenticator code did not verify.")
        else:
            user = User.objects.create_user(username=invite.username, password=password, role=invite.role, totp_confirmed=invite.role in (User.Role.REVIEWER, User.Role.ADMIN), totp_secret_encrypted=encrypt_secret(secret) if invite.role in (User.Role.REVIEWER, User.Role.ADMIN) else "")
            invite.used_at = timezone.now(); invite.save(update_fields=["used_at"]); request.session.pop(secret_key, None)
            codes = issue_recovery_codes(user) if user.role in (User.Role.REVIEWER, User.Role.ADMIN) else []
            audit(user, "invite.accepted"); return render(request, "recovery_codes.html", {"codes": codes})
    return render(request, "accept_invite.html", {"invite": invite, "totp_secret": secret})

@require_POST
def logout_view(request):
    if request.user.is_authenticated: audit(request.user, "auth.logout", request=request)
    logout(request); return redirect("login")

def _emotion_data(request):
    selected = set(request.POST.getlist("emotions")); result = []
    for emotion in Emotion.objects.filter(active=True, slug__in=selected):
        try: intensity = max(1, min(5, int(request.POST.get(f"intensity_{emotion.slug}", 3))))
        except ValueError: intensity = 3
        result.append({"id": emotion.slug, "label": emotion.label, "intensity": intensity, "note": request.POST.get(f"note_{emotion.slug}", "").strip()})
    return result

@role_required(User.Role.PATIENT)
@require_http_methods(["GET", "POST"])
def journal_today(request):
    local_date = timezone.localdate()
    form_def = FormDefinition.objects.filter(active=True).first()
    card, _ = Card.objects.get_or_create(patient=request.user, local_date=local_date, defaults={"form_version": form_def.version if form_def else 1})
    if card.status != Card.Status.DRAFT: return redirect("journal:detail", card_id=card.id)
    emotions = Emotion.objects.filter(active=True).order_by("sort_order", "label")
    custom_fields = [f for f in ((form_def.schema or {}).get("fields", []) if form_def else []) if isinstance(f, dict) and f.get("active", True)]
    if request.method == "POST":
        custom = []
        for field in custom_fields:
            key = field["key"]; value = request.POST.get(key, "")
            if field.get("type") == "checkbox": value = key in request.POST
            custom.append({"key": key, "label": field["label"], "type": field["type"], "value": value})
        data = {"emotions": _emotion_data(request), "activities": request.POST.get("activities", ""), "journal": request.POST.get("journal", ""), "custom": custom}
        if request.POST.get("action") == "save":
            card.draft_data = data; card.save(update_fields=["draft_data", "updated_at"]); messages.success(request, "Draft saved locally.")
        else:
            try:
                missing = [f["label"] for f, response in zip(custom_fields, custom) if f.get("required") and response["value"] in ("", False, None)]
                if missing: raise ValueError("Required fields: " + ", ".join(missing))
                submit_card(card, data, request.FILES.getlist("attachments"), request.user, request)
                messages.success(request, "Diary card locked and submitted."); return redirect("journal:detail", card_id=card.id)
            except Exception as exc: messages.error(request, str(exc))
    custom_values = {item.get("key"): item.get("value") for item in card.draft_data.get("custom", [])}
    return render(request, "journal/today.html", {"card": card, "emotions": emotions, "draft": card.draft_data, "custom_fields": custom_fields, "custom_values": custom_values, "storage": storage_status()})

@role_required(User.Role.PATIENT)
def journal_history(request):
    return render(request, "journal/history.html", {"cards": Card.objects.filter(patient=request.user).exclude(status=Card.Status.QUARANTINED)})

@role_required(User.Role.PATIENT)
def journal_trends(request):
    return _trends_response(request, Card.objects.filter(patient=request.user, status=Card.Status.SUBMITTED), "PATIENT SIGNALS")

def _card_for_user(user, card_id):
    card = get_object_or_404(Card, id=card_id, status=Card.Status.SUBMITTED)
    if user.role == User.Role.PATIENT and card.patient_id != user.id: raise Http404
    if user.role not in (User.Role.PATIENT, User.Role.REVIEWER): raise Http404
    return card

@role_required(User.Role.PATIENT, User.Role.REVIEWER)
def card_detail(request, card_id):
    card = _card_for_user(request.user, card_id)
    integrity = verify_card_integrity(card)
    if integrity != "verified":
        audit(request.user, "card.integrity_failed", card.id, {"state": integrity}, request)
        return render(request, "integrity_error.html", {"card": card, "integrity": integrity}, status=409)
    md_path = card_directory(card) / f"{card.local_date:%y%m%d}_diary.md"
    try: raw = md_path.read_text(encoding="utf-8")
    except OSError: raise Http404
    body = raw.split("---", 2)[-1]
    rendered = bleach.clean(markdown.markdown(body), tags=["h1", "h2", "h3", "p", "ul", "ol", "li", "strong", "em", "code", "pre"], strip=True)
    if request.user.role == User.Role.REVIEWER:
        meta, _ = ReviewerMetadata.objects.get_or_create(card=card, reviewer=request.user)
        if not meta.is_read: meta.is_read = True; meta.save(update_fields=["is_read", "updated_at"])
        audit(request.user, "card.viewed", card.id, request=request)
    else: meta = None
    return render(request, "card_detail.html", {"card": card, "rendered": rendered, "meta": meta, "addendum_form": AddendumForm(), "reviewer_form": ReviewerForm(instance=None, initial={"is_read": getattr(meta, "is_read", False), "starred": getattr(meta, "starred", False), "tags": ", ".join(getattr(meta, "tags", [])), "collections": ", ".join(getattr(meta, "collections", [])), "private_note": getattr(meta, "private_note", "")})})

@role_required(User.Role.PATIENT)
@require_POST
def card_addendum(request, card_id):
    card = _card_for_user(request.user, card_id); form = AddendumForm(request.POST)
    if form.is_valid():
        try: add_addendum(card, form.cleaned_data["body"], request.user, request); messages.success(request, "Addendum appended.")
        except Exception as exc: messages.error(request, str(exc))
    return redirect("journal:detail", card_id=card.id)

@role_required(User.Role.PATIENT, User.Role.REVIEWER)
def attachment_download(request, card_id, attachment_id):
    card = _card_for_user(request.user, card_id); attachment = get_object_or_404(card.attachments, id=attachment_id)
    path = card_directory(card) / attachment.stored_name
    if not path.is_file(): raise Http404
    audit(request.user, "attachment.downloaded", card.id, request=request)
    return FileResponse(path.open("rb"), as_attachment=True, filename=attachment.original_name, content_type="application/octet-stream")

@role_required(User.Role.REVIEWER)
def review_list(request):
    cards = list(Card.objects.filter(status=Card.Status.SUBMITTED).prefetch_related("reviewer_metadata"))
    emotion = request.GET.get("emotion")
    if emotion: cards = [c for c in cards if any(e.get("id") == emotion for e in c.content_index.get("emotions", []))]
    def meta(card): return next((m for m in card.reviewer_metadata.all() if m.reviewer_id == request.user.id), None)
    if request.GET.get("starred"): cards = [c for c in cards if meta(c) and meta(c).starred]
    if request.GET.get("unread"): cards = [c for c in cards if not meta(c) or not meta(c).is_read]
    tag = request.GET.get("tag", "").strip()
    if tag: cards = [c for c in cards if meta(c) and tag in meta(c).tags]
    collection = request.GET.get("collection", "").strip()
    if collection: cards = [c for c in cards if meta(c) and collection in meta(c).collections]
    if request.GET.get("sort") == "oldest": cards.reverse()
    return render(request, "review/list.html", {"cards": cards, "emotions": Emotion.objects.all(), "filter_emotion": emotion, "filters": request.GET})

@role_required(User.Role.REVIEWER)
@require_POST
def review_metadata(request, card_id):
    card = _card_for_user(request.user, card_id); meta, _ = ReviewerMetadata.objects.get_or_create(card=card, reviewer=request.user)
    meta.is_read = request.POST.get("is_read") == "on"; meta.starred = request.POST.get("starred") == "on"
    meta.tags = [x.strip() for x in request.POST.get("tags", "").split(",") if x.strip()]
    meta.collections = [x.strip() for x in request.POST.get("collections", "").split(",") if x.strip()]
    meta.private_note = request.POST.get("private_note", "").strip(); meta.save()
    audit(request.user, "reviewer.metadata_updated", card.id, request=request); messages.success(request, "Private organization saved.")
    return redirect("review:detail", card_id=card.id)

@role_required(User.Role.REVIEWER)
def review_trends(request):
    return _trends_response(request, Card.objects.filter(status=Card.Status.SUBMITTED), "THERAPIST SIGNALS")

def _trends_response(request, cards, channel):
    points = []
    for card in cards.order_by("local_date"):
        for item in card.content_index.get("emotions", []): points.append({"date": card.local_date.isoformat(), "emotion": item.get("label"), "intensity": item.get("intensity")})
    return render(request, "review/trends.html", {"points_json": json.dumps(points), "points": points, "channel": channel})

@role_required(User.Role.ADMIN)
def control_dashboard(request):
    form_def = FormDefinition.objects.filter(active=True).first()
    custom_fields = [f for f in ((form_def.schema or {}).get("fields", []) if form_def else []) if isinstance(f, dict)]
    cards = list(Card.objects.only("id", "local_date", "status", "folder_name", "quarantined_at", "purge_after").prefetch_related("attachments"))
    maintenance_cards = [{"card": card, "integrity": verify_card_integrity(card) if card.status != Card.Status.DRAFT else "draft"} for card in cards]
    return render(request, "control/dashboard.html", {"storage": storage_status(), "counts": {"users": User.objects.count(), "cards": Card.objects.count(), "submitted": Card.objects.filter(status=Card.Status.SUBMITTED).count()}, "maintenance_cards": maintenance_cards, "events": AuditEvent.objects.all()[:20], "users": User.objects.all(), "emotions": Emotion.objects.order_by("sort_order"), "custom_fields": custom_fields})

@role_required(User.Role.ADMIN)
@require_POST
def control_invite(request):
    role = request.POST.get("role"); username = request.POST.get("username", "").strip()
    if role not in (User.Role.PATIENT, User.Role.REVIEWER) or not username or User.objects.filter(username=username).exists():
        messages.error(request, "Choose a unique username and valid role."); return redirect("control:dashboard")
    if role == User.Role.PATIENT and User.objects.filter(role=User.Role.PATIENT).exists():
        messages.error(request, "This installation already has its patient account."); return redirect("control:dashboard")
    raw = secrets.token_urlsafe(32)
    Invite.objects.create(token_hash=hashlib.sha256(raw.encode()).hexdigest(), role=role, username=username, expires_at=timezone.now() + timedelta(hours=24))
    audit(request.user, "invite.created", metadata={"role": role, "username": username}, request=request)
    messages.success(request, f"Single-use invite (expires in 24h): {request.build_absolute_uri('/invite/' + raw + '/')}")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_emotion(request, emotion_id):
    emotion = get_object_or_404(Emotion, id=emotion_id)
    emotion.label = request.POST.get("label", emotion.label).strip()[:80] or emotion.label
    emotion.face = request.POST.get("face", emotion.face).strip()[:16] or emotion.face
    color = request.POST.get("color", emotion.color)
    if len(color) == 7 and color.startswith("#"): emotion.color = color
    emotion.active = request.POST.get("active") == "on"; emotion.save()
    audit(request.user, "emotion.updated", emotion.id, request=request); messages.success(request, "Emotion catalog updated.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_form_field(request):
    label = request.POST.get("label", "").strip()[:120]
    field_type = request.POST.get("type")
    allowed = {"short_text", "long_text", "number", "rating", "checkbox", "select", "date", "time"}
    if not label or field_type not in allowed:
        messages.error(request, "A label and supported field type are required."); return redirect("control:dashboard")
    current = FormDefinition.objects.filter(active=True).first()
    fields = [f for f in ((current.schema or {}).get("fields", []) if current else []) if isinstance(f, dict)]
    key_base = "field_" + (slugify(label).replace("-", "_") or "response"); key = key_base; n = 2
    while any(f.get("key") == key for f in fields): key = f"{key_base}_{n}"; n += 1
    options = [x.strip() for x in request.POST.get("options", "").split(",") if x.strip()]
    fields.append({"key": key, "label": label, "type": field_type, "required": request.POST.get("required") == "on", "options": options, "active": True})
    if current: current.active = False; current.save(update_fields=["active"]); version = current.version + 1
    else: version = 1
    FormDefinition.objects.create(version=version, active=True, schema={"fields": fields}, created_by=request.user)
    audit(request.user, "form.published", version, {"field_added": key}, request); messages.success(request, f"Form version {version} published.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_quarantine(request, card_id):
    card = get_object_or_404(Card, id=card_id)
    if request.POST.get("confirm_id") != str(card.id): messages.error(request, "The typed card ID did not match.")
    else:
        try: quarantine_card(card, request.user, request); messages.success(request, "Card quarantined for seven days.")
        except Exception as exc: messages.error(request, str(exc))
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_restore(request, card_id):
    card = get_object_or_404(Card, id=card_id, status=Card.Status.QUARANTINED)
    try: restore_card(card, request.user, request); messages.success(request, "Card restored.")
    except Exception as exc: messages.error(request, str(exc))
    return redirect("control:dashboard")
