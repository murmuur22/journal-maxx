from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0006_feedbackreport_subject")]
    operations = [
        migrations.AddField(
            model_name="therapistcomment",
            name="patient_read_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
