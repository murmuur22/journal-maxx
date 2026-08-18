import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from deploy.updater.journalmax_updater import Config, UpdateError, Updater, replace_env_value, semver_key
from .models import AuditEvent, User
from .updater import updater_request


class UpdaterControlTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="operator-update", password="a-long-test-password", role=User.Role.ADMIN)
        self.patient = User.objects.create_user(username="patient-update", password="a-long-test-password", role=User.Role.PATIENT)

    @patch("core.views.updater_request")
    def test_update_workspace_and_check_are_admin_only(self, request):
        request.return_value = {"installed_version": "0.2.0", "job": {"phase": "idle", "message": "Ready."}}
        self.client.force_login(self.admin)
        page = self.client.get(reverse("control:dashboard"))
        self.assertContains(page, "RELEASE CONTROL")
        self.assertContains(page, "HOST UPDATER CONNECTED")
        request.return_value = {
            "installed_version": "0.2.0",
            "latest": {"version": "0.3.0", "image": "ghcr.io/murmuur22/journal-maxx", "digest": "sha256:" + "a" * 64},
            "update_available": True,
            "release_url": "https://github.com/murmuur22/journal-maxx/releases/tag/v0.3.0",
            "job": {"phase": "idle", "message": "Ready."},
        }
        checked = self.client.post(reverse("control:update_check"))
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(checked.json()["update_available"])
        self.assertTrue(AuditEvent.objects.filter(action="system.update_checked", target_id="0.3.0").exists())
        self.client.force_login(self.patient)
        self.assertEqual(self.client.get(reverse("control:update_status")).status_code, 403)

    @patch("core.views.verify_second_factor")
    @patch("core.views.updater_request")
    def test_apply_requires_fresh_second_factor_and_is_audited(self, request, verify):
        self.client.force_login(self.admin)
        verify.return_value = False
        denied = self.client.post(reverse("control:update_apply"), {"version": "0.3.0", "code": "bad"})
        self.assertEqual(denied.status_code, 403)
        request.assert_not_called()
        verify.return_value = True
        request.return_value = {"accepted": True, "target_version": "0.3.0"}
        accepted = self.client.post(reverse("control:update_apply"), {"version": "0.3.0", "code": "123456"})
        self.assertEqual(accepted.status_code, 200)
        request.assert_called_once_with("apply", version="0.3.0")
        self.assertTrue(AuditEvent.objects.filter(action="system.update_requested", target_id="0.3.0").exists())


class HostUpdaterTests(TestCase):
    def test_application_client_uses_the_unix_json_protocol(self):
        class FakeSocket:
            sent = b""
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): self.timeout = timeout
            def connect(self, path): self.path = path
            def sendall(self, payload): self.sent = payload
            def recv(self, size):
                return json.dumps({"ok": True, "result": {"echo": json.loads(self.sent)["action"]}}).encode() + b"\n"
        fake = FakeSocket()
        with patch("core.updater.socket.socket", return_value=fake), override_settings(JOURNALMAX_UPDATER_SOCKET=Path("/run/test.sock"), JOURNALMAX_UPDATER_TIMEOUT=1):
            self.assertEqual(updater_request("status"), {"echo": "status"})
        self.assertEqual(fake.path, "/run/test.sock")

    def make_updater(self, root: Path):
        state = root / "application-state"
        state.mkdir()
        (state / "diary.sqlite3").write_bytes(b"known-good-database")
        env_file = root / ".env.production"
        env_file.write_text(f"JOURNALMAX_RELEASE=0.2.0\nDIARY_HOST_STATE_PATH={state}\n", encoding="utf-8")
        compose = root / "compose.production.yaml"
        compose.write_text("services: {}\n", encoding="utf-8")
        config = Config(
            repository="murmuur22/journal-maxx",
            image="ghcr.io/murmuur22/journal-maxx",
            compose_file=compose,
            env_file=env_file,
            state_dir=root / "updater-state",
            socket_path=root / "run" / "updater.sock",
            health_url="http://127.0.0.1:8800/health/ready",
        )
        return Updater(config), env_file, state / "diary.sqlite3"

    def test_semver_and_atomic_environment_replacement(self):
        self.assertLess(semver_key("1.9.9"), semver_key("2.0.0"))
        with self.assertRaises(UpdateError):
            semver_key("stable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.env"
            path.write_text("# keep this\nJOURNALMAX_RELEASE=0.2.0\nVALUE=yes\n", encoding="utf-8")
            replace_env_value(path, "JOURNALMAX_RELEASE", "0.3.0")
            self.assertEqual(path.read_text(), "# keep this\nJOURNALMAX_RELEASE=0.3.0\nVALUE=yes\n")

    def test_successful_update_keeps_backup_and_new_release(self):
        with tempfile.TemporaryDirectory() as directory:
            updater, env_file, _ = self.make_updater(Path(directory))
            commands = []
            updater._command = lambda arguments, **kwargs: commands.append(arguments)
            updater._compose = lambda *args, **kwargs: None
            updater._health_check = lambda: None
            updater._run_update({"version": "0.3.0", "digest": "sha256:" + "a" * 64})
            self.assertIn("JOURNALMAX_RELEASE=0.3.0", env_file.read_text())
            self.assertEqual(updater._read_state()["phase"], "complete")
            backups = list((updater.config.state_dir / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"known-good-database")
            attestation = next(command for command in commands if command[:3] == ["gh", "attestation", "verify"])
            self.assertIn("murmuur22/journal-maxx/.github/workflows/release.yml", attestation)
            self.assertIn("refs/tags/v0.3.0", attestation)
            self.assertIn("--deny-self-hosted-runners", attestation)

    def test_release_manifest_is_pinned_to_expected_image_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            updater, _, _ = self.make_updater(Path(directory))
            valid = {"schema_version": 1, "version": "0.3.0", "image": updater.config.image, "digest": "sha256:" + "c" * 64}
            updater._validate_manifest(valid)
            with self.assertRaises(UpdateError):
                updater._validate_manifest({**valid, "image": "ghcr.io/example/other"})
            with self.assertRaises(UpdateError):
                updater._validate_manifest({**valid, "digest": "latest"})

    def test_failed_health_check_restores_database_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            updater, env_file, database = self.make_updater(Path(directory))
            updater._command = lambda *args, **kwargs: None
            updater._compose = lambda *args, **kwargs: None
            attempts = 0
            def health_check():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    database.write_bytes(b"candidate-database")
                    raise UpdateError("candidate did not become ready")
            updater._health_check = health_check
            updater._run_update({"version": "0.3.0", "digest": "sha256:" + "b" * 64})
            self.assertIn("JOURNALMAX_RELEASE=0.2.0", env_file.read_text())
            self.assertEqual(database.read_bytes(), b"known-good-database")
            state = json.loads((updater.config.state_dir / "state.json").read_text())
            self.assertEqual(state["phase"], "rolled_back")
