import hashlib
import html
import json
import re
import secrets
from calendar import Calendar
from datetime import date, timedelta
from pathlib import Path
import bleach
import markdown
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Avg, Count, Max, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.cache import never_cache
from .decorators import role_required
from .context_processors import get_reviewer_patient
from .attachment_uploads import policy_context, ALLOWED_TYPES
from .attachment_delivery import attachment_response
from .forms import AttachmentLimitsForm, AddendumForm, ReviewerStatusForm, TherapistCommentForm
from .models import AttachmentSettings, AuditEvent, Card, CareRelationship, Emotion, FeedbackReport, FormDefinition, Invite, ReviewerMetadata, User
from .services import add_addendum, add_card_attachments, add_therapist_comment, audit, card_directory, card_file_inventory, comments_filename, purge_card, quarantine_card, restore_card, storage_status, submit_card, verify_card_integrity
from .security import encrypt_secret, generate_totp_secret, issue_recovery_codes, verify_privileged_credential, verify_second_factor, verify_totp
from .updater import UpdaterUnavailable, updater_request

def liveness(request): return JsonResponse({"status": "ok"})

@never_cache
def readiness(request):
    database_ok = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            database_ok = cursor.fetchone() == (1,)
    except Exception:
        pass
    storage = storage_status()
    ready = database_ok and storage["available"]
    return JsonResponse({
        "status": "ready" if ready else "unavailable",
        "version": settings.JOURNALMAX_VERSION,
        "database": "ok" if database_ok else "unavailable",
        "card_storage": storage["state"],
    }, status=200 if ready else 503)

def home(request):
    if not request.user.is_authenticated: return redirect("login")
    return redirect({User.Role.PATIENT: "journal:today", User.Role.REVIEWER: "review:list", User.Role.ADMIN: "control:dashboard"}[request.user.role])

def csrf_failure(request, reason=""):
    if request.path == reverse("login"):
        return redirect(f"{reverse('login')}?expired=1")
    return HttpResponse("Forbidden", status=403)

@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated: return redirect("home")
    login_context = {"password_only_preview": bool(settings.DEV_PASSWORD_ONLY_USERS), "expired": request.GET.get("expired") == "1"}
    if request.method == "POST":
        throttle_key = "login:" + hashlib.sha256(f"{request.META.get('REMOTE_ADDR')}:{request.POST.get('username', '').lower()}".encode()).hexdigest()
        attempts = cache.get(throttle_key, 0)
        if attempts >= 5: return render(request, "login.html", {**login_context, "username": request.POST.get("username", ""), "throttled": True}, status=429)
        user = authenticate(request, username=request.POST.get("username", ""), password=request.POST.get("password", ""))
        if user and user.is_active:
            if user.role in (User.Role.REVIEWER, User.Role.ADMIN) and user.totp_confirmed and user.username not in settings.DEV_PASSWORD_ONLY_USERS:
                if not verify_second_factor(user, request.POST.get("totp", "")):
                    cache.set(throttle_key, attempts + 1, 900)
                    messages.error(request, "A valid authenticator code is required."); return render(request, "login.html", {**login_context, "username": request.POST.get("username", "")})
            cache.delete(throttle_key); login(request, user); audit(user, "auth.login", request=request); return redirect("home")
        cache.set(throttle_key, attempts + 1, 900)
        messages.error(request, "Access denied. Check your credentials.")
    return render(request, "login.html", login_context)

@require_http_methods(["GET", "POST"])
def accept_invite(request, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    invite = get_object_or_404(Invite, token_hash=digest, used_at__isnull=True, expires_at__gt=timezone.now())
    secret_key = f"invite_totp_{invite.id}"
    secret = request.session.get(secret_key) or generate_totp_secret(); request.session[secret_key] = secret
    if request.method == "POST":
        password = request.POST.get("password", "")
        if len(password) < 12: messages.error(request, "Passphrase must be at least 12 characters.")
        elif request.POST.get("totp", "").strip() and invite.role in (User.Role.REVIEWER, User.Role.ADMIN) and not verify_totp(secret, request.POST.get("totp")):
            messages.error(request, "Authenticator code did not verify.")
        else:
            totp_enabled = invite.role in (User.Role.REVIEWER, User.Role.ADMIN) and bool(request.POST.get("totp", "").strip())
            user = User.objects.create_user(username=invite.username, password=password, role=invite.role, totp_confirmed=totp_enabled, totp_secret_encrypted=encrypt_secret(secret) if totp_enabled else "")
            invite.used_at = timezone.now(); invite.save(update_fields=["used_at"]); request.session.pop(secret_key, None)
            codes = issue_recovery_codes(user) if totp_enabled else []
            audit(user, "invite.accepted");
            if codes: return render(request, "recovery_codes.html", {"codes": codes})
            messages.success(request, "Account activated. You can enroll an authenticator later.")
            return redirect("login")
    return render(request, "accept_invite.html", {"invite": invite, "totp_secret": secret})

@require_POST
def logout_view(request):
    if request.user.is_authenticated: audit(request.user, "auth.logout", request=request)
    logout(request); return redirect("login")

@role_required(User.Role.PATIENT, User.Role.REVIEWER, User.Role.ADMIN)
@require_POST
def session_keepalive(request):
    request.session.set_expiry(settings.SESSION_IDLE_TIMEOUT)
    return JsonResponse({"ok": True})

@role_required(User.Role.PATIENT, User.Role.REVIEWER, User.Role.ADMIN)
@require_POST
def feedback_submit(request):
    wants_json = "application/json" in request.headers.get("Accept", "")
    body = request.POST.get("body", "").strip()
    subject = request.POST.get("subject", "").strip()
    kind = request.POST.get("kind", FeedbackReport.Kind.FEEDBACK)
    if kind not in FeedbackReport.Kind.values: kind = FeedbackReport.Kind.FEEDBACK
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()): next_url = reverse("home")
    if not subject or not body:
        if wants_json: return JsonResponse({"ok": False, "message": "Add a subject and note before sending feedback."}, status=400)
        messages.error(request, "Add a subject and note before sending feedback.")
        return redirect(next_url)
    FeedbackReport.objects.create(author=request.user, author_name=request.user.username, author_role=request.user.role, kind=kind, subject=subject[:140], body=body[:4000], page=request.POST.get("page", "")[:500])
    audit(request.user, "feedback.submitted", metadata={"kind": kind}, request=request)
    if wants_json: return JsonResponse({"ok": True, "message": "Feedback sent to the Journalmax administrator."})
    messages.success(request, "Feedback sent to the Journalmax administrator.")
    return redirect(next_url)

