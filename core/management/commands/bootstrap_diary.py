import secrets
from django.core.management.base import BaseCommand, CommandError
from core.models import Emotion, FormDefinition, User
from core.security import encrypt_secret, generate_totp_secret, issue_recovery_codes

EMOTIONS = [
    ("joy", "Joy", "◕‿◕", "#ffd166"), ("calm", "Calm", "─‿─", "#79ffe1"),
    ("sadness", "Sadness", "╥﹏╥", "#62a8ff"), ("anger", "Anger", "ಠ益ಠ", "#ff4f64"),
    ("fear", "Fear", "⊙﹏⊙", "#b69cff"), ("shame", "Shame", "⌣_⌣", "#e78ac3"),
    ("hope", "Hope", "✦‿✦", "#a8ff60"), ("numb", "Numb", "•_•", "#9aa4b2"),
]

class Command(BaseCommand):
    help = "Create the first administrator and seed the diary configuration."
    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password")
        parser.add_argument("--enable-totp", action="store_true", help="Enroll an authenticator during bootstrap.")
    def handle(self, *args, **options):
        if User.objects.filter(role=User.Role.ADMIN).exists(): raise CommandError("An administrator already exists.")
        password = options.get("password") or secrets.token_urlsafe(18)
        totp_secret = generate_totp_secret() if options["enable_totp"] else ""
        user = User.objects.create_user(username=options["username"], password=password, role=User.Role.ADMIN, is_staff=False, is_superuser=False, totp_confirmed=options["enable_totp"], totp_secret_encrypted=encrypt_secret(totp_secret) if totp_secret else "")
        recovery_codes = issue_recovery_codes(user) if totp_secret else []
        for order, (slug, label, face, color) in enumerate(EMOTIONS): Emotion.objects.get_or_create(slug=slug, defaults={"label": label, "face": face, "color": color, "sort_order": order})
        FormDefinition.objects.get_or_create(version=1, defaults={"active": True, "schema": {"fields": []}})
        self.stdout.write(self.style.SUCCESS(f"Administrator created. Username: {options['username']} Temporary password: {password}"))
        self.stdout.write(self.style.WARNING("FIRST STEP: sign in, open Accounts, edit the current administrator, and replace the temporary password."))
        if totp_secret:
            self.stdout.write(self.style.WARNING(f"Authenticator key: {totp_secret} Recovery codes: {', '.join(recovery_codes)}"))
        else:
            self.stdout.write("Authenticator: not enrolled (optional; it can be added from Accounts later).")
