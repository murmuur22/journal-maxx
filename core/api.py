import hashlib
from django.http import JsonResponse
from django.utils import timezone
from .models import ApiToken, Card, CareRelationship, User
from .services import storage_status, verify_card_integrity

def token_user(request, scope):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "): return None
    raw = header[7:]
    digest = hashlib.sha256(raw.encode()).hexdigest()
    token = ApiToken.objects.select_related("user").filter(token_hash=digest, revoked_at__isnull=True).first()
    if not token or scope not in token.scopes or (token.expires_at and token.expires_at <= timezone.now()): return None
    token.last_used_at = timezone.now(); token.save(update_fields=["last_used_at"])
    return token.user

def unauthorized(): return JsonResponse({"error": "unauthorized"}, status=401)

def cards(request):
    user = token_user(request, "cards:metadata")
    if not user: return unauthorized()
    query = Card.objects.all()
    if user.role == User.Role.PATIENT: query = query.filter(patient=user)
    elif user.role == User.Role.REVIEWER: query = query.filter(patient__therapist_assignments__therapist=user)
    elif user.role != User.Role.ADMIN: return unauthorized()
    data = [{"id": str(c.id), "date": c.local_date.isoformat(), "status": c.status, "addenda_count": c.addenda.count(), "attachment_count": c.attachments.count(), "integrity": verify_card_integrity(c) if c.status != Card.Status.DRAFT else "draft"} for c in query]
    return JsonResponse({"results": data})

def card(request, card_id):
    user = token_user(request, "cards:metadata")
    if not user: return unauthorized()
    try: item = Card.objects.get(id=card_id)
    except Card.DoesNotExist: return JsonResponse({"error": "not_found"}, status=404)
    if user.role == User.Role.PATIENT and item.patient_id != user.id: return JsonResponse({"error": "not_found"}, status=404)
    if user.role == User.Role.REVIEWER and not CareRelationship.objects.filter(therapist=user, patient_id=item.patient_id).exists(): return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({"id": str(item.id), "date": item.local_date.isoformat(), "status": item.status, "submitted_at": item.submitted_at, "addenda_count": item.addenda.count(), "attachment_count": item.attachments.count(), "integrity": verify_card_integrity(item) if item.status != Card.Status.DRAFT else "draft"})

def readiness(request):
    user = token_user(request, "system:read")
    if not user: return unauthorized()
    status = storage_status(); status.pop("root", None)
    return JsonResponse({"database": "ok", "card_storage": status})

def schema(request):
    return JsonResponse({"openapi": "3.0.3", "info": {"title": "Diary metadata API", "version": "1.0.0"}, "paths": {"/api/v1/cards": {"get": {"summary": "List card metadata"}}, "/api/v1/cards/{card_id}": {"get": {"summary": "Get card metadata"}}, "/api/v1/ready": {"get": {"summary": "Storage readiness"}}}})