def _emotion_data(request):
    selected = set(request.POST.getlist("emotions")); result = []
    for emotion in Emotion.objects.filter(active=True, slug__in=selected):
        try: intensity = max(1, min(5, int(request.POST.get(f"intensity_{emotion.slug}")))) if request.POST.get(f"intensity_{emotion.slug}") else None
        except ValueError: intensity = None
        result.append({"id": emotion.slug, "label": emotion.label, "intensity": intensity, "note": request.POST.get(f"note_{emotion.slug}", "").strip()})
    return result

@role_required(User.Role.PATIENT)
@require_http_methods(["GET", "POST"])
def journal_today(request):
    local_date = timezone.localdate()
    form_def = FormDefinition.objects.filter(active=True).first()
    card, _ = Card.objects.get_or_create(patient=request.user, local_date=local_date, defaults={"form_version": form_def.version if form_def else 1})
    if card.status == Card.Status.QUARANTINED:
        return render(request, "journal/recently_deleted.html", {"card": card})
    if card.status != Card.Status.DRAFT: return redirect("journal:detail", card_id=card.id)
    emotions = list(Emotion.objects.filter(active=True).order_by("sort_order", "label"))
    draft_emotions = {item.get("id"): item for item in card.draft_data.get("emotions", [])}
    for emotion in emotions:
        draft_emotion = draft_emotions.get(emotion.slug, {})
        emotion.draft_intensity = str(draft_emotion.get("intensity") or "")
        emotion.draft_note = draft_emotion.get("note", "")
    custom_fields = [f for f in ((form_def.schema or {}).get("fields", []) if form_def else []) if isinstance(f, dict) and f.get("active", True)]
    if request.method == "POST":
        custom = []
        for field in custom_fields:
            key = field["key"]; value = request.POST.get(key, "")
            if field.get("type") == "checkbox": value = key in request.POST
            custom.append({"key": key, "label": field["label"], "type": field["type"], "value": value})
        data = {"emotions": _emotion_data(request), "custom": custom}
        if request.POST.get("action") == "save":
            card.draft_data = data; card.save(update_fields=["draft_data", "updated_at"]); messages.success(request, "Draft saved locally.")
        else:
            try:
                missing = [f["label"] for f, response in zip(custom_fields, custom) if f.get("required") and response["value"] in ("", False, None)]
                if missing: raise ValueError("Required fields: " + ", ".join(missing))
                submit_card(card, data, request.FILES.getlist("attachments"), request.user, request)
                messages.success(request, "Diary card locked and submitted.")
                if request.headers.get("Accept") == "application/json":
                    return JsonResponse({"redirect": reverse("journal:detail", args=[card.id])})
                return redirect("journal:detail", card_id=card.id)
            except Exception as exc:
                if request.headers.get("Accept") == "application/json":
                    return JsonResponse({"error": str(exc)}, status=400)
                messages.error(request, str(exc))
    custom_values = {item.get("key"): item.get("value") for item in card.draft_data.get("custom", [])}
    return render(request, "journal/today.html", {**policy_context(), "card": card, "emotions": emotions, "draft": card.draft_data, "custom_fields": custom_fields, "custom_values": custom_values, "storage": storage_status()})

@role_required(User.Role.PATIENT)
def journal_history(request):
    cards = list(Card.objects.filter(patient=request.user).exclude(status=Card.Status.QUARANTINED).annotate(
        unread_comment_count=Count("therapist_comments", filter=Q(therapist_comments__patient_read_at__isnull=True)),
    ).prefetch_related("addenda", "therapist_comments", "attachments"))
    catalog = {emotion.slug: emotion for emotion in Emotion.objects.all()}
    for card in cards:
        card.display_emotions = [{**item, "face": getattr(catalog.get(item.get("id")), "face", "◆"), "color": getattr(catalog.get(item.get("id")), "color", "#79ffe1")} for item in card.content_index.get("emotions", [])]
        card.prominent_emotion = max(card.display_emotions, key=lambda item: int(item.get("intensity", 1)), default=None)
    card_by_date = {card.local_date: card for card in cards}
    today = timezone.localdate()
    try:
        year, month = map(int, request.GET.get("month", "").split("-"))
        month_start = date(year, month, 1)
    except (TypeError, ValueError): month_start = today.replace(day=1)
    previous_day = month_start - timedelta(days=1)
    next_month = date(month_start.year + (month_start.month == 12), 1 if month_start.month == 12 else month_start.month + 1, 1)
    calendar_weeks = [[{"date": day, "in_month": day.month == month_start.month, "is_today": day == today, "card": card_by_date.get(day)} for day in week] for week in Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month)]
    if request.GET.get("sort") == "oldest": cards.reverse()
    return render(request, "journal/history.html", {"cards": cards, "filters": request.GET, "calendar_weeks": calendar_weeks, "month_start": month_start, "previous_month": previous_day.strftime("%Y-%m"), "next_month": next_month.strftime("%Y-%m")})

@role_required(User.Role.PATIENT)
def journal_trends(request):
    cards = Card.objects.filter(patient=request.user, status=Card.Status.SUBMITTED)
    return render(request, "journal/trends.html", _signals_context(cards, "journal:detail", reviewer_mode=False, range_key=request.GET.get("range", "all")))

