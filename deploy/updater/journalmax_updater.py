#!/usr/bin/env python3
"""Small, root-owned update broker for a JOURNALMAX production host.

The web application can request one of three fixed operations over a Unix
socket. It cannot submit commands, paths, image names, or digests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_REQUEST_BYTES = 16 * 1024


class UpdateError(RuntimeError):
    pass


def semver_key(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(str(value))
    if not match:
        raise UpdateError(f"Invalid release version: {value!r}")
    return tuple(int(part) for part in match.groups())


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def replace_env_value(path: Path, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise UpdateError("Environment values may not contain newlines")
    original = path.read_text(encoding="utf-8")
    replacement = f"{key}={value}"
    lines = original.splitlines()
    found = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(f"{key}="):
            lines[index] = replacement
            found = True
            break
    if not found:
        lines.append(replacement)
    rendered = "\n".join(lines) + "\n"
    mode = path.stat().st_mode & 0o777
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Config:
    repository: str
    image: str
    compose_file: Path
    env_file: Path
    state_dir: Path
    socket_path: Path
    health_url: str
    service_name: str = "diary"
    socket_group: int = 10001
    health_timeout_seconds: int = 120

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text(encoding="utf-8"))
        expected_repository = "murmuur22/journal-maxx"
        expected_image = "ghcr.io/murmuur22/journal-maxx"
        if raw.get("repository", expected_repository) != expected_repository:
            raise UpdateError("The updater is locked to the JOURNALMAX repository")
        if raw.get("image", expected_image) != expected_image:
            raise UpdateError("The updater is locked to the JOURNALMAX image")
        config = cls(
            repository=expected_repository,
            image=expected_image,
            compose_file=Path(raw["compose_file"]),
            env_file=Path(raw["env_file"]),
            state_dir=Path(raw.get("state_dir", "/var/lib/journalmax/updater")),
            socket_path=Path(raw.get("socket_path", "/run/journalmax-updater/updater.sock")),
            health_url=raw.get("health_url", "http://127.0.0.1:8800/health/ready"),
            service_name=raw.get("service_name", "diary"),
            socket_group=int(raw.get("socket_group", 10001)),
            health_timeout_seconds=int(raw.get("health_timeout_seconds", 120)),
        )
        for candidate in (config.compose_file, config.env_file, config.state_dir, config.socket_path):
            if not candidate.is_absolute():
                raise UpdateError(f"Updater paths must be absolute: {candidate}")
        return config


class Updater:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._job_thread: threading.Thread | None = None
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        (self.config.state_dir / "backups").mkdir(mode=0o700, exist_ok=True)
        self._state_path = self.config.state_dir / "state.json"
        self._log_path = self.config.state_dir / "updater.log"
        if not self._state_path.exists():
            self._write_state({"phase": "idle", "message": "No update is running."})

    def _log(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{timestamp} {message}\n")

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"phase": "unknown", "message": "Updater state could not be read."}

    def _write_state(self, state: dict[str, Any]) -> None:
        state = {**state, "updated_at": datetime.now(timezone.utc).isoformat()}
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)

    def _installed_version(self) -> str:
        version = read_env(self.config.env_file).get("JOURNALMAX_RELEASE", "")
        semver_key(version)
        return version

    def _release_json(self) -> tuple[dict[str, Any], str]:
        api_url = f"https://api.github.com/repos/{self.config.repository}/releases/latest"
        request = urllib.request.Request(
            api_url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "journalmax-updater/1"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.load(response)
        asset_url = next(
            (asset["browser_download_url"] for asset in release.get("assets", []) if asset.get("name") == "journalmax-release.json"),
            None,
        )
        if not asset_url:
            raise UpdateError("Latest release has no journalmax-release.json asset")
        manifest_request = urllib.request.Request(asset_url, headers={"User-Agent": "journalmax-updater/1"})
        with urllib.request.urlopen(manifest_request, timeout=15) as response:
            manifest = json.load(response)
        self._validate_manifest(manifest)
        return manifest, str(release.get("html_url", ""))

    def _validate_manifest(self, manifest: dict[str, Any]) -> None:
        if manifest.get("schema_version") != 1:
            raise UpdateError("Unsupported release manifest schema")
        semver_key(manifest.get("version", ""))
        if manifest.get("image") != self.config.image:
            raise UpdateError("Release manifest names an unexpected container image")
        if not DIGEST.fullmatch(str(manifest.get("digest", ""))):
            raise UpdateError("Release manifest has an invalid image digest")

    def status(self, include_latest: bool = False) -> dict[str, Any]:
        installed = self._installed_version()
        result: dict[str, Any] = {"installed_version": installed, "job": self._read_state()}
        if include_latest:
            manifest, release_url = self._release_json()
            result.update(
                latest=manifest,
                release_url=release_url,
                update_available=semver_key(manifest["version"]) > semver_key(installed),
            )
        return result

    def apply(self, requested_version: str) -> dict[str, Any]:
        semver_key(requested_version)
        with self._lock:
            if self._job_thread and self._job_thread.is_alive():
                raise UpdateError("An update is already running")
            manifest, release_url = self._release_json()
            if manifest["version"] != requested_version:
                raise UpdateError("Requested release is no longer the latest release")
            installed = self._installed_version()
            if semver_key(requested_version) <= semver_key(installed):
                raise UpdateError("Requested release is not newer than the installed release")
            self._write_state({"phase": "queued", "message": f"Preparing JOURNALMAX {requested_version}.", "target_version": requested_version})
            self._job_thread = threading.Thread(
                target=self._run_update,
                args=(manifest,),
                name=f"journalmax-update-{requested_version}",
                daemon=True,
            )
            self._job_thread.start()
        return {"accepted": True, "target_version": requested_version, "release_url": release_url}

    def _command(self, arguments: list[str], timeout: int = 600) -> None:
        self._log("RUN " + " ".join(arguments))
        completed = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=timeout)
        if completed.stdout.strip():
            self._log(completed.stdout.strip()[-4000:])
        if completed.stderr.strip():
            self._log(completed.stderr.strip()[-4000:])
        if completed.returncode:
            raise UpdateError(f"Command failed ({completed.returncode}): {' '.join(arguments)}")

    def _compose(self, *arguments: str, timeout: int = 600) -> None:
        self._command([
            "docker", "compose", "--env-file", str(self.config.env_file),
            "-f", str(self.config.compose_file), *arguments,
        ], timeout=timeout)

    def _health_check(self) -> None:
        deadline = time.monotonic() + self.config.health_timeout_seconds
        last_error = "service did not answer"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.config.health_url, timeout=5) as response:
                    if 200 <= response.status < 300:
                        return
                    last_error = f"HTTP {response.status}"
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(2)
        raise UpdateError(f"Health check failed: {last_error}")

    def _database_path(self) -> Path:
        values = read_env(self.config.env_file)
        state_path = values.get("DIARY_HOST_STATE_PATH", "")
        if not state_path:
            raise UpdateError("DIARY_HOST_STATE_PATH is missing from the production environment")
        root = Path(state_path)
        if not root.is_absolute():
            raise UpdateError("DIARY_HOST_STATE_PATH must be absolute")
        return root / "diary.sqlite3"

    def _run_update(self, manifest: dict[str, Any]) -> None:
        target = manifest["version"]
        old_version = ""
        database: Path | None = None
        backup: Path | None = None
        stopped = False
        backed_up = False
        try:
            old_version = self._installed_version()
            database = self._database_path()
            backup = self.config.state_dir / "backups" / f"diary-{old_version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
            self._write_state({"phase": "downloading", "message": f"Downloading and verifying JOURNALMAX {target}.", "target_version": target})
            reference = f"{self.config.image}@{manifest['digest']}"
            self._command(["docker", "pull", reference])
            self._command([
                "gh", "attestation", "verify", f"oci://{reference}",
                "-R", self.config.repository,
                "--signer-workflow", f"{self.config.repository}/.github/workflows/release.yml",
                "--source-ref", f"refs/tags/v{target}",
                "--deny-self-hosted-runners",
            ])
            self._command(["docker", "tag", reference, f"{self.config.image}:{target}"])

            self._write_state({"phase": "backing_up", "message": "Stopping JOURNALMAX and backing up its database.", "target_version": target})
            self._compose("stop", self.config.service_name)
            stopped = True
            if database.exists():
                shutil.copy2(database, backup)
                backed_up = True
                self._log(f"Database backup written to {backup}")

            replace_env_value(self.config.env_file, "JOURNALMAX_RELEASE", target)
            self._write_state({"phase": "starting", "message": f"Starting JOURNALMAX {target}.", "target_version": target})
            self._compose("up", "-d", "--force-recreate", self.config.service_name)
            self._health_check()
            self._write_state({"phase": "complete", "message": f"JOURNALMAX {target} is healthy.", "target_version": target})
            self._log(f"Update from {old_version} to {target} completed")
        except Exception as exc:
            self._log(f"Update failed: {exc}")
            rollback_error = ""
            try:
                if stopped and old_version and database is not None:
                    self._compose("stop", self.config.service_name)
                    replace_env_value(self.config.env_file, "JOURNALMAX_RELEASE", old_version)
                    if backed_up and backup is not None:
                        shutil.copy2(backup, database)
                    self._compose("up", "-d", "--force-recreate", self.config.service_name)
                    self._health_check()
            except Exception as rollback_exc:
                rollback_error = f" Rollback also failed: {rollback_exc}"
                self._log(rollback_error.strip())
            phase = "rolled_back" if stopped and not rollback_error else "failed"
            self._write_state({
                "phase": phase,
                "message": f"Update failed. {'The previous release was restored.' if phase == 'rolled_back' else 'Manual recovery is required.'}",
                "target_version": target,
                "error": (str(exc) + rollback_error)[-1000:],
            })

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "status":
            return self.status(include_latest=False)
        if action == "check":
            return self.status(include_latest=True)
        if action == "apply":
            return self.apply(str(request.get("version", "")))
        raise UpdateError("Unknown updater action")


class UpdateRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            response = {"ok": False, "error": "Invalid updater request"}
        else:
            try:
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise UpdateError("Updater request must be an object")
                response = {"ok": True, "result": self.server.updater.handle(request)}  # type: ignore[attr-defined]
            except (UpdateError, ValueError, OSError, urllib.error.URLError) as exc:
                response = {"ok": False, "error": str(exc)}
            except Exception:
                response = {"ok": False, "error": "Unexpected updater error"}
        self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")


class UnixUpdateServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(config: Config) -> None:
    updater = Updater(config)
    config.socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chown(config.socket_path.parent, 0, config.socket_group)
    os.chmod(config.socket_path.parent, 0o750)
    config.socket_path.unlink(missing_ok=True)
    server = UnixUpdateServer(str(config.socket_path), UpdateRequestHandler)
    server.updater = updater  # type: ignore[attr-defined]
    os.chown(config.socket_path, 0, config.socket_group)
    os.chmod(config.socket_path, 0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        config.socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="JOURNALMAX host update broker")
    parser.add_argument("--config", type=Path, default=Path("/etc/journalmax/updater.json"))
    arguments = parser.parse_args()
    serve(Config.load(arguments.config))


if __name__ == "__main__":
    main()
