from django.core.management.base import BaseCommand, CommandError
from core.models import User
from core.security import encrypt_secret, generate_totp_secret, issue_recovery_codes

class Command(BaseCommand):
    help = "Create a patient or reviewer account."
    def add_arguments(self, parser):
        parser.add_argument("username"); parser.add_argument("--role", choices=[User.Role.PATIENT, User.Role.REVIEWER], required=True); parser.add_argument("--password", required=True); parser.add_argument("--enable-totp", action="store_true")
    def handle(self, *args, **options):
        if options["role"] == User.Role.PATIENT and User.objects.filter(role=User.Role.PATIENT).exists(): raise CommandError("Only one patient is supported.")
        extra = {}
        if options["role"] == User.Role.REVIEWER and options["enable_totp"]:
            secret = generate_totp_secret(); extra = {"totp_confirmed": True, "totp_secret_encrypted": encrypt_secret(secret)}
        user = User.objects.create_user(username=options["username"], password=options["password"], role=options["role"], **extra)
        if options["role"] == User.Role.REVIEWER and options["enable_totp"]:
            codes = issue_recovery_codes(user); self.stdout.write(self.style.WARNING(f"Authenticator key: {secret} Recovery codes: {', '.join(codes)}"))
        self.stdout.write(self.style.SUCCESS("Account created."))
