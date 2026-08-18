from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0005_feedbackreport")]

    operations = [
        migrations.AddField(
            model_name="feedbackreport",
            name="subject",
            field=models.CharField(blank=True, max_length=140),
        ),
    ]
