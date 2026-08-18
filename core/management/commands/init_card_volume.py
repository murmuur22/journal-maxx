import json
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.services import CARD_VOLUME_FORMAT, CARD_VOLUME_SCHEMA_VERSION, atomic_write


class Command(BaseCommand):
    help = "Initialize the configured DIARY_CARD_ROOT as a recognized Journalmax card volume."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm-path",
            required=True,
            help="Must exactly match the configured card root. This prevents initializing the wrong directory.",
        )
        parser.add_argument(
            "--adopt-existing",
            action="store_true",
            help="Permit adding an identity marker to a non-empty existing card archive.",
        )

    def handle(self, *args, **options):
        root = settings.CARD_ROOT
        marker_path = root / getattr(settings, "CARD_VOLUME_MARKER", ".journalmax-volume.json")
        if options["confirm_path"] != str(root):
            raise CommandError(f"Confirmation did not match configured card root: {root}")
        if not root.exists():
            raise CommandError("The configured directory does not exist. Mount the storage volume before initializing it.")
        if not root.is_dir():
            raise CommandError("The configured card root is not a directory.")
        if marker_path.exists():
            raise CommandError(f"A volume identity already exists at {marker_path}; nothing was changed.")
        if any(root.iterdir()) and not options["adopt_existing"]:
            raise CommandError("The card root is not empty. Inspect it, then repeat with --adopt-existing to preserve and identify the existing archive.")

        marker = {
            "format": CARD_VOLUME_FORMAT,
            "schema_version": CARD_VOLUME_SCHEMA_VERSION,
            "volume_id": str(uuid.uuid4()),
            "created_at": timezone.now().isoformat(),
        }
        atomic_write(marker_path, (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        self.stdout.write(self.style.SUCCESS("Journalmax card volume initialized."))
        self.stdout.write(f"Path: {root}")
        self.stdout.write(f"Volume ID: {marker['volume_id']}")
        self.stdout.write("Set DIARY_CARD_VOLUME_ID to this ID to pin the installation to this exact volume.")
