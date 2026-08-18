"""Client for the privileged host updater's deliberately small API."""

import json
import socket

from django.conf import settings


class UpdaterUnavailable(RuntimeError):
    pass


def updater_request(action, **parameters):
    payload = json.dumps({"action": action, **parameters}).encode("utf-8") + b"\n"
    received = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(settings.JOURNALMAX_UPDATER_TIMEOUT)
            client.connect(str(settings.JOURNALMAX_UPDATER_SOCKET))
            client.sendall(payload)
            while b"\n" not in received:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > 1024 * 1024:
                    raise UpdaterUnavailable("Updater response was too large")
    except (OSError, TimeoutError) as exc:
        raise UpdaterUnavailable("The host updater is not connected") from exc
    try:
        response = json.loads(bytes(received).split(b"\n", 1)[0])
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdaterUnavailable("The host updater returned an invalid response") from exc
    if not response.get("ok"):
        raise UpdaterUnavailable(str(response.get("error", "Updater request failed")))
    return response["result"]
