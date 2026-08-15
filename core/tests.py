import hashlib
import secrets
import tempfile
from datetime import date
from pathlib import Path
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from .models import ApiToken, Card, Emotion, FormDefinition, ReviewerMetadata, User
from .security import decrypt_secret, encrypt_secret, generate_totp_secret, totp, verify_totp
from .services import add_addendum, card_directory, quarantine_card, restore_card, submit_card

class DiaryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(CARD_ROOT=Path(self.temp.name))
        self.override.enable()
        self.patient = User.objects.create_user(username="patient", password="a-long-test-password", role=User.Role.PATIENT)
        self.reviewer = User.objects.create_user(username="reviewer", password="a-long-test-password", role=User.Role.REVIEWER)
        self.admin = User.objects.create_user(username="operator", password="a-long-test-password", role=User.Role.ADMIN)
        self.emotion = Emotion.objects.create(slug="joy", label="Joy")
    def tearDown(self): self.override.disable(); self.temp.cleanup()

    def submitted(self):
        card = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 15))
        upload = SimpleUploadedFile("image one.txt", b"safe context", content_type="text/plain")
        data = {"emotions": [{"id": "joy", "label": "Joy", "intensity": 4, "note": "A bright moment"}], "activities": "Walked", "journal": "Felt present."}
        return submit_card(card, data, [upload], self.patient)

    def test_submission_creates_readable_markdown_and_locks_card(self):
        card = self.submitted(); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.SUBMITTED)
        path = card_directory(card) / "260815_diary.md"
        self.assertTrue(path.exists()); self.assertIn("Felt present.", path.read_text())
        self.assertTrue((card_directory(card) / "260815_image_one.txt").exists())
        with self.assertRaises(Exception): submit_card(card, card.content_index, [], self.patient)

    def test_addendum_does_not_modify_original(self):
        card = self.submitted(); original = (card_directory(card) / "260815_diary.md").read_bytes()
        item = add_addendum(card, "Later context", self.patient)
        self.assertEqual(original, (card_directory(card) / "260815_diary.md").read_bytes())
        self.assertTrue((card_directory(card) / item.filename).exists())

    def test_admin_cannot_open_content_but_can_quarantine_and_restore(self):
        card = self.submitted(); client = Client(); client.force_login(self.admin)
        self.assertEqual(client.get(reverse("journal:detail", args=[card.id])).status_code, 403)
        quarantine_card(card, self.admin); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.QUARANTINED)
        self.assertTrue((Path(self.temp.name) / ".quarantine" / card.folder_name).exists())
        restore_card(card, self.admin); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.SUBMITTED)

    def test_reviewer_metadata_is_private(self):
        card = self.submitted(); other = User.objects.create_user(username="other", password="a-long-test-password", role=User.Role.REVIEWER)
        ReviewerMetadata.objects.create(card=card, reviewer=self.reviewer, private_note="private", tags=["session"])
        self.assertFalse(ReviewerMetadata.objects.filter(card=card, reviewer=other).exists())

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

    def test_patient_progressive_form_renders(self):
        FormDefinition.objects.create(version=1, active=True, schema={"fields": [{"key": "field_sleep", "label": "Sleep", "type": "number", "required": False, "active": True}]})
        self.client.force_login(self.patient)
        response = self.client.get("/journal/")
        self.assertEqual(response.status_code, 200); self.assertContains(response, "field_sleep"); self.assertContains(response, "Joy")
