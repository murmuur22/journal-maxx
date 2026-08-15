import hashlib
import secrets
from django.core.management.base import BaseCommand
from core.models import ApiToken, User

class Command(BaseCommand):
    help = "Create a read-only personal API token (printed once)."
    def add_arguments(self, parser): parser.add_argument("username"); parser.add_argument("--name", default="terminal")
    def handle(self, *args, **options):
        user = User.objects.get(username=options["username"]); raw = "dcard_" + secrets.token_urlsafe(32)
        ApiToken.objects.create(user=user, name=options["name"], prefix=raw[:12], token_hash=hashlib.sha256(raw.encode()).hexdigest(), scopes=["cards:metadata", "system:read"])
        self.stdout.write(self.style.SUCCESS(f"Token (shown once): {raw}"))
