from django.db import migrations, models
import django.db.models.deletion


def preserve_existing_reviewer_access(apps, schema_editor):
    User = apps.get_model("core", "User")
    CareRelationship = apps.get_model("core", "CareRelationship")
    reviewers = User.objects.filter(role="reviewer").values_list("id", flat=True)
    patients = User.objects.filter(role="patient").values_list("id", flat=True)
    CareRelationship.objects.bulk_create(
        [CareRelationship(therapist_id=therapist_id, patient_id=patient_id) for therapist_id in reviewers for patient_id in patients],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_recoverycode")]
    operations = [
        migrations.CreateModel(
            name="CareRelationship",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("patient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="therapist_assignments", to="core.user")),
                ("therapist", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="patient_assignments", to="core.user")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("therapist", "patient"), name="one_therapist_patient_relationship")]},
        ),
        migrations.RunPython(preserve_existing_reviewer_access, migrations.RunPython.noop),
    ]
