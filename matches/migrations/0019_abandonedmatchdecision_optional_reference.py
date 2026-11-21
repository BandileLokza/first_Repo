# Generated manually for the simplified abandonment-resolution form.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0018_abandoned_match_resolution"),
    ]

    operations = [
        migrations.AlterField(
            model_name="abandonedmatchdecision",
            name="decision_reference",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
