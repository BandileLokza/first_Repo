import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("matches", "0019_abandonedmatchdecision_optional_reference")]

    operations = [
        migrations.CreateModel(
            name="MatchResultProcessingOutbox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("requested_revision", models.PositiveBigIntegerField(default=1)),
                ("completed_revision", models.PositiveBigIntegerField(default=0)),
                ("state", models.CharField(choices=[("PENDING", "Pending"), ("COMPLETED", "Completed"), ("CANCELLED", "Cancelled")], default="PENDING", max_length=10)),
                ("pending_since", models.DateTimeField(blank=True, null=True)),
                ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("dispatch_token", models.UUIDField(blank=True, null=True)),
                ("dispatch_attempts", models.PositiveIntegerField(default=0)),
                ("processing_failures", models.PositiveIntegerField(default=0)),
                ("last_dispatch_at", models.DateTimeField(blank=True, null=True)),
                ("last_processing_at", models.DateTimeField(blank=True, null=True)),
                ("last_completed_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("match", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="result_processing", to="matches.match")),
            ],
            options={
                "indexes": [models.Index(fields=["state", "next_attempt_at", "id"], name="match_result_outbox_due_idx")],
                "constraints": [models.CheckConstraint(condition=models.Q(completed_revision__lte=models.F("requested_revision")), name="match_result_outbox_revision_order")],
            },
        ),
    ]