def _signals_context(cards, detail_route, reviewer_mode, range_key="all"):
    range_options = [
        ("all", "All time", None), ("7d", "Past 7 days", 7), ("30d", "Past 30 days", 30),
        ("90d", "Past 3 months", 90), ("180d", "Past 6 months", 180), ("365d", "Past year", 365),
    ]
    ranges = {key: {"label": label, "days": days} for key, label, days in range_options}
    if range_key not in ranges: range_key = "all"
    total_checkin_count = cards.count()
    if ranges[range_key]["days"]:
        cutoff = timezone.localdate() - timedelta(days=ranges[range_key]["days"] - 1)
        cards = cards.filter(local_date__gte=cutoff)
    cards = list(cards.order_by("local_date"))
    catalog = {emotion.slug: emotion for emotion in Emotion.objects.all()}
    summaries = {}
    timeline = []
    for card in cards:
        signals = {}
        for item in card.content_index.get("emotions", []):
            key = item.get("id") or slugify(item.get("label", "emotion"))
            intensity = max(1, min(5, int(item.get("intensity", 1))))
            emotion = catalog.get(key)
            label = item.get("label") or getattr(emotion, "label", key.replace("-", " ").title())
            signals[key] = intensity
            summary = summaries.setdefault(key, {"id": key, "label": label, "face": getattr(emotion, "face", "◆"), "color": getattr(emotion, "color", "#79ffe1"), "count": 0, "total": 0, "peak": 0, "last_date": card.local_date})
            summary["count"] += 1; summary["total"] += intensity
            summary["peak"] = max(summary["peak"], intensity); summary["last_date"] = card.local_date
        card.detail_url = reverse(detail_route, args=[card.id])
        timeline.append({"date": card.local_date.isoformat(), "url": card.detail_url, "signals": signals, "total_intensity": sum(signals.values())})
    emotion_rows = sorted(summaries.values(), key=lambda item: (-item["count"], item["label"]))
    for item in emotion_rows: item["average"] = round(item["total"] / item["count"], 1)
    strongest = max(emotion_rows, key=lambda item: (item["average"], item["count"]), default=None)
    context = {
        "checkin_count": len(cards), "signal_count": sum(item["count"] for item in emotion_rows),
        "first_date": cards[0].local_date if cards else None, "last_date": cards[-1].local_date if cards else None,
        "emotion_rows": emotion_rows, "most_present": emotion_rows[0] if emotion_rows else None,
        "strongest": strongest, "timeline_json": json.dumps(timeline),
        "emotions_json": json.dumps([{"id": item["id"], "label": item["label"], "face": item["face"], "color": item["color"]} for item in emotion_rows]),
        "recent_cards": list(reversed(cards[-5:])), "reviewer_mode": reviewer_mode,
        "total_checkin_count": total_checkin_count, "selected_range": range_key,
        "selected_range_label": ranges[range_key]["label"],
        "range_options": [{"key": key, "label": label} for key, label, _ in range_options],
        "high_intensity_threshold": 8,
        "very_high_intensity_threshold": 12,
    }
    return context

def _card_for_user(user, card_id):
    card = get_object_or_404(Card, id=card_id, status=Card.Status.SUBMITTED)
    if user.role == User.Role.PATIENT and card.patient_id != user.id: raise Http404
    if user.role == User.Role.REVIEWER and not CareRelationship.objects.filter(therapist=user, patient_id=card.patient_id).exists(): raise Http404
    if user.role not in (User.Role.PATIENT, User.Role.REVIEWER): raise Http404
    return card

def _emotion_detail_html(emotions):
    items = []
    for emotion in emotions:
        intensity = max(1, min(5, int(emotion.get("intensity", 1))))
        label = html.escape(str(emotion.get("label", "Emotion")))
        face = html.escape(str(emotion.get("face", "◆")))
        color = html.escape(str(emotion.get("color", "#ffffff")), quote=True)
        note = html.escape(str(emotion.get("note", "")).strip())
        note_html = f"<p>{note}</p>" if note else ""
        items.append(f'<div class="detail-emotion" style="--emotion:{color};--level:{intensity}" aria-label="{label}, intensity {intensity} of 5"><span>{face}</span><div><b>{label}</b><small>INTENSITY {intensity} / 5</small>{note_html}</div></div>')
    return '<div class="detail-emotions">' + "".join(items) + "</div>"

def _collapsible_markdown(rendered, emotions=None):
    headings = list(re.finditer(r"<h2>(.*?)</h2>", rendered, flags=re.DOTALL))
    if not headings: return rendered
    output = [rendered[:headings[0].start()]]
    for index, heading in enumerate(headings):
        body_start = heading.end()
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(rendered)
        body = rendered[body_start:body_end]
        heading_text = html.unescape(re.sub(r"<[^>]+>", " ", heading.group(1))).strip()
        if heading_text.casefold() == "significant emotions" and emotions:
            body = _emotion_detail_html(emotions)
        plain_body = html.unescape(re.sub(r"<[^>]+>", " ", body)).strip()
        open_attribute = "" if not plain_body or plain_body.casefold() == "no response" else " open"
        output.append(f'<details class="diary-section"{open_attribute}><summary>{heading.group(1)}</summary><div class="diary-section-body">{body}</div></details>')
    return "".join(output)

@role_required(User.Role.PATIENT, User.Role.REVIEWER)
def card_detail(request, card_id):
    card = _card_for_user(request.user, card_id)
    integrity = verify_card_integrity(card)
    if integrity != "verified":
        audit(request.user, "card.integrity_failed", card.id, {"state": integrity}, request)
        return render(request, "integrity_error.html", {"card": card, "integrity": integrity}, status=503 if integrity == "offline" else 409)
    md_path = card_directory(card) / f"{card.local_date:%y%m%d}_diary.md"
    try: raw = md_path.read_text(encoding="utf-8")
    except OSError: raise Http404
    body = raw.split("---", 2)[-1]
    rendered = bleach.clean(markdown.markdown(body), tags=["h1", "h2", "h3", "p", "ul", "ol", "li", "strong", "em", "code", "pre"], strip=True)
    catalog = {emotion.slug: emotion for emotion in Emotion.objects.all()}
    display_emotions = [{**item, "face": getattr(catalog.get(item.get("id")), "face", "◆"), "color": getattr(catalog.get(item.get("id")), "color", "#ffffff")} for item in card.content_index.get("emotions", [])]
    rendered = _collapsible_markdown(rendered, display_emotions)
    raw_mode = request.GET.get("view") == "raw"
    raw_documents = [{"filename": md_path.name, "content": raw}]
    if raw_mode:
        for item in card.addenda.all():
            try: content = (card_directory(card) / item.filename).read_text(encoding="utf-8")
            except OSError: content = "[MARKDOWN FILE UNAVAILABLE]"
            raw_documents.append({"filename": item.filename, "content": content})
        if card.therapist_comments.exists():
            filename = comments_filename(card)
            try: content = (card_directory(card) / filename).read_text(encoding="utf-8")
            except OSError: content = "[COMMENTS FILE UNAVAILABLE]"
            raw_documents.append({"filename": filename, "content": content, "label": "APPEND-ONLY COMMENT LOG"})
    if request.user.role == User.Role.REVIEWER:
        meta, _ = ReviewerMetadata.objects.get_or_create(card=card, reviewer=request.user)
        if not meta.is_read: meta.is_read = True; meta.save(update_fields=["is_read", "updated_at"])
        audit(request.user, "card.viewed", card.id, request=request)
    else:
        meta = None
        card.therapist_comments.filter(patient_read_at__isnull=True).update(patient_read_at=timezone.now())
    attachments = list(card.attachments.all())
    image_attachments = [item for item in attachments if item.content_type.startswith("image/")]
    media_attachments = [item for item in attachments if item.content_type.startswith(("audio/", "video/"))]
    file_attachments = [item for item in attachments if not item.content_type.startswith(("image/", "audio/", "video/"))]
    return render(request, "card_detail.html", {**policy_context(sum(item.size for item in attachments)), "media_attachments": media_attachments, "card": card, "rendered": rendered, "raw_mode": raw_mode, "raw_documents": raw_documents, "meta": meta, "addendum_form": AddendumForm(), "reviewer_status_form": ReviewerStatusForm(initial={"is_read": getattr(meta, "is_read", False)}), "comment_form": TherapistCommentForm(), "image_attachments": image_attachments, "file_attachments": file_attachments})

