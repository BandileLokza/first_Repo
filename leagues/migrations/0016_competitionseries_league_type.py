from django.db import migrations, models


def classify_existing_region_series(apps, schema_editor):
    CompetitionSeries = apps.get_model("leagues", "CompetitionSeries")
    CompetitionSeries.objects.filter(region__isnull=False).update(league_type="REGIONAL")


class Migration(migrations.Migration):
    dependencies = [("leagues", "0015_region_competitionseries_region")]

    operations = [
        migrations.AddField(
            model_name="competitionseries",
            name="league_type",
            field=models.CharField(choices=[("REGIONAL", "Regional League"), ("ZONAL", "Zonal League")], db_index=True, default="ZONAL", max_length=10),
        ),
        migrations.RunPython(classify_existing_region_series, migrations.RunPython.noop),
    ]
