from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Card
from core.services import purge_card

class Command(BaseCommand):
    help = "Permanently purge quarantined cards whose seven-day recovery window expired."
    def handle(self, *args, **options):
        count = 0
        for card in Card.objects.filter(status=Card.Status.QUARANTINED, purge_after__lte=timezone.now()):
            purge_card(card); count += 1
        self.stdout.write(self.style.SUCCESS(f"Purged {count} card(s)."))
