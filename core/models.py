import secrets
import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

class User(AbstractUser):
    class Role(models.TextChoices):
        PATIENT = "patient", "Patient"
        REVIEWER = "reviewer", "Therapist"
        ADMIN = "admin", "Administrator"
    role = models.CharField(max_length=16, choices=Role.choices)
    totp_secret_encrypted = models.TextField(blank=True)
    totp_confirmed = models.BooleanField(default=False)

class CareRelationship(models.Model):
    therapist = models.ForeignKey(User, on_delete=models.CASCADE, related_name="patient_assignments")
    patient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="therapist_assignments")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["therapist", "patient"], name="one_therapist_patient_relationship")]

class Emotion(models.Model):
    slug = models.SlugField(unique=True)
    label = models.CharField(max_length=80)
    face = models.CharField(max_length=16, default="◆")
    color = models.CharField(max_length=16, default="#79ffe1")
    sort_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    def __str__(self): return self.label

class FormDefinition(models.Model):
    version = models.PositiveIntegerField(unique=True)
    schema = models.JSONField(default=dict)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

class Card(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        QUARANTINED = "quarantined", "Quarantined"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="cards")
    local_date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    draft_data = models.JSONField(default=dict, blank=True)
    content_index = models.JSONField(default=dict, blank=True)
    form_version = models.PositiveIntegerField(default=1)
    folder_name = models.CharField(max_length=160, blank=True)
    checksum = models.CharField(max_length=64, blank=True)
    comments_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    quarantined_at = models.DateTimeField(null=True, blank=True)
    purge_after = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "local_date"], name="one_card_per_patient_day")]
        ordering = ["-local_date"]

class AttachmentSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    file_limit_mib = models.PositiveIntegerField(default=100)
    card_limit_mib = models.PositiveIntegerField(default=500)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="attachment_settings_singleton"),
            models.CheckConstraint(condition=models.Q(file_limit_mib__gt=0, card_limit_mib__gte=models.F("file_limit_mib")), name="attachment_limits_valid"),
        ]

    @classmethod
    def current(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def file_limit_bytes(self): return self.file_limit_mib * 1024 * 1024

    @property
    def card_limit_bytes(self): return self.card_limit_mib * 1024 * 1024


class Attachment(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="attachments")
    stored_name = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.PositiveBigIntegerField()
    checksum = models.CharField(max_length=64)

class Addendum(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="addenda")
    body = models.TextField()
    filename = models.CharField(max_length=255)
    checksum = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

class TherapistComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="therapist_comments")
    reviewer = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="therapist_comments")
    author_name = models.CharField(max_length=150)
    body = models.TextField()
    patient_read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ["created_at", "id"]

class ReviewerMetadata(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="reviewer_metadata")
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE)
    is_read = models.BooleanField(default=False)
    starred = models.BooleanField(default=False)
    tags = models.JSONField(default=list, blank=True)
    collections = models.JSONField(default=list, blank=True)
    private_note = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["card", "reviewer"], name="one_reviewer_metadata")]

class Invite(models.Model):
    token_hash = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=16, choices=User.Role.choices)
    username = models.CharField(max_length=150)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

class ApiToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

class RecoveryCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64, unique=True)
    used_at = models.DateTimeField(null=True, blank=True)

class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    target_id = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    class Meta: ordering = ["-created_at"]

class FeedbackReport(models.Model):
    class Kind(models.TextChoices):
        FEEDBACK = "feedback", "Feedback"
        BUG = "bug", "Bug report"
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="feedback_reports")
    author_name = models.CharField(max_length=150)
    author_role = models.CharField(max_length=16, choices=User.Role.choices)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.FEEDBACK)
    subject = models.CharField(max_length=140, blank=True)
    body = models.TextField(max_length=4000)
    page = models.CharField(max_length=500, blank=True)
    important = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="archived_feedback_reports")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta: ordering = ["-important", "-created_at"]
