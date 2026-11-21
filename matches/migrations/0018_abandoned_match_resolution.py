# Generated manually for the abandonment-resolution workflow.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions


def backfill_historical_abandonments(apps, schema_editor):
    """Freeze the existing on-field state of abandoned legacy fixtures.

    Pre-workflow fixtures already have a score, clock and scheduled date, but
    cannot have the new explicit snapshot values yet.  Preserve those values
    so a later correction/reopen restores the historical result correctly.
    ``abandoned_at`` is intentionally left null: its real value is not known
    from the legacy schema.
    """

    Match = apps.get_model("matches", "Match")
    Match.objects.filter(status="ABANDONED").update(
        abandoned_home_score=models.F("home_score"),
        abandoned_away_score=models.F("away_score"),
        abandoned_minute=models.F("minute"),
        abandoned_phase=models.F("phase"),
        abandoned_scheduled_for=models.F("match_date"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0017_match_generated_by_system"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="match",
            name="abandoned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="abandoned_away_score",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="abandoned_home_score",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="abandoned_minute",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="abandoned_phase",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="match",
            name="abandoned_scheduled_for",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="match",
            name="abandonment_resolution",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Awaiting official resolution"),
                    ("REPLAY", "Full replay ordered"),
                    ("RESUMED", "Resumption approved"),
                    ("AWARDED", "Official result awarded"),
                    ("CONFIRMED", "Score at abandonment confirmed"),
                    ("VOID", "Match voided"),
                ],
                db_index=True,
                default="",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="replay_of",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replays",
                to="matches.match",
            ),
        ),
        migrations.RunPython(
            backfill_historical_abandonments,
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="match",
            name="unique_match_per_leg",
        ),
        migrations.AddConstraint(
            model_name="match",
            constraint=models.UniqueConstraint(
                condition=models.Q(("replay_of__isnull", True)),
                fields=("competition", "stage", "home_team", "away_team", "leg"),
                name="unique_primary_match_per_leg",
            ),
        ),
        migrations.AddConstraint(
            model_name="match",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["UPCOMING", "LIVE"])),
                fields=("replay_of",),
                name="unique_active_replay_per_source",
            ),
        ),
        migrations.CreateModel(
            name="AbandonedMatchDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("decision_type", models.CharField(choices=[("REPLAY", "Schedule full replay"), ("RESUME", "Resume match"), ("AWARD", "Award official result"), ("CONFIRM", "Confirm score at abandonment"), ("VOID", "Void match")], max_length=12)),
                ("reason", models.TextField()),
                ("decision_reference", models.CharField(max_length=200)),
                ("official_home_score", models.PositiveIntegerField(blank=True, null=True)),
                ("official_away_score", models.PositiveIntegerField(blank=True, null=True)),
                ("scheduled_for", models.DateTimeField(blank=True, null=True)),
                ("venue", models.CharField(blank=True, max_length=200)),
                ("resume_minute", models.PositiveIntegerField(blank=True, null=True)),
                ("decided_at", models.DateTimeField(auto_now_add=True)),
                ("is_current", models.BooleanField(db_index=True, default=True)),
                ("superseded_at", models.DateTimeField(blank=True, null=True)),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="abandoned_match_decisions", to=settings.AUTH_USER_MODEL)),
                ("match", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="abandonment_decisions", to="matches.match")),
                ("replay_match", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_by_abandonment_decisions", to="matches.match")),
                ("superseded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="supersedes", to="matches.abandonedmatchdecision")),
            ],
            options={"ordering": ["-decided_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="abandonedmatchdecision",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_current", True)),
                fields=("match",),
                name="one_current_abandonment_decision_per_match",
            ),
        ),
        migrations.AddConstraint(
            model_name="abandonedmatchdecision",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        models.Q(
                            ("official_away_score__isnull", True),
                            ("official_home_score__isnull", True),
                        ),
                        models.Q(
                            ("official_away_score__isnull", False),
                            ("official_home_score__isnull", False),
                        ),
                        _connector="OR",
                    )
                ),
                name="abandonment_decision_scores_paired",
            ),
        ),
    ]
