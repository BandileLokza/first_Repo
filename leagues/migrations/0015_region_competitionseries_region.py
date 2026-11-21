from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("leagues", "0014_competition_display_order")]

    operations = [
        migrations.CreateModel(
            name="Region",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("slug", models.SlugField(unique=True)),
                ("display_order", models.PositiveIntegerField(db_index=True, default=100)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["display_order", "name", "id"]},
        ),
        migrations.AddField(
            model_name="competitionseries",
            name="region",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="league_series", to="leagues.region"),
        ),
    ]
