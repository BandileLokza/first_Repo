# Generated manually for the Phase Two connected-bracket schedule fields.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0011_footballknockouttieslot_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="footballknockouttie",
            name="leg_one_scheduled_for",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="footballknockouttie",
            name="leg_two_scheduled_for",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
