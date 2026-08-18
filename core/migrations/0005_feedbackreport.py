from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_therapistcomment_card_comments_checksum"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FeedbackReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("author_name", models.CharField(max_length=150)),
                ("author_role", models.CharField(choices=[("patient", "Patient"), ("reviewer", "Therapist"), ("admin", "Administrator")], max_length=16)),
                ("kind", models.CharField(choices=[("feedback", "Feedback"), ("bug", "Bug report")], default="feedback", max_length=16)),
                ("body", models.TextField(max_length=4000)),
                ("page", models.CharField(blank=True, max_length=500)),
                ("important", models.BooleanField(default=False)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("archived_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="archived_feedback_reports", to=settings.AUTH_USER_MODEL)),
                ("author", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="feedback_reports", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-important", "-created_at"]},
        ),
    ]
