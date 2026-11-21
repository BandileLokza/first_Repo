from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("teams", "0004_teamcoachassignment")]

    operations = [
        migrations.AddField(
            model_name="team",
            name="home_ground",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
