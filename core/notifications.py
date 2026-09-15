"""Build the patient inbox using a shared shape for notification sources."""
from django.urls import reverse

from .models import Card, TherapistComment, User


def unread_notifications(user):
    if not user.is_authenticated or user.role != User.Role.PATIENT:
        return []
    comments = TherapistComment.objects.filter(
        card__patient=user, card__status=Card.Status.SUBMITTED,
        patient_read_at__isnull=True,
    ).select_related("card").only("id", "author_name", "created_at", "card__id", "card__local_date").order_by("-created_at", "-id")
    # Future system notifications can provide the same kind/title/detail/url/time fields.
    return [{
        "kind": "therapist_comment",
        "title": f"New comment from {comment.author_name}",
        "detail": f"Diary card · {comment.card.local_date:%a, %d %b %Y}",
        "url": reverse("journal:detail", args=[comment.card_id]) + f"#comment-{comment.pk}",
        "created_at": comment.created_at,
    } for comment in comments.iterator(chunk_size=200)]
