from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import User


PREVIEW_ACCOUNTS = (
    ("admin-preview", User.Role.ADMIN),
    ("therapist-preview", User.Role.REVIEWER),
    ("patient-preview", User.Role.PATIENT),
)


class Command(BaseCommand):
    help = "Create or repair the three password-only local preview accounts."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Preview accounts can only be configured while DIARY_DEBUG=1.")

        password = options["password"]
        if len(password) < 12:
            raise CommandError("The preview passphrase must be at least 12 characters.")

        for username, role in PREVIEW_ACCOUNTS:
            account, created = User.objects.get_or_create(username=username, defaults={"role": role})
            account.role = role
            account.is_active = True
            account.is_staff = False
            account.is_superuser = False
            account.totp_confirmed = False
            account.totp_secret_encrypted = ""
            account.set_password(password)
            account.save()
            account.recovery_codes.all().delete()
            state = "created" if created else "repaired"
            self.stdout.write(f"{username}: {state} as {role}, password-only access enabled")

        cache.clear()
        self.stdout.write(self.style.SUCCESS("Preview accounts are ready; login throttles were cleared."))
