import hashlib
import json
import secrets
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .models import ApiToken, AuditEvent, Card, CareRelationship, Emotion, FormDefinition, ReviewerMetadata, TherapistComment, User
from .security import decrypt_secret, encrypt_secret, generate_totp_secret, totp, verify_totp
from .services import add_addendum, card_directory, card_file_inventory, comments_filename, quarantine_card, restore_card, storage_status, submit_card, verify_card_integrity

class DiaryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(CARD_ROOT=Path(self.temp.name))
        self.override.enable()
        self.patient = User.objects.create_user(username="patient", password="a-long-test-password", role=User.Role.PATIENT)
        self.reviewer = User.objects.create_user(username="reviewer", password="a-long-test-password", role=User.Role.REVIEWER)
        self.admin = User.objects.create_user(username="operator", password="a-long-test-password", role=User.Role.ADMIN)
        CareRelationship.objects.create(therapist=self.reviewer, patient=self.patient)
        self.emotion = Emotion.objects.create(slug="joy", label="Joy")
    def tearDown(self): self.override.disable(); self.temp.cleanup()

    def submitted(self):
        card = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 15))
        upload = SimpleUploadedFile("image one.txt", b"safe context", content_type="text/plain")
        data = {"emotions": [{"id": "joy", "label": "Joy", "intensity": 4, "note": "A bright moment"}], "activities": "Walked", "journal": "Felt present."}
        return submit_card(card, data, [upload], self.patient)

    def test_login_is_minimal_authentication_form(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IDENTITY")
        self.assertContains(response, "PASSPHRASE")
        self.assertContains(response, "CODE")
        self.assertContains(response, "JOURNALMAX")
        self.assertContains(response, "login-brand")
        self.assertNotContains(response, "ENTER THE THOUGHTSPACE")

    def test_readiness_reports_release_database_and_card_volume(self):
        response = self.client.get(reverse("readiness"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], "0.2.1")
        self.assertEqual(response.json()["database"], "ok")
        self.assertEqual(response.json()["card_storage"], "local")
        with override_settings(CARD_VOLUME_REQUIRE_MARKER=True):
            unavailable = self.client.get(reverse("readiness"))
            self.assertEqual(unavailable.status_code, 503)
            self.assertEqual(unavailable.json()["status"], "unavailable")

    def test_production_middleware_serves_collected_static_files(self):
        with tempfile.TemporaryDirectory() as static_directory, override_settings(DEBUG=False, STATIC_ROOT=Path(static_directory)):
            call_command("collectstatic", interactive=False, verbosity=0)
            production_client = Client()
            response = production_client.get("/static/app.css")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "max-age=60, public")

    def test_stale_login_form_recovers_instead_of_showing_forbidden(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("login"), {"username": self.patient.username, "password": "a-long-test-password"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LOGIN SESSION EXPIRED")
        self.assertEqual(response.redirect_chain, [(reverse("login") + "?expired=1", 302)])

    @override_settings(DEBUG=True, DEV_PASSWORD_ONLY_USERS={"reviewer"})
    def test_named_preview_reviewer_can_sign_in_without_code(self):
        response = self.client.post("/login/", {"username": "reviewer", "password": "a-long-test-password"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    @override_settings(DEBUG=False, DEV_PASSWORD_ONLY_USERS=set())
    def test_real_reviewer_still_requires_second_factor(self):
        response = self.client.post("/login/", {"username": "reviewer", "password": "a-long-test-password"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid authenticator code")

    def test_submission_creates_readable_markdown_and_locks_card(self):
        card = self.submitted(); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.SUBMITTED)
        path = card_directory(card) / "260815_diary.md"
        self.assertTrue(path.exists()); self.assertIn("## Significant emotions", path.read_text())
        self.assertNotIn("## What I did today", path.read_text())
        self.assertNotIn("## Reflection", path.read_text())
        self.assertTrue((card_directory(card) / "260815_image_one.txt").exists())
        with self.assertRaises(Exception): submit_card(card, card.content_index, [], self.patient)

    def test_required_volume_marker_fails_closed_without_creating_card_files(self):
        with override_settings(CARD_VOLUME_REQUIRE_MARKER=True, CARD_VOLUME_ID=""):
            status = storage_status()
            self.assertFalse(status["available"])
            self.assertEqual(status["state"], "unrecognized")
            card = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 13))
            with self.assertRaisesMessage(ValidationError, "Card storage offline"):
                submit_card(card, {"emotions": [{"id": "joy", "label": "Joy", "intensity": 3}]}, [], self.patient)
            self.assertFalse(card_directory(card).exists())

    def test_initialized_volume_is_recognized_and_can_be_pinned_by_id(self):
        output = StringIO()
        with override_settings(CARD_VOLUME_REQUIRE_MARKER=True, CARD_VOLUME_ID=""):
            call_command("init_card_volume", confirm_path=str(self.temp.name), stdout=output)
            status = storage_status()
            self.assertTrue(status["available"])
            self.assertTrue(status["protected"])
            self.assertEqual(status["schema_version"], 1)
            self.assertIn("Volume ID:", output.getvalue())
            with override_settings(CARD_VOLUME_ID="a-different-volume"):
                mismatch = storage_status()
                self.assertFalse(mismatch["available"])
                self.assertEqual(mismatch["state"], "mismatch")

    def test_addendum_does_not_modify_original(self):
        card = self.submitted(); original = (card_directory(card) / "260815_diary.md").read_bytes()
        item = add_addendum(card, "Later context", self.patient)
        self.assertEqual(original, (card_directory(card) / "260815_diary.md").read_bytes())
        self.assertTrue((card_directory(card) / item.filename).exists())

    def test_submitted_card_detail_renders_for_patient_and_reviewer(self):
        card = self.submitted(); client = Client()
        client.force_login(self.patient)
        response = client.get(reverse("journal:detail", args=[card.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "detail-emotion")
        self.assertContains(response, "INTENSITY 4 / 5")
        client.force_login(self.reviewer)
        self.assertEqual(client.get(reverse("review:detail", args=[card.id])).status_code, 200)

    def test_unanswered_diary_sections_start_collapsed(self):
        card = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 14))
        submit_card(card, {"emotions": [{"id": "joy", "label": "Joy", "intensity": 3, "note": ""}], "custom": [{"key": "optional", "label": "Optional prompt", "type": "long_text", "value": ""}]}, [], self.patient)
        self.client.force_login(self.patient)
        response = self.client.get(reverse("journal:detail", args=[card.id]))
        content = response.content.decode()
        self.assertEqual(content.count('<details class="diary-section" open>'), 1)
        self.assertEqual(content.count('<details class="diary-section">'), 1)

    def test_card_can_show_escaped_raw_markdown_files(self):
        card = self.submitted(); add_addendum(card, "Raw follow-up", self.patient)
        self.client.force_login(self.patient)
        response = self.client.get(reverse("journal:detail", args=[card.id]) + "?view=raw")
        self.assertContains(response, "RAW MARKDOWN")
        self.assertContains(response, "schema_version: 1")
        self.assertContains(response, "# Addendum")
        self.assertNotContains(response, "detail-emotion")

    def test_admin_cannot_open_content_but_can_quarantine_and_restore(self):
        card = self.submitted(); client = Client(); client.force_login(self.admin)
        self.assertEqual(client.get(reverse("journal:detail", args=[card.id])).status_code, 403)
        quarantine_card(card, self.admin); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.QUARANTINED)
        self.assertTrue((Path(self.temp.name) / ".quarantine" / card.folder_name).exists())
        restore_card(card, self.admin); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.SUBMITTED)

    def test_patient_login_handles_a_deleted_entry_for_today_without_404(self):
        card = Card.objects.create(patient=self.patient, local_date=timezone.localdate())
        submit_card(card, {"emotions": [{"id": "joy", "label": "Joy", "intensity": 3, "note": ""}], "activities": "", "journal": "Today"}, [], self.patient)
        quarantine_card(card, self.admin)
        response = self.client.post(reverse("login"), {"username": self.patient.username, "password": "a-long-test-password"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RECENTLY<br>DELETED")
        self.assertContains(response, "Today’s entry is in recovery storage.")
        self.assertContains(response, "OPEN ENTRY HISTORY")

    def test_admin_can_permanently_delete_recently_deleted_card_and_patient_can_remake_it(self):
        card = Card.objects.create(patient=self.patient, local_date=timezone.localdate())
        old_id = card.id
        submit_card(card, {"emotions": [{"id": "joy", "label": "Joy", "intensity": 3, "note": ""}], "activities": "", "journal": "Replace me"}, [], self.patient)
        quarantine_card(card, self.admin)
        self.client.force_login(self.admin)
        dashboard = self.client.get(reverse("control:dashboard"))
        self.assertContains(dashboard, f'data-modal-open="purge-{old_id}"')
        self.assertContains(dashboard, "PERMANENTLY DELETE")
        response = self.client.post(reverse("control:purge", args=[old_id]), {"confirm_id": str(old_id)})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertFalse(Card.objects.filter(id=old_id).exists())
        event = AuditEvent.objects.get(action="card.purged", target_id=str(old_id))
        self.assertEqual(event.actor, self.admin)
        self.client.force_login(self.patient)
        response = self.client.get(reverse("journal:today"))
        self.assertEqual(response.status_code, 200)
        replacement = Card.objects.get(patient=self.patient, local_date=timezone.localdate())
        self.assertNotEqual(replacement.id, old_id)
        self.assertEqual(replacement.status, Card.Status.DRAFT)

    def test_admin_stages_prompt_changes_then_publishes_one_version(self):
        original_fields = [
            {"key": "field_first", "label": "First prompt", "type": "short_text", "required": False, "active": True},
            {"key": "field_second", "label": "Second prompt", "type": "long_text", "required": True, "active": True},
        ]
        original = FormDefinition.objects.create(version=1, active=True, schema={"fields": original_fields}, created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("control:form_field_move", args=["field_second"]), {"direction": "up"})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertEqual(FormDefinition.objects.count(), 1)
        response = self.client.get(reverse("control:dashboard"))
        self.assertTrue(response.context["has_staged_changes"])
        self.assertEqual([field["key"] for field in response.context["custom_fields"]], ["field_second", "field_first"])
        response = self.client.post(reverse("control:form_field_remove", args=["field_first"]))
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertEqual(FormDefinition.objects.count(), 1)
        response = self.client.post(reverse("control:form_publish"))
        self.assertRedirects(response, reverse("control:dashboard"))
        version_two = FormDefinition.objects.get(version=2)
        self.assertEqual([field["key"] for field in version_two.schema["fields"]], ["field_second"])
        original.refresh_from_db()
        self.assertFalse(original.active)
        self.assertEqual(original.schema["fields"], original_fields)
        response = self.client.get(reverse("control:dashboard"))
        self.assertFalse(response.context["has_staged_changes"])

    def test_admin_can_discard_staged_prompt_changes(self):
        original_fields = [
            {"key": "field_first", "label": "First prompt", "type": "short_text", "required": False, "active": True},
            {"key": "field_second", "label": "Second prompt", "type": "long_text", "required": True, "active": True},
        ]
        FormDefinition.objects.create(version=1, active=True, schema={"fields": original_fields}, created_by=self.admin)
        self.client.force_login(self.admin)
        self.client.post(reverse("control:form_field_remove", args=["field_first"]))
        response = self.client.post(reverse("control:form_discard"))
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertEqual(FormDefinition.objects.count(), 1)
        response = self.client.get(reverse("control:dashboard"))
        self.assertFalse(response.context["has_staged_changes"])
        self.assertEqual(response.context["custom_fields"], original_fields)

    def test_admin_form_builder_loads_in_place_controls(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "data-form-builder")
        self.assertContains(response, "static/control.js")
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'class="control-tabs"')
        for panel_id in ("overview", "accounts", "emotions", "form", "maintenance", "audit"):
            self.assertContains(response, f'id="{panel_id}"')

    def test_admin_sees_staged_form_preview_and_recoverable_delete_modal(self):
        FormDefinition.objects.create(version=1, active=True, schema={"fields": [
            {"key": "field_context", "label": "What needs context?", "type": "long_text", "required": True, "active": True},
        ]}, created_by=self.admin)
        card = self.submitted()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "DRAFT PREVIEW", count=0)
        self.assertContains(response, "ACTIVE VERSION PREVIEW")
        self.assertContains(response, "FORM PREVIEW")
        self.assertContains(response, "Full patient form")
        self.assertContains(response, "Choose significant emotions")
        self.assertContains(response, "How strongly did each show up?")
        self.assertContains(response, "Reflection")
        self.assertNotContains(response, "WHAT DID YOU DO TODAY?")
        self.assertNotContains(response, "Write what you want remembered")
        self.assertContains(response, "Attach context")
        self.assertContains(response, "What needs context?")
        self.assertContains(response, f'data-modal-open="delete-{card.id}"')
        self.assertContains(response, "DELETE CARD")
        self.assertContains(response, "Recently Deleted")

    def test_maintenance_lists_filenames_sizes_and_addendum_integrity_without_content(self):
        card = self.submitted()
        addendum = add_addendum(card, "Sensitive follow-up content", self.patient)
        inventory = card_file_inventory(card)
        self.assertTrue(inventory["available"])
        self.assertEqual(inventory["file_count"], 3)
        self.assertGreater(inventory["total_size"], 0)
        self.assertEqual(verify_card_integrity(card), "verified")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, f'data-modal-open="contents-{card.id}"')
        self.assertContains(response, addendum.filename)
        self.assertContains(response, "image_one.txt")
        self.assertContains(response, "SHA-256")
        self.assertContains(response, "data-sortable-table")
        self.assertContains(response, "data-sort-type=\"number\"")
        self.assertContains(response, "ACTIONS")
        self.assertContains(response, "Storage identity")
        self.assertContains(response, "DIARY_CARD_ROOT")
        self.assertContains(response, "LOCAL DEV")
        self.assertNotContains(response, "Maintenance index")
        self.assertNotContains(response, "Sensitive follow-up content")

    def test_admin_audit_workspace_uses_compact_terminal_buffer(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "audit-console")
        self.assertContains(response, "JOURNALMAX // AUDITD")
        self.assertContains(response, "100 EVENTS")
        self.assertContains(response, "audit-resizer", count=5)
        self.assertContains(response, "DRAG HEADER EDGES TO RESIZE")
        self.assertNotContains(response, "Recent audit events")

    def test_overview_links_to_contextual_accounts_entries_and_form_workspace(self):
        card = self.submitted()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, 'class="panel overview-metric" href="#accounts"')
        self.assertContains(response, 'class="panel overview-metric" href="#maintenance"')
        self.assertContains(response, "Recent activity")
        self.assertContains(response, "PATIENT EXPERIENCE")
        self.assertContains(response, "Build, preview, and publish the questions patients receive.")
        self.assertContains(response, f'?account={self.patient.id}#accounts')
        self.assertContains(response, f'?entry={card.id}#maintenance')
        self.assertContains(response, f'ENTRY {card.local_date:%Y-%m-%d}')
        self.assertContains(response, "MAINTENANCE")
        self.assertContains(response, reverse("control:activity"))
        activity = self.client.get(reverse("control:activity"))
        self.assertEqual(activity.status_code, 200)
        payload = activity.json()
        self.assertEqual(payload["items"][0]["action"], "card.submitted")
        self.assertEqual(payload["items"][0]["actor_label"], self.patient.username)
        self.assertEqual(payload["items"][0]["target_url"], f"?entry={card.id}#maintenance")
        self.assertEqual(payload["counts"]["submitted"], 1)

    def test_live_activity_feed_is_restricted_to_administrators(self):
        self.client.force_login(self.patient)
        response = self.client.get(reverse("control:activity"))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_add_edit_and_safely_remove_emotions_from_signal_field(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("control:emotion_add"), {"label": "Tenderness", "face": "♡", "color": "#ff77aa"})
        self.assertRedirects(response, reverse("control:dashboard"))
        emotion = Emotion.objects.get(label="Tenderness")
        self.assertTrue(emotion.active)
        response = self.client.post(reverse("control:emotion", args=[emotion.id]), {"label": "Softness", "face": "◇", "color": "#ffaaee", "active": "on"})
        self.assertRedirects(response, reverse("control:dashboard"))
        emotion.refresh_from_db(); self.assertEqual(emotion.label, "Softness")
        response = self.client.post(reverse("control:emotion_remove", args=[emotion.id]))
        self.assertRedirects(response, reverse("control:dashboard"))
        emotion.refresh_from_db(); self.assertFalse(emotion.active)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "emotion-space")
        self.assertContains(response, f'data-emotion-node="{emotion.id}"')
        self.assertContains(response, "emotion-catalog-panel")
        self.assertContains(response, "AMBIENT SIGNAL FIELD")
        self.assertContains(response, "REMOVED")
        self.assertContains(response, "ADD EMOTION")

    def test_admin_account_registry_edits_and_deletes_safe_accounts(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "account-registry")
        self.assertContains(response, "ADD ACCOUNT")
        self.assertContains(response, f'id="account-edit-{self.reviewer.id}"')
        response = self.client.post(reverse("control:account", args=[self.reviewer.id]), {"username": "therapist-edited", "is_active": "on", "patient_ids": [self.patient.id]})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.reviewer.refresh_from_db(); self.assertEqual(self.reviewer.username, "therapist-edited")
        response = self.client.post(reverse("control:account_delete", args=[self.reviewer.id]), {"confirm_username": "therapist-edited"})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertFalse(User.objects.filter(id=self.reviewer.id).exists())
        response = self.client.post(reverse("control:account_delete", args=[self.admin.id]), {"confirm_username": self.admin.username})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertTrue(User.objects.filter(id=self.admin.id).exists())

    def test_account_roles_are_immutable_and_therapists_manage_patient_assignments(self):
        second = User.objects.create_user(username="second-patient", password="a-long-test-password", role=User.Role.PATIENT)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("control:account", args=[self.reviewer.id]), {"username": self.reviewer.username, "role": User.Role.PATIENT, "is_active": "on"})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.reviewer.refresh_from_db(); self.assertEqual(self.reviewer.role, User.Role.REVIEWER)
        response = self.client.post(reverse("control:account", args=[self.reviewer.id]), {"username": self.reviewer.username, "is_active": "on", "patient_ids": [second.id]})
        self.assertRedirects(response, reverse("control:dashboard"))
        self.assertEqual(list(CareRelationship.objects.filter(therapist=self.reviewer).values_list("patient_id", flat=True)), [second.id])
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "PERMANENT ROLE")
        self.assertContains(response, "PATIENT ACCESS")
        self.assertContains(response, "ASSIGNED THERAPISTS")

    def test_admin_can_replace_password_and_verified_authenticator_without_exposing_old_secrets(self):
        secret = generate_totp_secret()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("control:account", args=[self.reviewer.id]), {
            "username": self.reviewer.username, "is_active": "on", "patient_ids": [self.patient.id],
            "new_password": "a-different-long-password", "confirm_password": "a-different-long-password",
            "new_totp_secret": secret, "new_totp_code": totp(secret),
        })
        self.assertRedirects(response, reverse("control:dashboard"))
        self.reviewer.refresh_from_db()
        self.assertTrue(self.reviewer.check_password("a-different-long-password"))
        self.assertEqual(decrypt_secret(self.reviewer.totp_secret_encrypted), secret)
        self.assertTrue(self.reviewer.totp_confirmed)
        self.assertEqual(self.reviewer.recovery_codes.count(), 8)
        response = self.client.get(reverse("control:dashboard"))
        self.assertContains(response, "GENERATE NEW KEY")
        self.assertNotContains(response, secret)
        self.assertContains(response, "account-sort")

    def test_reviewer_metadata_is_private(self):
        card = self.submitted(); other = User.objects.create_user(username="other", password="a-long-test-password", role=User.Role.REVIEWER)
        ReviewerMetadata.objects.create(card=card, reviewer=self.reviewer, private_note="private", tags=["session"])
        self.assertFalse(ReviewerMetadata.objects.filter(card=card, reviewer=other).exists())

    def test_reviewer_comments_are_stored_in_one_card_file_and_visible_to_patient(self):
        card = self.submitted(); client = Client(); client.force_login(self.reviewer)
        response = client.get(reverse("review:detail", args=[card.id]))
        self.assertContains(response, "Leave a comment")
        self.assertContains(response, "SHARED WITH PATIENT")
        response = client.post(reverse("review:comment", args=[card.id]), {"body": "Ask about the quiet morning."})
        self.assertRedirects(response, reverse("review:detail", args=[card.id]))
        response = client.post(reverse("review:comment", args=[card.id]), {"body": "A second thought."})
        self.assertRedirects(response, reverse("review:detail", args=[card.id]))
        self.assertEqual(TherapistComment.objects.filter(card=card).count(), 2)
        card.refresh_from_db()
        comment_path = card_directory(card) / comments_filename(card)
        self.assertTrue(comment_path.exists())
        comment_file = comment_path.read_text()
        self.assertIn("Ask about the quiet morning.", comment_file)
        self.assertIn("A second thought.", comment_file)
        self.assertIn(self.reviewer.username, comment_file)
        self.assertEqual(len(list(card_directory(card).glob("*therapist_comments.md"))), 1)
        self.assertEqual(verify_card_integrity(card), "verified")
        client.force_login(self.patient)
        patient_view = client.get(reverse("journal:detail", args=[card.id]))
        self.assertContains(patient_view, "Therapist comments")
        self.assertContains(patient_view, "PATIENT VISIBLE")
        self.assertContains(patient_view, "Ask about the quiet morning.")
        self.assertContains(patient_view, "A second thought.")
        self.assertNotContains(patient_view, "POST COMMENT")
        raw_view = client.get(reverse("journal:detail", args=[card.id]) + "?view=raw")
        self.assertContains(raw_view, comments_filename(card))
        self.assertContains(raw_view, "APPEND-ONLY COMMENT LOG")

    def test_patient_cannot_post_therapist_comment(self):
        card = self.submitted(); self.client.force_login(self.patient)
        response = self.client.post(reverse("review:comment", args=[card.id]), {"body": "Not a therapist comment"})
        self.assertEqual(response.status_code, 403)

    def test_metadata_api_never_returns_content(self):
        card = self.submitted(); raw = "dcard_" + secrets.token_urlsafe(20)
        ApiToken.objects.create(user=self.patient, name="test", prefix=raw[:12], token_hash=hashlib.sha256(raw.encode()).hexdigest(), scopes=["cards:metadata"])
        response = self.client.get("/api/v1/cards", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 200)
        payload = response.content.decode()
        self.assertIn(str(card.id), payload); self.assertNotIn("Felt present", payload); self.assertNotIn("Joy", payload); self.assertNotIn("image_one", payload)

    def test_totp_encrypts_and_verifies_with_small_clock_drift(self):
        secret = generate_totp_secret(); encrypted = encrypt_secret(secret)
        self.assertNotIn(secret, encrypted); self.assertEqual(decrypt_secret(encrypted), secret)
        self.assertTrue(verify_totp(secret, totp(secret))); self.assertFalse(verify_totp(secret, "000000"))

    def test_role_routes_are_isolated(self):
        card = self.submitted(); client = Client()
        client.force_login(self.reviewer); self.assertEqual(client.get("/control/").status_code, 403)
        client.force_login(self.patient); self.assertEqual(client.get("/review/").status_code, 403)
        client.force_login(self.admin); self.assertEqual(client.get(f"/review/cards/{card.id}/").status_code, 403)

    def test_authenticated_brand_shows_account_identity(self):
        self.client.force_login(self.reviewer)
        response = self.client.get("/review/")
        self.assertContains(response, f"ID-{self.reviewer.id:04d}")
        self.assertContains(response, self.reviewer.username)

    def test_patient_progressive_form_renders(self):
        FormDefinition.objects.create(version=1, active=True, schema={"fields": [{"key": "field_sleep", "label": "Sleep", "type": "number", "required": False, "active": True}]})
        self.client.force_login(self.patient)
        response = self.client.get("/journal/")
        self.assertEqual(response.status_code, 200); self.assertContains(response, "field_sleep"); self.assertContains(response, "Joy")
        self.assertContains(response, "Choose significant emotions")
        self.assertContains(response, "Reflection")
        self.assertContains(response, 'class="face"')
        self.assertContains(response, "HOW DO YOU FEEL TODAY?")
        self.assertContains(response, "Move one step at a time.")
        self.assertNotContains(response, "Nothing except an emotion is required to begin.")
        self.assertNotContains(response, 'name="activities"')
        self.assertNotContains(response, 'name="journal"')
        self.assertContains(response, 'name="note_joy"')
        self.assertContains(response, 'data-progress-reflection hidden')
        self.assertContains(response, '<span class="stepno">04</span>', html=True)

    def test_patient_form_without_admin_prompts_numbers_attachments_as_step_three(self):
        self.client.force_login(self.patient)
        response = self.client.get(reverse("journal:today"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<section class="panel step" data-progress-reflection')
        self.assertContains(response, '<span class="stepno">03</span><h2>Attach context</h2>', html=True)

    def test_patient_signals_summarizes_submitted_cards(self):
        self.submitted(); self.client.force_login(self.patient)
        response = self.client.get("/journal/trends/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "EMOTIONAL")
        self.assertContains(response, "WEATHER")
        self.assertEqual(response.context["checkin_count"], 1)
        self.assertEqual(response.context["most_present"]["label"], "Joy")
        self.assertEqual(response.context["selected_range"], "all")
        self.assertContains(response, 'data-weather-view="table"')
        self.assertContains(response, 'data-weather-view="graph"')
        self.assertContains(response, 'id="weather-graph"')
        self.assertContains(response, "Grouped bars show every marked emotion’s intensity for each submitted check-in.")
        self.assertContains(response, "ONE GROUP PER SUBMITTED CHECK-IN")
        response = self.client.get("/journal/trends/?range=7d")
        self.assertEqual(response.context["selected_range_label"], "Past 7 days")
        self.assertContains(response, "Past 3 months")

    def test_checkin_table_marks_days_above_combined_intensity_threshold(self):
        fear = Emotion.objects.create(slug="fear", label="Fear", color="#ff3355")
        sadness = Emotion.objects.create(slug="sadness", label="Sadness", color="#5577ff")
        card = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 13))
        submit_card(card, {"emotions": [{"id": self.emotion.slug, "label": self.emotion.label, "intensity": 4}, {"id": fear.slug, "label": fear.label, "intensity": 5}, {"id": sadness.slug, "label": sadness.label, "intensity": 4}]}, [], self.patient)
        self.client.force_login(self.patient)
        response = self.client.get(reverse("journal:trends"))
        timeline = json.loads(response.context["timeline_json"])
        self.assertEqual(timeline[0]["total_intensity"], 13)
        self.assertEqual(response.context["high_intensity_threshold"], 8)
        self.assertEqual(response.context["very_high_intensity_threshold"], 12)
        self.assertContains(response, "HIGH TOTAL ≥ 8")
        self.assertContains(response, "VERY HIGH ≥ 12")
        self.assertContains(response, "is-very-high-load")

    def test_therapist_signals_uses_emotional_weather(self):
        card = self.submitted(); self.client.force_login(self.reviewer)
        response = self.client.get("/review/trends/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "EMOTIONAL")
        self.assertContains(response, "THERAPIST WEATHER DESK")
        self.assertContains(response, "SESSION PREP")
        self.assertContains(response, "What the patient recorded")
        self.assertContains(response, reverse("review:detail", args=[card.id]))

    def test_therapist_card_queue_distinguishes_unread_and_read(self):
        card = self.submitted(); self.client.force_login(self.reviewer)
        response = self.client.get("/review/")
        self.assertContains(response, "is-unread")
        self.assertContains(response, "Joy")
        self.assertNotContains(response, "COLLECTION")
        self.client.get(reverse("review:detail", args=[card.id]))
        response = self.client.get("/review/")
        self.assertContains(response, "is-read")
        response = self.client.post(reverse("review:metadata", args=[card.id]), {})
        self.assertRedirects(response, reverse("review:list"))
        self.assertFalse(ReviewerMetadata.objects.get(card=card, reviewer=self.reviewer).is_read)

    def test_therapist_calendar_stamps_card_with_prominent_emotion(self):
        card = self.submitted(); self.client.force_login(self.reviewer)
        response = self.client.get("/review/?view=calendar&month=2026-08")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "August 2026")
        self.assertContains(response, "emotion-stamp is-unread")
        self.assertContains(response, "Joy, intensity 4 of 5")
        self.assertContains(response, reverse("review:detail", args=[card.id]))

    def test_patient_archive_has_clean_list_and_calendar_without_review_controls(self):
        card = self.submitted(); self.client.force_login(self.patient)
        response = self.client.get(reverse("journal:history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PATIENT ARCHIVE")
        self.assertContains(response, 'class="view-tabs"')
        self.assertContains(response, "patient-review-card")
        self.assertContains(response, "Joy, intensity 4 of 5")
        self.assertContains(response, reverse("journal:detail", args=[card.id]))
        self.assertNotContains(response, "UNREAD")
        self.assertNotContains(response, "REVIEWED")
        response = self.client.get(reverse("journal:history") + "?view=calendar&month=2026-08")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "YOUR CARD CALENDAR")
        self.assertContains(response, 'class="emotion-stamp"')
        self.assertContains(response, "Joy, intensity 4 of 5")
        self.assertNotContains(response, "is-unread")

    def test_therapist_patient_dropdown_populates_and_scopes_cards(self):
        first_card = self.submitted()
        second = User.objects.create_user(username="second-patient", password="a-long-test-password", role=User.Role.PATIENT)
        CareRelationship.objects.create(therapist=self.reviewer, patient=second)
        second_card = Card.objects.create(patient=second, local_date=date(2026, 8, 14))
        submit_card(second_card, {"emotions": [{"id": "joy", "label": "Joy", "intensity": 2, "note": ""}], "activities": "Second account", "journal": "Different card"}, [], second)
        self.client.force_login(self.reviewer)
        response = self.client.get("/review/")
        self.assertContains(response, "patient")
        self.assertContains(response, "second-patient")
        response = self.client.post(reverse("review:patient_select"), {"patient_id": second.id, "next": "/review/trends/?range=all"})
        self.assertRedirects(response, "/review/trends/?range=all")
        response = self.client.get("/review/")
        self.assertEqual([card.id for card in response.context["cards"]], [second_card.id])
        self.assertNotEqual(second_card.id, first_card.id)