@role_required(User.Role.PATIENT)
@require_POST
def card_addendum(request, card_id):
    card = _card_for_user(request.user, card_id); form = AddendumForm(request.POST)
    if form.is_valid():
        try: add_addendum(card, form.cleaned_data["body"], request.user, request); messages.success(request, "Addendum appended.")
        except Exception as exc: messages.error(request, str(exc))
    return redirect("journal:detail", card_id=card.id)

@role_required(User.Role.PATIENT)
@require_POST
def card_attachment_add(request, card_id):
    card = _card_for_user(request.user, card_id)
    try:
        add_card_attachments(card, request.FILES.getlist("attachments"), request.user, request)
        messages.success(request, "Attachment added to the submitted card.")
    except Exception as exc:
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"error": str(exc)}, status=400)
        messages.error(request, str(exc))
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"redirect": reverse("journal:detail", args=[card.id])})
    return redirect("journal:detail", card_id=card.id)

@role_required(User.Role.PATIENT, User.Role.REVIEWER)
@require_http_methods(["GET", "HEAD"])
def attachment_download(request, card_id, attachment_id):
    card = _card_for_user(request.user, card_id); attachment = get_object_or_404(card.attachments, id=attachment_id)
    if not storage_status()["available"]: return HttpResponse("Card storage offline", status=503)
    path = card_directory(card) / attachment.stored_name
    if not path.is_file(): raise Http404
    inline = request.GET.get("inline") == "1" and attachment.content_type in ALLOWED_TYPES
    audit(request.user, "attachment.viewed" if inline else "attachment.downloaded", card.id, request=request)
    return attachment_response(request, path, attachment, inline)

@role_required(User.Role.REVIEWER)
def review_list(request):
    patient = get_reviewer_patient(request)
    cards = list(Card.objects.filter(status=Card.Status.SUBMITTED, patient=patient).prefetch_related("reviewer_metadata", "addenda", "attachments", "therapist_comments")) if patient else []
    catalog = {emotion.slug: emotion for emotion in Emotion.objects.all()}
    def meta(card): return next((m for m in card.reviewer_metadata.all() if m.reviewer_id == request.user.id), None)
    for card in cards:
        card.is_read = bool(meta(card) and meta(card).is_read)
        card.display_emotions = [{**item, "face": getattr(catalog.get(item.get("id")), "face", "◆"), "color": getattr(catalog.get(item.get("id")), "color", "#79ffe1")} for item in card.content_index.get("emotions", [])]
        card.prominent_emotion = max(card.display_emotions, key=lambda item: int(item.get("intensity", 1)), default=None)
    unread_count = sum(not card.is_read for card in cards)
    card_by_date = {card.local_date: card for card in cards}
    today = timezone.localdate()
    try:
        year, month = map(int, request.GET.get("month", "").split("-"))
        month_start = date(year, month, 1)
    except (TypeError, ValueError):
        month_start = today.replace(day=1)
    previous_day = month_start - timedelta(days=1)
    next_month = date(month_start.year + (month_start.month == 12), 1 if month_start.month == 12 else month_start.month + 1, 1)
    calendar_weeks = [[{"date": day, "in_month": day.month == month_start.month, "is_today": day == today, "card": card_by_date.get(day)} for day in week] for week in Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month)]
    if request.GET.get("status") == "unread": cards = [card for card in cards if not card.is_read]
    if request.GET.get("sort") == "oldest": cards.reverse()
    return render(request, "review/list.html", {"cards": cards, "unread_count": unread_count, "filters": request.GET, "calendar_weeks": calendar_weeks, "month_start": month_start, "previous_month": previous_day.strftime("%Y-%m"), "next_month": next_month.strftime("%Y-%m")})

@role_required(User.Role.REVIEWER)
@require_POST
def review_patient_select(request):
    patient = get_object_or_404(User, id=request.POST.get("patient_id"), role=User.Role.PATIENT, is_active=True, therapist_assignments__therapist=request.user)
    request.session["review_patient_id"] = patient.id
    request._reviewer_patient = patient
    audit(request.user, "reviewer.patient_selected", patient.id, request=request)
    destination = request.POST.get("next", "")
    if destination.startswith("/review/") and url_has_allowed_host_and_scheme(destination, allowed_hosts={request.get_host()}):
        return redirect(destination)
    return redirect("review:list")

@role_required(User.Role.REVIEWER)
@require_POST
def review_metadata(request, card_id):
    card = _card_for_user(request.user, card_id); meta, _ = ReviewerMetadata.objects.get_or_create(card=card, reviewer=request.user)
    form = ReviewerStatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Review details could not be saved."); return redirect("review:detail", card_id=card.id)
    meta.is_read = form.cleaned_data["is_read"]
    meta.save(update_fields=["is_read", "updated_at"])
    audit(request.user, "reviewer.status_updated", card.id, {"is_read": meta.is_read}, request); messages.success(request, "Review status saved.")
    if meta.is_read: return redirect("review:detail", card_id=card.id)
    return redirect("review:list")

@role_required(User.Role.REVIEWER)
@require_POST
def review_comment(request, card_id):
    card = _card_for_user(request.user, card_id)
    form = TherapistCommentForm(request.POST)
    if form.is_valid():
        try: add_therapist_comment(card, form.cleaned_data["body"], request.user, request); messages.success(request, "Comment added. The patient can now read it.")
        except Exception as exc: messages.error(request, str(exc))
    else: messages.error(request, "Write a comment before posting.")
    return redirect("review:detail", card_id=card.id)

@role_required(User.Role.REVIEWER)
def review_trends(request):
    patient = get_reviewer_patient(request)
    cards = Card.objects.filter(status=Card.Status.SUBMITTED, patient=patient) if patient else Card.objects.none()
    return render(request, "journal/trends.html", _signals_context(cards, "review:detail", reviewer_mode=True, range_key=request.GET.get("range", "all")))

