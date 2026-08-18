import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0003_carerelationship")]
    operations = [
        migrations.AddField(
            model_name="card",
            name="comments_checksum",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.CreateModel(
            name="TherapistComment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("author_name", models.CharField(max_length=150)),
                ("body", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("card", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="therapist_comments", to="core.card")),
                ("reviewer", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="therapist_comments", to="core.user")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
    ]
