from django.db import migrations


def create_site(apps, schema_editor):
    Site = apps.get_model("sites", "Site")

    Site.objects.update_or_create(
        id=1,
        defaults={
            "domain": "esportslive.site",
            "name": "EsportsLive",
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0002_alter_domain_unique"),
        ("leagues", "0008_siteidentity"),
    ]

    operations = [
        migrations.RunPython(create_site, migrations.RunPython.noop),
    ]
