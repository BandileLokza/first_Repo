from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def create_series_for_existing_competitions(apps, schema_editor):
    """Give every existing competition its own conservative series root."""

    PoolCompetition = apps.get_model("pool", "PoolCompetition")
    PoolCompetitionSeries = apps.get_model("pool", "PoolCompetitionSeries")

    for competition in PoolCompetition.objects.filter(series__isnull=True).iterator():
        series = PoolCompetitionSeries.objects.create(
            name=competition.name,
            # Competition slugs are already globally unique and are capped well
            # below this series field's 255-character limit.  Do not infer
            # relationships from matching display names.
            slug=f"{competition.slug}-series",
        )
        competition.series_id = series.pk
        competition.save(update_fields=["series"])


def remove_backfilled_series(apps, schema_editor):
    PoolCompetition = apps.get_model("pool", "PoolCompetition")
    PoolCompetitionSeries = apps.get_model("pool", "PoolCompetitionSeries")
    PoolCompetition.objects.update(series=None)
    PoolCompetitionSeries.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pool", "0014_poolmanualresultracksummary"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PoolCompetitionSeries",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddField(
            model_name="poolcompetition",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="poolcompetition",
            name="archived_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="archived_pool_competitions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="poolcompetition",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="poolcompetition",
            name="season",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="poolcompetition",
            name="series",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="competitions",
                to="pool.poolcompetitionseries",
            ),
        ),
        migrations.RunPython(
            create_series_for_existing_competitions,
            remove_backfilled_series,
        ),
        migrations.AlterField(
            model_name="poolcompetition",
            name="series",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="competitions",
                to="pool.poolcompetitionseries",
            ),
        ),
        migrations.AddConstraint(
            model_name="poolcompetition",
            constraint=models.UniqueConstraint(
                fields=("series", "season"),
                name="pool_unique_season_per_competition_series",
            ),
        ),
    ]
