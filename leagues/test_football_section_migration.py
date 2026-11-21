"""Regression test for the safe regional/zonal data-classification migration."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class FootballSectionMigrationTests(TransactionTestCase):
    migrate_from = ("leagues", "0019_alter_competition_sponsor_logo_and_more")
    migrate_to = ("leagues", "0020_alter_competitionseries_league_type")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.executor = MigrationExecutor(connection)
        cls.executor.migrate([cls.migrate_from])
        old_apps = cls.executor.loader.project_state([cls.migrate_from]).apps
        Region = old_apps.get_model("leagues", "Region")
        CompetitionSeries = old_apps.get_model("leagues", "CompetitionSeries")
        region = Region.objects.create(name="Existing Region", slug="existing-region")
        cls.regional_id = CompetitionSeries.objects.create(
            name="Existing Regional League",
            slug="existing-regional-league",
            region=region,
            # Deliberately stale legacy classifications verify that the
            # established region association, not old values, is authoritative.
            league_type="ZONAL",
        ).pk
        cls.zonal_id = CompetitionSeries.objects.create(
            name="Existing Independent League",
            slug="existing-independent-league",
            league_type="REGIONAL",
        ).pk
        cls.executor = MigrationExecutor(connection)
        cls.executor.migrate([cls.migrate_to])
        cls.apps = cls.executor.loader.project_state([cls.migrate_to]).apps

    @classmethod
    def tearDownClass(cls):
        # Restore the full project migration state for the rest of the suite.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDownClass()

    def test_existing_region_linked_and_independent_series_keep_correct_sections(self):
        CompetitionSeries = self.apps.get_model("leagues", "CompetitionSeries")
        self.assertEqual(
            CompetitionSeries.objects.get(pk=self.regional_id).league_type,
            "REGIONAL",
        )
        self.assertEqual(
            CompetitionSeries.objects.get(pk=self.zonal_id).league_type,
            "ZONAL",
        )
