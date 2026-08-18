from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Disable an account authenticator and/or clear temporary login throttles."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--disable-mfa", action="store_true")
        parser.add_argument("--clear-throttle", action="store_true")

    def handle(self, *args, **options):
        if not options["disable_mfa"] and not options["clear_throttle"]:
            raise CommandError("Choose --disable-mfa, --clear-throttle, or both.")
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError("Account not found.") from exc
        if options["disable_mfa"]:
            user.totp_confirmed = False
            user.totp_secret_encrypted = ""
            user.save(update_fields=["totp_confirmed", "totp_secret_encrypted"])
            user.recovery_codes.all().delete()
            self.stdout.write(self.style.SUCCESS(f"Authenticator disabled for {user.username}."))
        if options["clear_throttle"]:
            cache.clear()
            self.stdout.write(self.style.SUCCESS("Login throttle cache cleared."))