def _recent_activity(events):
    account_by_id = {str(account.id): account for account in User.objects.all()}
    card_by_id = {str(card.id): card for card in Card.objects.only("id", "local_date")}
    emotion_by_id = {str(emotion.id): emotion for emotion in Emotion.objects.all()}
    action_labels = {
        "auth.login": "signed in",
        "auth.logout": "signed out",
        "invite.accepted": "activated an invited account",
        "invite.created": "created an account invitation",
        "card.submitted": "submitted a diary entry",
        "card.addendum_created": "added context to a diary entry",
        "card.viewed": "opened a diary entry",
        "card.integrity_failed": "encountered an entry integrity conflict",
        "attachment.downloaded": "downloaded an entry attachment",
        "attachment.viewed": "viewed an entry attachment",
        "card.quarantined": "moved an entry to Recently Deleted",
        "card.restored": "restored a deleted entry",
        "card.purged": "permanently removed an expired entry",
        "reviewer.patient_selected": "switched the active patient",
        "reviewer.status_updated": "updated an entry’s review status",
        "reviewer.comment_added": "left a patient-visible comment",
        "account.updated": "updated an account",
        "account.deleted": "deleted an account",
        "emotion.created": "added an emotion",
        "emotion.updated": "updated an emotion",
        "emotion.removed": "removed an emotion from new forms",
        "form.published": "published a new form version",
        "system.update_checked": "checked for a JOURNALMAX update",
        "system.update_requested": "authorized a JOURNALMAX update",
        "system.legacy_credentials_purged": "purged legacy updater credentials",
    }
    activity = []
    for event in events:
        target_label = ""
        target_url = ""
        if event.action.startswith("card.") or event.action in {"attachment.downloaded", "attachment.viewed", "reviewer.status_updated", "reviewer.comment_added"}:
            card = card_by_id.get(event.target_id)
            if card:
                target_label = f"ENTRY {card.local_date:%Y-%m-%d}"
                target_url = f"?entry={card.id}#maintenance"
        elif event.action in {"account.updated", "account.deleted", "reviewer.patient_selected"}:
            account = account_by_id.get(event.target_id)
            target_label = account.username if account else event.metadata.get("username", "")
            if account:
                target_url = f"?account={account.id}#accounts"
        elif event.action.startswith("emotion."):
            emotion = emotion_by_id.get(event.target_id)
            target_label = emotion.label if emotion else "EMOTION CATALOG"
            target_url = "#emotions"
        elif event.action == "form.published":
            target_label = f"FORM VERSION {event.target_id}" if event.target_id else "FORM BUILDER"
            target_url = "#form"
        elif event.action == "invite.created":
            target_label = event.metadata.get("username", "NEW ACCOUNT")
        elif event.action.startswith("system.update_"):
            target_label = f"JOURNALMAX {event.target_id}" if event.target_id else "UPDATE SERVICE"
            target_url = reverse("control:updates")
        actor_label = (event.actor.get_full_name().strip() or event.actor.username) if event.actor else "JOURNALMAX SYSTEM"
        activity.append({
            "event": event,
            "actor_label": actor_label,
            "actor_url": f"?account={event.actor_id}#accounts" if event.actor_id and str(event.actor_id) in account_by_id else "",
            "action_label": action_labels.get(event.action, event.action.replace(".", " ").replace("_", " ")),
            "target_label": target_label,
            "target_url": target_url,
        })
    return activity

@role_required(User.Role.ADMIN)
def control_dashboard(request, limits_form=None, response_status=200):
    form_def = FormDefinition.objects.filter(active=True).first()
    custom_fields, active_fields, _ = _form_builder_state(request)
    storage = storage_status()
    cards = list(Card.objects.only("id", "local_date", "status", "folder_name", "quarantined_at", "purge_after").prefetch_related("attachments", "addenda"))
    maintenance_cards = [{"card": card, "integrity": verify_card_integrity(card, storage) if card.status != Card.Status.DRAFT else "draft", **card_file_inventory(card, storage)} for card in cards]
    stored_file_count = sum(item["file_count"] for item in maintenance_cards)
    stored_file_bytes = sum(item["total_size"] for item in maintenance_cards)
    journal_capacity = stored_file_bytes + storage.get("free_bytes", 0) if storage["available"] else 0
    journal_storage_percent = round(stored_file_bytes / journal_capacity * 100, 2) if journal_capacity else 0
    journal_volume_percent = round(stored_file_bytes / storage["total_bytes"] * 100, 2) if storage["available"] and storage["total_bytes"] else 0
    users = list(User.objects.annotate(card_count=Count("cards", distinct=True), recovery_count=Count("recovery_codes", filter=Q(recovery_codes__used_at__isnull=True), distinct=True)).order_by("role", "username"))
    patient_accounts = [account for account in users if account.role == User.Role.PATIENT]
    relationships = list(CareRelationship.objects.select_related("therapist", "patient"))
    patient_ids_by_therapist = {}
    therapists_by_patient = {}
    for relationship in relationships:
        patient_ids_by_therapist.setdefault(relationship.therapist_id, set()).add(relationship.patient_id)
        therapists_by_patient.setdefault(relationship.patient_id, []).append(relationship.therapist)
    admin_count = sum(account.role == User.Role.ADMIN for account in users)
    for account in users:
        account.assigned_patient_ids = patient_ids_by_therapist.get(account.id, set())
        account.assigned_therapists = sorted(therapists_by_patient.get(account.id, []), key=lambda therapist: therapist.username.lower())
        if account.id == request.user.id: account.delete_block = "The account controlling this session cannot delete itself."
        elif account.card_count: account.delete_block = "This account owns diary cards and cannot be deleted without a separate data-transfer workflow."
        elif account.role == User.Role.ADMIN and admin_count <= 1: account.delete_block = "The installation must retain at least one administrator."
        else: account.delete_block = ""
    events = list(AuditEvent.objects.select_related("actor")[:100])
    emotions = list(Emotion.objects.order_by("sort_order"))
    feedback_reports = list(FeedbackReport.objects.select_related("author", "archived_by"))
    return render(request, "control/dashboard.html", {**policy_context(), "attachment_limits_form": limits_form if limits_form is not None else AttachmentLimitsForm(instance=AttachmentSettings.current()), "journalmax_version": settings.JOURNALMAX_VERSION, "storage": storage, "journal_storage": {"bytes": stored_file_bytes, "capacity": journal_capacity, "percent": journal_storage_percent, "volume_percent": journal_volume_percent}, "counts": {"users": User.objects.count(), "cards": Card.objects.count(), "submitted": Card.objects.filter(status=Card.Status.SUBMITTED).count(), "files": stored_file_count}, "maintenance_cards": maintenance_cards, "events": events, "recent_activity": _recent_activity(events[:12]), "users": users, "patient_accounts": patient_accounts, "emotions": emotions, "custom_fields": custom_fields, "form_version": getattr(form_def, "version", None), "has_staged_changes": custom_fields != active_fields, "feedback_reports": feedback_reports, "active_feedback_count": sum(report.archived_at is None for report in feedback_reports)}, status=response_status)

