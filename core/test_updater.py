import io
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
        page = self.client.get(reverse("control:updates"))
        self.assertContains(page, "RELEASE CONTROL")
        self.assertContains(page, "HOST UPDATER CONNECTED")
        self.assertContains(page, "Legacy GitHub credentials")
        self.assertContains(page, "Changelog")
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
        self.assertEqual(self.client.get(reverse("control:updates")).status_code, 403)
        self.assertEqual(self.client.get(reverse("control:update_status")).status_code, 403)

    @patch("core.views.updater_request")
    def test_apply_requires_current_password_when_mfa_is_not_enrolled(self, request):
        self.client.force_login(self.admin)
        denied = self.client.post(reverse("control:update_apply"), {"version": "0.3.0", "code": "bad"})
        self.assertEqual(denied.status_code, 403)
        request.assert_not_called()
        request.return_value = {"accepted": True, "target_version": "0.3.0"}
        accepted = self.client.post(reverse("control:update_apply"), {"version": "0.3.0", "code": "a-long-test-password"})
        self.assertEqual(accepted.status_code, 200)
        request.assert_called_once_with("apply", version="0.3.0")
        self.assertTrue(AuditEvent.objects.filter(action="system.update_requested", target_id="0.3.0").exists())

    @patch("core.views.updater_request")
    def test_credential_purge_requires_password_and_is_audited(self, request):
        self.client.force_login(self.admin)
        denied = self.client.post(reverse("control:update_credentials_purge"), {"code": "bad"})
        self.assertEqual(denied.status_code, 403)
        request.assert_not_called()
        request.return_value = {"legacy_github_credentials": {"clean": True, "findings": [], "removed_count": 3}}
        accepted = self.client.post(reverse("control:update_credentials_purge"), {"code": "a-long-test-password"})
        self.assertEqual(accepted.status_code, 200)
        request.assert_called_once_with("purge_legacy_github_credentials")
        self.assertTrue(AuditEvent.objects.filter(action="system.legacy_credentials_purged", actor=self.admin).exists())


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
            updater_env_file=root / "updater.env",
            root_gh_hosts_file=root / "root-gh-hosts.yml",
            root_docker_config_file=root / "root-docker-config.json",
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
            manifest = {"version": "0.3.0", "digest": "sha256:" + "a" * 64}
            updater._run_update(manifest, json.dumps(manifest).encode(), b"{}")
            self.assertIn("JOURNALMAX_RELEASE=0.3.0", env_file.read_text())
            self.assertEqual(updater._read_state()["phase"], "complete")
            backups = list((updater.config.state_dir / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"known-good-database")
            attestation = next(command for command in commands if command[:3] == ["gh", "attestation", "verify"])
            self.assertNotIn("oci://", " ".join(attestation))
            self.assertIn("--bundle", attestation)
            self.assertIn("murmuur22/journal-maxx/.github/workflows/release.yml", attestation)
            self.assertIn("refs/tags/v0.3.0", attestation)
            self.assertIn("--deny-self-hosted-runners", attestation)

    def test_release_manifest_is_pinned_to_expected_image_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            updater, _, _ = self.make_updater(Path(directory))
            valid = {"schema_version": 2, "updater_protocol": 2, "version": "0.3.0", "image": updater.config.image, "digest": "sha256:" + "c" * 64, "changelog": ["A tested change."]}
            updater._validate_manifest(valid)
            with self.assertRaises(UpdateError):
                updater._validate_manifest({**valid, "image": "ghcr.io/example/other"})
            with self.assertRaises(UpdateError):
                updater._validate_manifest({**valid, "digest": "latest"})

    @patch("deploy.updater.journalmax_updater.urllib.request.urlopen")
    def test_release_discovery_downloads_manifest_and_bundle_without_auth_headers(self, urlopen):
        with tempfile.TemporaryDirectory() as directory:
            updater, _, _ = self.make_updater(Path(directory))
            manifest = {"schema_version": 2, "updater_protocol": 2, "version": "0.4.0", "image": updater.config.image, "digest": "sha256:" + "d" * 64, "changelog": ["Credential-free updates."]}
            release = {"html_url": "https://example/release", "assets": [
                {"name": "journalmax-release.json", "browser_download_url": "https://example/manifest"},
                {"name": "journalmax-release.attestation.json", "browser_download_url": "https://example/bundle"},
            ]}
            urlopen.side_effect = [io.BytesIO(json.dumps(release).encode()), io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(b"{}")]
            result, release_url, manifest_bytes, bundle_bytes = updater._release_artifacts()
            self.assertEqual(result["version"], "0.4.0"); self.assertEqual(release_url, "https://example/release")
            self.assertEqual(json.loads(manifest_bytes), manifest); self.assertEqual(bundle_bytes, b"{}")
            for call in urlopen.call_args_list:
                self.assertNotIn("Authorization", dict(call.args[0].header_items()))

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
            manifest = {"version": "0.3.0", "digest": "sha256:" + "b" * 64}
            updater._run_update(manifest, json.dumps(manifest).encode(), b"{}")
            self.assertIn("JOURNALMAX_RELEASE=0.2.0", env_file.read_text())
            self.assertEqual(database.read_bytes(), b"known-good-database")
            state = json.loads((updater.config.state_dir / "state.json").read_text())
            self.assertEqual(state["phase"], "rolled_back")

    def test_legacy_credential_purge_is_scoped_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); updater, _, _ = self.make_updater(root)
            updater.config.updater_env_file.write_text("GH_TOKEN=secret\nKEEP=yes\n", encoding="utf-8")
            updater.config.root_gh_hosts_file.write_text("github.com:\n  user: operator\n  oauth_token: secret\n", encoding="utf-8")
            updater.config.root_docker_config_file.write_text(json.dumps({"auths": {"ghcr.io": {"auth": "secret"}, "registry.example": {"auth": "keep"}}, "credHelpers": {"ghcr.io": "secretservice", "registry.example": "pass"}}), encoding="utf-8")
            before = updater.legacy_credentials_status()
            self.assertFalse(before["clean"]); self.assertEqual(len(before["findings"]), 4)
            result = updater.purge_legacy_credentials()
            self.assertTrue(result["clean"]); self.assertEqual(result["removed_count"], 4)
            self.assertEqual(updater.config.updater_env_file.read_text(), "KEEP=yes\n")
            self.assertNotIn("oauth_token", updater.config.root_gh_hosts_file.read_text())
            docker = json.loads(updater.config.root_docker_config_file.read_text())
            self.assertIn("registry.example", docker["auths"]); self.assertNotIn("ghcr.io", docker["auths"])
            self.assertIn("registry.example", docker["credHelpers"]); self.assertNotIn("ghcr.io", docker["credHelpers"])

    def test_legacy_credential_purge_refuses_invalid_docker_config_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); updater, _, _ = self.make_updater(root)
            updater.config.updater_env_file.write_text("GH_TOKEN=secret\n", encoding="utf-8")
            updater.config.root_docker_config_file.write_text("not-json", encoding="utf-8")
            with self.assertRaises(UpdateError): updater.purge_legacy_credentials()
            self.assertEqual(updater.config.updater_env_file.read_text(), "GH_TOKEN=secret\n")
