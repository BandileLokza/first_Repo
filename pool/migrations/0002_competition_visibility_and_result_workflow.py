from django.db import migrations, models


def split_competition_status(apps, schema_editor):
    """Map the phase-one overloaded status field without hiding history."""
    PoolCompetition = apps.get_model("pool", "PoolCompetition")

    PoolCompetition.objects.filter(visibility="DRAFT").update(
        operational_state="UPCOMING"
    )
    PoolCompetition.objects.filter(visibility="PUBLISHED").update(
        operational_state="UPCOMING"
    )
    PoolCompetition.objects.filter(visibility="COMPLETED").update(
        visibility="PUBLISHED",
        operational_state="COMPLETED",
    )


def merge_competition_status(apps, schema_editor):
    """Provide a best-effort reverse mapping for an unapplied local migration."""
    PoolCompetition = apps.get_model("pool", "PoolCompetition")
    PoolCompetition.objects.filter(
        visibility="PUBLISHED",
        operational_state="COMPLETED",
    ).update(visibility="COMPLETED")


class Migration(migrations.Migration):

    dependencies = [
        ("pool", "0001_initial"),
    ]

    operations = [
        # Drop the old index before renaming its column.  SQLite remakes the
        # table while adding later fields, and cannot build an index that still
        # names the pre-rename `status` field.
        migrations.RemoveIndex(
            model_name="poolcompetition",
            name="pool_comp_status_date_idx",
        ),
        migrations.RenameField(
            model_name="poolcompetition",
            old_name="status",
            new_name="visibility",
        ),
        migrations.AddField(
            model_name="poolcompetition",
            name="operational_state",
            field=models.CharField(
                choices=[
                    ("UPCOMING", "Upcoming"),
                    ("ACTIVE", "Active"),
                    ("COMPLETED", "Completed"),
                ],
                default="UPCOMING",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="poolmatch",
            name="is_in_correction_mode",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Allows an authorised manager to correct a completed result "
                    "through the pool result service."
                ),
            ),
        ),
        migrations.AddField(
            model_name="poolmatchresultrevision",
            name="action",
            field=models.CharField(
                choices=[
                    ("RESULT_CORRECTED", "Result corrected"),
                    ("MATCH_STARTED", "Match started"),
                    ("RACK_RECORDED", "Rack recorded"),
                    ("RACK_CORRECTED", "Rack corrected"),
                    ("RACK_UNDONE", "Rack undone"),
                    ("CORRECTION_OPENED", "Correction opened"),
                    ("MATCH_COMPLETED", "Played match completed"),
                    ("NON_PLAYED_RESULT", "Non-played result recorded"),
                    ("MATCH_POSTPONED", "Match postponed"),
                    ("MATCH_CANCELLED", "Match cancelled"),
                    ("MATCH_ABANDONED", "Match abandoned"),
                ],
                default="RESULT_CORRECTED",
                max_length=24,
            ),
        ),
        migrations.RunPython(split_competition_status, merge_competition_status),
        migrations.AlterField(
            model_name="poolcompetition",
            name="visibility",
            field=models.CharField(
                choices=[("DRAFT", "Draft"), ("PUBLISHED", "Published")],
                default="DRAFT",
                max_length=10,
            ),
        ),
        migrations.AddIndex(
            model_name="poolcompetition",
            index=models.Index(
                fields=["visibility", "start_date"],
                name="pool_comp_visibility_date_idx",
            ),
        ),
    ]