@role_required(User.Role.ADMIN)
@require_POST
def control_feedback_important(request, report_id):
    report = get_object_or_404(FeedbackReport, id=report_id)
    report.important = not report.important
    report.save(update_fields=["important", "updated_at"])
    audit(request.user, "feedback.importance_updated", report.id, {"important": report.important}, request)
    messages.success(request, "Feedback priority updated.")
    return redirect(reverse("control:dashboard") + "#feedback")

@role_required(User.Role.ADMIN)
@require_POST
def control_feedback_archive(request, report_id):
    report = get_object_or_404(FeedbackReport, id=report_id)
    report.archived_at = None if report.archived_at else timezone.now()
    report.archived_by = None if report.archived_at is None else request.user
    report.save(update_fields=["archived_at", "archived_by", "updated_at"])
    audit(request.user, "feedback.archive_updated", report.id, {"archived": report.archived_at is not None}, request)
    messages.success(request, "Feedback archive updated.")
    return redirect(reverse("control:dashboard") + "#feedback")

@role_required(User.Role.ADMIN)
def control_updates(request):
    try:
        updater_status = {"connected": True, **updater_request("status")}
    except UpdaterUnavailable as exc:
        updater_status = {"connected": False, "error": str(exc), "installed_version": settings.JOURNALMAX_VERSION, "job": {"phase": "offline", "message": "The host updater is not connected in this environment."}}
    try:
        changelog = json.loads((settings.BASE_DIR / "CHANGELOG.json").read_text(encoding="utf-8")).get(settings.JOURNALMAX_VERSION, [])
    except (OSError, json.JSONDecodeError):
        changelog = []
    return render(request, "control/updates.html", {"journalmax_version": settings.JOURNALMAX_VERSION, "updater_status": updater_status, "installed_changelog": changelog})

def _updater_json(action, **parameters):
    try:
        return JsonResponse({"connected": True, **updater_request(action, **parameters)})
    except UpdaterUnavailable as exc:
        return JsonResponse({"connected": False, "error": str(exc)}, status=503)

@never_cache
@role_required(User.Role.ADMIN)
@require_http_methods(["GET"])
def control_update_status(request):
    return _updater_json("status")

@never_cache
@role_required(User.Role.ADMIN)
@require_POST
def control_update_check(request):
    response = _updater_json("check")
    if response.status_code == 200:
        payload = json.loads(response.content)
        latest = payload.get("latest", {}).get("version", "")
        audit(request.user, "system.update_checked", latest, {"update_available": payload.get("update_available", False)}, request)
    return response

@never_cache
@role_required(User.Role.ADMIN)
@require_POST
def control_update_apply(request):
    if not verify_privileged_credential(request.user, request.POST.get("code", "")):
        requirement = "authenticator or recovery code" if request.user.totp_confirmed else "current passphrase"
        return JsonResponse({"connected": True, "error": f"A valid {requirement} is required."}, status=403)
    version = request.POST.get("version", "").strip()
    response = _updater_json("apply", version=version)
    if response.status_code == 200:
        audit(request.user, "system.update_requested", version, request=request)
    return response

@never_cache
@role_required(User.Role.ADMIN)
@require_http_methods(["GET"])
def control_update_tools_status(request):
    return _updater_json("tools_status")

@never_cache
@role_required(User.Role.ADMIN)
@require_POST
def control_update_credentials_purge(request):
    if not verify_privileged_credential(request.user, request.POST.get("code", "")):
        requirement = "authenticator or recovery code" if request.user.totp_confirmed else "current passphrase"
        return JsonResponse({"connected": True, "error": f"A valid {requirement} is required."}, status=403)
    response = _updater_json("purge_legacy_github_credentials")
    if response.status_code == 200:
        payload = json.loads(response.content)
        result = payload.get("legacy_github_credentials", {})
        audit(request.user, "system.legacy_credentials_purged", metadata={"removed_count": result.get("removed_count", 0), "clean": result.get("clean", False)}, request=request)
    return response

@role_required(User.Role.ADMIN)
def control_activity(request):
    events = list(AuditEvent.objects.select_related("actor")[:12])
    items = []
    for item in _recent_activity(events):
        event = item["event"]
        local_created = timezone.localtime(event.created_at)
        items.append({
            "id": event.id,
            "created_at": event.created_at.isoformat(),
            "time": local_created.strftime("%H:%M"),
            "date": local_created.strftime("%b %d").upper(),
            "actor_label": item["actor_label"],
            "actor_url": item["actor_url"],
            "action_label": item["action_label"],
            "target_label": item["target_label"],
            "target_url": item["target_url"],
            "action": event.action,
        })
    response = JsonResponse({"items": items, "counts": {"users": User.objects.count(), "submitted": Card.objects.filter(status=Card.Status.SUBMITTED).count()}})
    response["Cache-Control"] = "no-store"
    return response

@role_required(User.Role.ADMIN)
@require_POST
def control_invite(request):
    role = request.POST.get("role"); username = request.POST.get("username", "").strip()
    if role not in (User.Role.PATIENT, User.Role.REVIEWER) or not username or User.objects.filter(username=username).exists():
        messages.error(request, "Choose a unique username and valid role."); return redirect("control:dashboard")
    raw = secrets.token_urlsafe(32)
    Invite.objects.create(token_hash=hashlib.sha256(raw.encode()).hexdigest(), role=role, username=username, expires_at=timezone.now() + timedelta(hours=24))
    audit(request.user, "invite.created", metadata={"role": role, "username": username}, request=request)
    messages.success(request, f"Single-use invite (expires in 24h): {request.build_absolute_uri('/invite/' + raw + '/')}")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_account(request, user_id):
    account = get_object_or_404(User, id=user_id)
    username = request.POST.get("username", "").strip()[:150]
    if not username or User.objects.filter(username=username).exclude(id=account.id).exists():
        messages.error(request, "Choose a unique account name."); return redirect("control:dashboard")
    if "role" in request.POST and request.POST.get("role") != account.role:
        messages.error(request, "Account roles are permanent after creation."); return redirect("control:dashboard")
    new_password = request.POST.get("new_password", "")
    confirm_password = request.POST.get("confirm_password", "")
    if new_password or confirm_password:
        if new_password != confirm_password:
            messages.error(request, "The new passphrase confirmation did not match."); return redirect("control:dashboard")
        try: validate_password(new_password, user=account)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages)); return redirect("control:dashboard")
    new_totp_secret = request.POST.get("new_totp_secret", "").replace(" ", "").upper()
    new_totp_code = request.POST.get("new_totp_code", "").strip()
    rotate_totp = bool(new_totp_secret or new_totp_code)
    disable_totp = request.POST.get("disable_totp") == "1"
    if rotate_totp and disable_totp:
        messages.error(request, "Choose either authenticator enrollment or removal, not both."); return redirect("control:dashboard")
    if rotate_totp:
        if account.role not in (User.Role.REVIEWER, User.Role.ADMIN):
            messages.error(request, "Patient accounts do not use an authenticator code."); return redirect("control:dashboard")
        if not re.fullmatch(r"[A-Z2-7]{32}", new_totp_secret):
            messages.error(request, "Generate a valid new authenticator setup key."); return redirect("control:dashboard")
        try: valid_totp = verify_totp(new_totp_secret, new_totp_code)
        except Exception: valid_totp = False
        if not valid_totp:
            messages.error(request, "The verification code did not match the new authenticator."); return redirect("control:dashboard")
    selected_patient_ids = set()
    if account.role == User.Role.REVIEWER:
        try: selected_patient_ids = {int(value) for value in request.POST.getlist("patient_ids")}
        except (TypeError, ValueError):
            messages.error(request, "The patient assignment list was invalid."); return redirect("control:dashboard")
        valid_ids = set(User.objects.filter(id__in=selected_patient_ids, role=User.Role.PATIENT).values_list("id", flat=True))
        if valid_ids != selected_patient_ids:
            messages.error(request, "Only patient accounts can be assigned to a therapist."); return redirect("control:dashboard")
    with transaction.atomic():
        account.username = username
        if account.id != request.user.id: account.is_active = request.POST.get("is_active") == "on"
        update_fields = ["username", "is_active"]
        if new_password:
            account.set_password(new_password); update_fields.append("password")
        if rotate_totp:
            account.totp_secret_encrypted = encrypt_secret(new_totp_secret); account.totp_confirmed = True
            update_fields.extend(["totp_secret_encrypted", "totp_confirmed"])
        elif disable_totp and account.role in (User.Role.REVIEWER, User.Role.ADMIN):
            account.totp_secret_encrypted = ""; account.totp_confirmed = False
            update_fields.extend(["totp_secret_encrypted", "totp_confirmed"])
        account.save(update_fields=update_fields)
        if account.role == User.Role.REVIEWER:
            CareRelationship.objects.filter(therapist=account).exclude(patient_id__in=selected_patient_ids).delete()
            existing = set(CareRelationship.objects.filter(therapist=account, patient_id__in=selected_patient_ids).values_list("patient_id", flat=True))
            CareRelationship.objects.bulk_create([CareRelationship(therapist=account, patient_id=patient_id) for patient_id in selected_patient_ids - existing])
        recovery_codes = []
        if rotate_totp:
            account.recovery_codes.all().delete(); recovery_codes = issue_recovery_codes(account)
        elif disable_totp:
            account.recovery_codes.all().delete()
    audit(request.user, "account.updated", account.id, {"role": account.role, "is_active": account.is_active, "patient_ids": sorted(selected_patient_ids) if account.role == User.Role.REVIEWER else None, "password_replaced": bool(new_password), "authenticator_rotated": rotate_totp, "authenticator_disabled": disable_totp}, request)
    if new_password and account.id == request.user.id: update_session_auth_hash(request, account)
    if recovery_codes: messages.success(request, f"Account updated. New one-time recovery codes for {account.username}: " + "  ".join(recovery_codes))
    else: messages.success(request, "Account settings updated.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_account_delete(request, user_id):
    account = get_object_or_404(User, id=user_id)
    if request.POST.get("confirm_username") != account.username:
        messages.error(request, "The typed account name did not match."); return redirect("control:dashboard")
    if account.id == request.user.id:
        messages.error(request, "You cannot delete the account controlling this session."); return redirect("control:dashboard")
    if account.cards.exists():
        messages.error(request, "This account owns diary cards and cannot be deleted."); return redirect("control:dashboard")
    if account.role == User.Role.ADMIN and User.objects.filter(role=User.Role.ADMIN).count() <= 1:
        messages.error(request, "The installation must retain at least one administrator."); return redirect("control:dashboard")
    account_id, username = account.id, account.username
    account.delete()
    audit(request.user, "account.deleted", account_id, {"username": username}, request)
    messages.success(request, f"Account {username} deleted.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_emotion_add(request):
    label = request.POST.get("label", "").strip()[:80]
    face = request.POST.get("face", "").strip()[:16]
    color = request.POST.get("color", "#ffffff")
    if not label or not face or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        messages.error(request, "A name, icon, and valid color are required."); return redirect("control:dashboard")
    slug_base = slugify(label) or "emotion"; slug = slug_base; suffix = 2
    while Emotion.objects.filter(slug=slug).exists(): slug = f"{slug_base}-{suffix}"; suffix += 1
    sort_order = (Emotion.objects.aggregate(highest=Max("sort_order"))["highest"] or 0) + 1
    emotion = Emotion.objects.create(slug=slug, label=label, face=face, color=color, sort_order=sort_order, active=True)
    audit(request.user, "emotion.created", emotion.id, request=request); messages.success(request, "Emotion added to the signal field.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_emotion(request, emotion_id):
    emotion = get_object_or_404(Emotion, id=emotion_id)
    emotion.label = request.POST.get("label", emotion.label).strip()[:80] or emotion.label
    emotion.face = request.POST.get("face", emotion.face).strip()[:16] or emotion.face
    color = request.POST.get("color", emotion.color)
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", color): emotion.color = color
    emotion.active = request.POST.get("active") == "on"; emotion.save()
    audit(request.user, "emotion.updated", emotion.id, request=request); messages.success(request, "Emotion catalog updated.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_emotion_remove(request, emotion_id):
    emotion = get_object_or_404(Emotion, id=emotion_id)
    emotion.active = False; emotion.save(update_fields=["active"])
    audit(request.user, "emotion.removed", emotion.id, request=request)
    messages.success(request, "Emotion removed from the patient form. Its historical appearance is preserved.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_form_field(request):
    label = request.POST.get("label", "").strip()[:120]
    field_type = request.POST.get("type")
    allowed = {"short_text", "long_text", "number", "rating", "checkbox", "select", "date", "time"}
    if not label or field_type not in allowed:
        messages.error(request, "A label and supported field type are required."); return redirect("control:dashboard")
    fields, _, base_version = _form_builder_state(request)
    key_base = "field_" + (slugify(label).replace("-", "_") or "response"); key = key_base; n = 2
    while any(f.get("key") == key for f in fields): key = f"{key_base}_{n}"; n += 1
    options = [x.strip() for x in request.POST.get("options", "").split(",") if x.strip()]
    fields.append({"key": key, "label": label, "type": field_type, "required": request.POST.get("required") == "on", "options": options, "active": True})
    _save_form_draft(request, fields, base_version)
    messages.success(request, "Prompt added to the draft. Publish when the staged form is ready.")
    return redirect("control:dashboard")

FORM_DRAFT_SESSION_KEY = "form_builder_draft"

def _form_builder_state(request):
    current = FormDefinition.objects.filter(active=True).first()
    base_version = current.version if current else 0
    active_fields = [dict(field) for field in ((current.schema or {}).get("fields", []) if current else []) if isinstance(field, dict)]
    draft = request.session.get(FORM_DRAFT_SESSION_KEY)
    if isinstance(draft, dict) and draft.get("base_version") == base_version and isinstance(draft.get("fields"), list):
        fields = [dict(field) for field in draft["fields"] if isinstance(field, dict)]
    else:
        if draft is not None: request.session.pop(FORM_DRAFT_SESSION_KEY, None)
        fields = [dict(field) for field in active_fields]
    return fields, active_fields, base_version

def _save_form_draft(request, fields, base_version):
    request.session[FORM_DRAFT_SESSION_KEY] = {"base_version": base_version, "fields": fields}
    request.session.modified = True

@transaction.atomic
def _publish_form_fields(fields, actor, expected_version):
    current = FormDefinition.objects.select_for_update().filter(active=True).first()
    current_version = current.version if current else 0
    if current_version != expected_version: raise ValueError("The active form changed after this draft was started.")
    latest_version = FormDefinition.objects.order_by("-version").values_list("version", flat=True).first() or 0
    if current:
        current.active = False; current.save(update_fields=["active"])
    version = latest_version + 1
    FormDefinition.objects.create(version=version, active=True, schema={"fields": fields}, created_by=actor)
    return version

@role_required(User.Role.ADMIN)
@require_POST
def control_form_field_remove(request, field_key):
    fields, _, base_version = _form_builder_state(request)
    updated = [field for field in fields if field.get("key") != field_key]
    if len(updated) == len(fields):
        messages.error(request, "That prompt is not part of the staged form."); return redirect("control:dashboard")
    _save_form_draft(request, updated, base_version)
    messages.success(request, "Prompt removed from the draft. The active form is unchanged until publish.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_form_field_move(request, field_key):
    direction = request.POST.get("direction")
    fields, _, base_version = _form_builder_state(request)
    index = next((position for position, field in enumerate(fields) if field.get("key") == field_key), None)
    target = index - 1 if index is not None and direction == "up" else index + 1 if index is not None and direction == "down" else None
    if index is None or target is None or target < 0 or target >= len(fields):
        messages.error(request, "That prompt cannot move in that direction."); return redirect("control:dashboard")
    fields[index], fields[target] = fields[target], fields[index]
    _save_form_draft(request, fields, base_version)
    messages.success(request, "Prompt reordered in the draft. The active form is unchanged until publish.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_form_publish(request):
    fields, active_fields, base_version = _form_builder_state(request)
    if fields == active_fields:
        messages.error(request, "There are no staged changes to publish."); return redirect("control:dashboard")
    try: version = _publish_form_fields(fields, request.user, base_version)
    except ValueError as exc:
        request.session.pop(FORM_DRAFT_SESSION_KEY, None)
        messages.error(request, f"{exc} The draft was discarded; stage the changes again."); return redirect("control:dashboard")
    request.session.pop(FORM_DRAFT_SESSION_KEY, None)
    audit(request.user, "form.published", version, {"prompt_count": len(fields)}, request)
    messages.success(request, f"Form version {version} published. New cards will use it.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_form_discard(request):
    request.session.pop(FORM_DRAFT_SESSION_KEY, None)
    messages.success(request, "Staged form changes discarded.")
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_quarantine(request, card_id):
    card = get_object_or_404(Card, id=card_id)
    if request.POST.get("confirm_id") != str(card.id): messages.error(request, "The typed card ID did not match.")
    else:
        try: quarantine_card(card, request.user, request); messages.success(request, "Card moved to Recently Deleted. It can be restored for seven days.")
        except Exception as exc: messages.error(request, str(exc))
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_restore(request, card_id):
    card = get_object_or_404(Card, id=card_id, status=Card.Status.QUARANTINED)
    try: restore_card(card, request.user, request); messages.success(request, "Card restored.")
    except Exception as exc: messages.error(request, str(exc))
    return redirect("control:dashboard")

@role_required(User.Role.ADMIN)
@require_POST
def control_purge(request, card_id):
    card = get_object_or_404(Card, id=card_id, status=Card.Status.QUARANTINED)
    if request.POST.get("confirm_id") != str(card.id):
        messages.error(request, "The typed card ID did not match. Nothing was permanently deleted.")
    else:
        try:
            purge_card(card, request.user, request, force=True)
            messages.success(request, "Card permanently deleted. The patient can now create a new entry for that date.")
        except Exception as exc: messages.error(request, str(exc))
    return redirect("control:dashboard")


@role_required(User.Role.ADMIN)
@require_POST
@transaction.atomic
def control_attachment_limits(request):
    limits = AttachmentSettings.objects.select_for_update().get(pk=AttachmentSettings.current().pk)
    old = {"file_limit_mib": limits.file_limit_mib, "card_limit_mib": limits.card_limit_mib}
    form = AttachmentLimitsForm(request.POST, instance=limits)
    if not form.is_valid():
        return control_dashboard(request, limits_form=form, response_status=400)
    form.save()
    audit(request.user, "attachment_limits.updated", "global", {"old": old, "new": form.cleaned_data}, request)
    messages.success(request, "Attachment limits updated. Existing attachments are unchanged.")
    return redirect(reverse("control:dashboard") + "#maintenance")
