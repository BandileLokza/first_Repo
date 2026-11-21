from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import (
    ActivityLog,
    LeagueMembership,
    OrganisationAdministrator,
    RegionalAdministrator,
)
from leagues.fixtures import (
    build_generation_preview,
    confirm_generation_preview,
    generate_first_round,
)
from leagues.models import Competition, CompetitionSeries, Region, Stage, Standing
from matches.models import (
    FootballKnockoutRoundRule,
    FootballKnockoutTie,
    FootballKnockoutTieSlot,
    Goal,
    Match,
    MatchAdminAssignment,
    MatchLineup,
    MatchLineupEntry,
    Player,
    YellowCard,
)
from matches.services import apply_official_match_result, rebuild_league_standings
from matches.tasks import (
    recalculate_league_after_bulk_update_task,
    recalculate_match_after_result_task,
)
from predictions.models import Prediction, PredictionMatch
from teams.models import Team


User = get_user_model()


class FixtureSafetyMixin:
    def make_competition(self, *, name, slug, league_type, region=None):
        series = CompetitionSeries.objects.create(
            name=f"{name} Series",
            slug=f"{slug}-series",
            league_type=league_type,
            region=region,
        )
        competition = Competition.objects.create(
            name=name,
            slug=slug,
            season="2026/27",
            series=series,
            format_type="TABLE",
        )
        stage = Stage.objects.create(
            competition=competition,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        teams = [
            Team.objects.create(competition=competition, name=f"{name} Team {number}")
            for number in range(1, 5)
        ]
        return competition, stage, teams

    def make_fixture(
        self,
        competition,
        stage,
        teams,
        *,
        status="UPCOMING",
        generated=False,
        leg=1,
    ):
        return Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[0],
            away_team=teams[1],
            gameweek=1,
            match_date=timezone.now() + timedelta(days=1),
            location="Old Ground",
            status=status,
            generated_by_system=generated,
            leg=leg,
        )


class DashboardSearchPermissionTests(FixtureSafetyMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="search-user", password="pass")
        self.region = Region.objects.create(name="Hhohho", slug="search-hhohho")
        self.other_region = Region.objects.create(name="Manzini", slug="search-manzini")
        self.regional, _, _ = self.make_competition(
            name="Hhohho Search League",
            slug="search-regional",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=self.region,
        )
        self.other_regional, _, _ = self.make_competition(
            name="Manzini Search League",
            slug="search-other-regional",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=self.other_region,
        )
        self.ewfa, _, _ = self.make_competition(
            name="EWFA Search League",
            slug="search-ewfa",
            league_type=CompetitionSeries.LeagueType.EWFA,
        )
        self.ple, _, _ = self.make_competition(
            name="PLE Search League",
            slug="search-ple",
            league_type=CompetitionSeries.LeagueType.PLE,
        )
        self.owned, _, _ = self.make_competition(
            name="Owned Search League",
            slug="search-owned",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        self.zonal, _, _ = self.make_competition(
            name="Zonal Search League",
            slug="search-zonal",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        RegionalAdministrator.objects.create(user=self.user, region=self.region)
        OrganisationAdministrator.objects.create(
            user=self.user,
            organisation=OrganisationAdministrator.Organisation.EWFA,
        )
        LeagueMembership.objects.create(
            user=self.user,
            competition=self.owned,
            role=LeagueMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_search_filters_only_the_existing_authorized_management_union(self):
        response = self.client.get(reverse("league_manager:dashboard") + "?q=Search")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {league.pk for league in response.context["managed_leagues"]},
            {self.regional.pk, self.ewfa.pk, self.owned.pk},
        )
        self.assertNotContains(response, self.other_regional.name)
        self.assertNotContains(response, self.ple.name)

    def test_search_deduplicates_overlapping_authority(self):
        LeagueMembership.objects.create(
            user=self.user,
            competition=self.regional,
            role=LeagueMembership.Role.ADMIN,
        )
        response = self.client.get(reverse("league_manager:dashboard") + "?q=Hhohho")
        self.assertEqual(
            [league.pk for league in response.context["managed_leagues"]],
            [self.regional.pk],
        )

    def test_each_management_scope_remains_server_side_filtered(self):
        regional_user = User.objects.create_user(username="regional-search", password="pass")
        ewfa_user = User.objects.create_user(username="ewfa-search", password="pass")
        ple_user = User.objects.create_user(username="ple-search", password="pass")
        zonal_user = User.objects.create_user(username="zonal-search", password="pass")
        owner_user = User.objects.create_user(username="owner-search", password="pass")
        superuser = User.objects.create_superuser(
            username="search-super", email="search-super@example.test", password="pass"
        )
        RegionalAdministrator.objects.create(user=regional_user, region=self.region)
        OrganisationAdministrator.objects.create(
            user=ewfa_user,
            organisation=OrganisationAdministrator.Organisation.EWFA,
        )
        OrganisationAdministrator.objects.create(
            user=ple_user,
            organisation=OrganisationAdministrator.Organisation.PLE,
        )
        OrganisationAdministrator.objects.create(
            user=zonal_user,
            organisation=OrganisationAdministrator.Organisation.ZONAL,
        )
        LeagueMembership.objects.create(
            user=owner_user,
            competition=self.owned,
            role=LeagueMembership.Role.OWNER,
        )

        cases = (
            (regional_user, {self.regional.pk}),
            (ewfa_user, {self.ewfa.pk}),
            (ple_user, {self.ple.pk}),
            (zonal_user, {self.owned.pk, self.zonal.pk}),
            (owner_user, {self.owned.pk}),
            (
                superuser,
                {
                    self.regional.pk,
                    self.other_regional.pk,
                    self.ewfa.pk,
                    self.ple.pk,
                    self.owned.pk,
                    self.zonal.pk,
                },
            ),
        )
        dashboard_url = reverse("league_manager:dashboard") + "?q=Search"
        for user, expected_ids in cases:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(dashboard_url)
                self.assertEqual(
                    {league.pk for league in response.context["managed_leagues"]},
                    expected_ids,
                )

        self.client.force_login(regional_user)
        response = self.client.get(reverse("league_manager:dashboard") + "?q=Manzini")
        self.assertNotContains(response, self.other_regional.name)
        self.assertEqual(
            self.client.get(
                reverse("league_manager:league_dashboard", args=[self.other_regional.slug])
            ).status_code,
            403,
        )


class GeneratedFixtureLockTests(FixtureSafetyMixin, TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="fixture-manager", password="pass")
        self.competition, self.stage, self.teams = self.make_competition(
            name="Generated Fixture League",
            slug="generated-fixture-league",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        LeagueMembership.objects.create(
            user=self.manager,
            competition=self.competition,
            role=LeagueMembership.Role.OWNER,
        )
        self.fixture = self.make_fixture(
            self.competition,
            self.stage,
            self.teams,
            generated=True,
        )
        self.client.force_login(self.manager)

    def test_generated_fixture_allows_schedule_changes_and_rejects_forged_structure(self):
        url = reverse(
            "league_manager:fixture_update",
            args=[self.competition.slug, self.fixture.id],
        )
        new_time = timezone.localtime(timezone.now() + timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        response = self.client.post(
            url,
            {
                "match_date": new_time,
                "location": "New Ground",
            },
        )
        self.assertRedirects(
            response,
            reverse("league_manager:fixture_list", args=[self.competition.slug]),
        )
        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.home_team_id, self.teams[0].id)
        self.assertEqual(self.fixture.away_team_id, self.teams[1].id)
        self.assertEqual(self.fixture.gameweek, 1)
        self.assertEqual(self.fixture.leg, 1)
        self.assertEqual(self.fixture.location, "New Ground")

        forged_response = self.client.post(
            url,
            {
                "stage": self.stage.id,
                "home_team": self.teams[2].id,
                "away_team": self.teams[3].id,
                "gameweek": 9,
                "match_date": new_time,
                "location": "Forged Ground",
                "leg": 2,
            },
        )
        self.assertEqual(forged_response.status_code, 200)
        self.assertContains(forged_response, "locked and cannot be changed")
        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.location, "New Ground")

    def test_model_and_bulk_paths_reject_generated_structure_rewrites(self):
        self.fixture.home_team = self.teams[2]
        with self.assertRaises(ValidationError):
            self.fixture.save()
        # The bracket service's private rewrite flag is not a general escape
        # hatch: non-bracket fixtures still reject it.
        with self.assertRaises(ValidationError):
            self.fixture.save(_allow_generated_bracket_participant_rewrite=True)
        with self.assertRaises(ValidationError):
            Match.objects.filter(pk=self.fixture.pk).update(gameweek=2)
        self.fixture.refresh_from_db()
        self.fixture.gameweek = 2
        with self.assertRaises(ValidationError):
            Match.objects.bulk_update([self.fixture], ["gameweek"])

    def test_manual_fixture_structure_remains_editable(self):
        manual = self.make_fixture(
            self.competition,
            self.stage,
            self.teams,
            generated=False,
            leg=2,
        )
        manual.home_team = self.teams[2]
        manual.away_team = self.teams[3]
        manual.gameweek = 2
        manual.save()
        manual.refresh_from_db()
        self.assertEqual(manual.home_team_id, self.teams[2].id)
        self.assertEqual(manual.away_team_id, self.teams[3].id)
        self.assertEqual(manual.gameweek, 2)
        manual.gameweek = 3
        Match.objects.bulk_update([manual], ["gameweek"])
        manual.refresh_from_db()
        self.assertEqual(manual.gameweek, 3)

    def test_admin_marks_generated_structure_read_only_and_disables_direct_delete(self):
        from matches.admin import MatchAdmin

        match_admin = MatchAdmin(Match, AdminSite())
        readonly = match_admin.get_readonly_fields(RequestFactory().get("/"), self.fixture)
        self.assertTrue({"home_team", "away_team", "gameweek", "leg"}.issubset(readonly))
        self.assertFalse(match_admin.has_delete_permission(RequestFactory().get("/"), self.fixture))

    def test_fixture_table_uses_the_shared_theme_aware_class(self):
        response = self.client.get(
            reverse("league_manager:fixture_list", args=[self.competition.slug])
        )
        self.assertContains(response, "league-manager-table")
        self.assertContains(response, "badge bg-info text-dark")
        stylesheet = Path(settings.BASE_DIR, "static", "css", "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("html.dark-theme .league-manager-table", stylesheet)
        self.assertIn(".league-manager-table .text-muted", stylesheet)
        self.assertIn("--bs-table-bg: #ffffff", stylesheet)

    def test_generator_marks_new_fixtures_without_reclassifying_manual_rows(self):
        generated_competition, _, _ = self.make_competition(
            name="Automatic Lock League",
            slug="automatic-lock-league",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        generate_first_round(generated_competition)
        self.assertTrue(
            Match.objects.filter(competition=generated_competition).exists()
        )
        self.assertFalse(
            Match.objects.filter(
                competition=generated_competition,
                generated_by_system=False,
            ).exists()
        )
        self.assertTrue(self.fixture.generated_by_system)


class FixtureDeletionTests(FixtureSafetyMixin, TestCase):
    def setUp(self):
        self.region = Region.objects.create(name="Lubombo", slug="delete-lubombo")
        self.regional_admin = User.objects.create_user(username="regional-delete", password="pass")
        self.other_regional_admin = User.objects.create_user(username="other-regional-delete", password="pass")
        self.owner = User.objects.create_user(username="owner-delete", password="pass")
        self.league_admin = User.objects.create_user(username="league-admin-delete", password="pass")
        self.agent = User.objects.create_user(username="agent-delete", password="pass")
        self.organisation_admin = User.objects.create_user(username="organisation-delete", password="pass")
        self.superuser = User.objects.create_superuser(
            username="super-delete", email="super@example.test", password="pass"
        )
        self.regional_league, self.regional_stage, self.regional_teams = self.make_competition(
            name="Lubombo Delete League",
            slug="lubombo-delete-league",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=self.region,
        )
        self.ple_league, self.ple_stage, self.ple_teams = self.make_competition(
            name="PLE Delete League",
            slug="ple-delete-league",
            league_type=CompetitionSeries.LeagueType.PLE,
        )
        self.ewfa_league, self.ewfa_stage, self.ewfa_teams = self.make_competition(
            name="EWFA Delete League",
            slug="ewfa-delete-league",
            league_type=CompetitionSeries.LeagueType.EWFA,
        )
        self.zonal_league, self.zonal_stage, self.zonal_teams = self.make_competition(
            name="Zonal Delete League",
            slug="zonal-delete-league",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        RegionalAdministrator.objects.create(user=self.regional_admin, region=self.region)
        other_region = Region.objects.create(name="Manzini", slug="delete-manzini")
        RegionalAdministrator.objects.create(
            user=self.other_regional_admin,
            region=other_region,
        )
        (
            self.other_regional_league,
            self.other_regional_stage,
            self.other_regional_teams,
        ) = self.make_competition(
            name="Manzini Delete League",
            slug="manzini-delete-league",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=other_region,
        )
        OrganisationAdministrator.objects.create(
            user=self.organisation_admin,
            organisation=OrganisationAdministrator.Organisation.PLE,
        )
        LeagueMembership.objects.create(
            user=self.owner,
            competition=self.regional_league,
            role=LeagueMembership.Role.OWNER,
        )
        LeagueMembership.objects.create(
            user=self.league_admin,
            competition=self.regional_league,
            role=LeagueMembership.Role.ADMIN,
        )
        LeagueMembership.objects.create(
            user=self.agent,
            competition=self.regional_league,
            role=LeagueMembership.Role.AGENT,
        )
        self.fixture = self.make_fixture(
            self.regional_league,
            self.regional_stage,
            self.regional_teams,
            status="FINISHED",
            generated=True,
        )

    def delete_url(self, league, fixture):
        return reverse("league_manager:fixture_delete", args=[league.slug, fixture.id])

    def test_only_superuser_or_correct_regional_administrator_can_open_confirmation(self):
        url = self.delete_url(self.regional_league, self.fixture)
        for user in (
            self.owner,
            self.league_admin,
            self.agent,
            self.organisation_admin,
            self.other_regional_admin,
        ):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.regional_admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Match.objects.filter(pk=self.fixture.pk).exists())
        self.assertContains(response, "Delete Fixture")
        self.assertContains(response, "Status: Finished")

        for league, stage, teams in (
            (self.ple_league, self.ple_stage, self.ple_teams),
            (self.ewfa_league, self.ewfa_stage, self.ewfa_teams),
            (self.zonal_league, self.zonal_stage, self.zonal_teams),
        ):
            fixture = self.make_fixture(league, stage, teams)
            with self.subTest(league_type=league.series.league_type):
                self.assertEqual(self.client.get(self.delete_url(league, fixture)).status_code, 403)
                self.assertEqual(self.client.post(self.delete_url(league, fixture)).status_code, 403)

        cross_region_fixture = self.make_fixture(
            self.other_regional_league,
            self.other_regional_stage,
            self.other_regional_teams,
        )
        self.assertEqual(
            self.client.get(
                self.delete_url(self.other_regional_league, cross_region_fixture)
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                self.delete_url(self.other_regional_league, cross_region_fixture)
            ).status_code,
            403,
        )

        self.regional_league.is_archived = True
        self.regional_league.save(update_fields=["is_archived"])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_post_deletes_all_related_match_data_and_keeps_durable_audit_snapshot(self):
        player = Player.objects.create(
            name="Deleted Match Player",
            team=self.regional_teams[0],
        )
        goal = Goal.objects.create(
            match=self.fixture,
            team=self.regional_teams[0],
            player=player,
        )
        card = YellowCard.objects.create(
            match=self.fixture,
            team=self.regional_teams[0],
            player=player,
        )
        assignment = MatchAdminAssignment.objects.create(
            match=self.fixture,
            admin=self.agent,
        )
        lineup = MatchLineup.objects.create(
            match=self.fixture,
            team=self.regional_teams[0],
        )
        appearance = MatchLineupEntry.objects.create(
            lineup=lineup,
            player=player,
            role=MatchLineupEntry.Role.STARTER,
        )
        prediction_match, _ = PredictionMatch.objects.get_or_create(
            match=self.fixture,
            defaults={"competition": self.regional_league},
        )
        prediction = Prediction.objects.create(
            user=self.owner,
            prediction_match=prediction_match,
            predicted_result="H",
            predicted_home_goals=1,
            predicted_away_goals=0,
        )
        survivor = Match.objects.create(
            competition=self.regional_league,
            stage=self.regional_stage,
            home_team=self.regional_teams[2],
            away_team=self.regional_teams[3],
            gameweek=1,
            leg=1,
            status="FINISHED",
            home_score=2,
            away_score=1,
            match_date=timezone.now() - timedelta(days=1),
        )
        second_survivor = Match.objects.create(
            competition=self.regional_league,
            stage=self.regional_stage,
            home_team=self.regional_teams[0],
            away_team=self.regional_teams[2],
            gameweek=2,
            leg=1,
            status="FINISHED",
            home_score=1,
            away_score=0,
            match_date=timezone.now() - timedelta(days=1),
        )
        rebuild_league_standings(self.regional_league)
        self.client.force_login(self.regional_admin)
        response = self.client.post(self.delete_url(self.regional_league, self.fixture))
        self.assertRedirects(
            response,
            reverse("league_manager:fixture_list", args=[self.regional_league.slug]),
        )
        self.assertFalse(Match.objects.filter(pk=self.fixture.pk).exists())
        self.assertFalse(Goal.objects.filter(pk=goal.pk).exists())
        self.assertFalse(YellowCard.objects.filter(pk=card.pk).exists())
        self.assertFalse(MatchAdminAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertFalse(MatchLineup.objects.filter(pk=lineup.pk).exists())
        self.assertFalse(MatchLineupEntry.objects.filter(pk=appearance.pk).exists())
        self.assertFalse(PredictionMatch.objects.filter(pk=prediction_match.pk).exists())
        self.assertFalse(Prediction.objects.filter(pk=prediction.pk).exists())
        self.assertEqual(
            Standing.objects.get(
                competition=self.regional_league,
                stage=self.regional_stage,
                team=self.regional_teams[0],
            ).played,
            1,
        )
        self.assertEqual(
            Standing.objects.get(
                competition=self.regional_league,
                stage=self.regional_stage,
                team=self.regional_teams[2],
            ).played,
            2,
        )
        self.assertTrue(Match.objects.filter(pk=survivor.pk).exists())
        self.assertTrue(Match.objects.filter(pk=second_survivor.pk).exists())
        self.assertEqual(
            Standing.objects.get(
                competition=self.regional_league,
                stage=self.regional_stage,
                team=self.regional_teams[2],
            ).played,
            2,
        )
        audit = ActivityLog.objects.get(action=ActivityLog.Action.FIXTURE_DELETED)
        self.assertIsNone(audit.match_id)
        self.assertEqual(audit.actor_id, self.regional_admin.id)
        self.assertEqual(audit.competition_id, self.regional_league.id)
        self.assertEqual(audit.old_values["match_id"], self.fixture.id)
        self.assertEqual(audit.old_values["home_team"], self.regional_teams[0].name)
        self.assertEqual(audit.old_values["region"], self.region.name)
        self.assertTrue(audit.old_values["generated_by_system"])
        self.assertFalse(
            Match.objects.filter(
                competition=self.regional_league,
                generated_by_system=True,
            ).exists()
        )

        # Both existing asynchronous follow-up task shapes now treat a
        # concurrently deleted fixture as a completed no-op.
        self.assertIsNone(recalculate_match_after_result_task.run(self.fixture.id))
        self.assertIsNone(
            recalculate_league_after_bulk_update_task.run(
                self.regional_league.id,
                [self.fixture.id],
            )
        )

    def test_post_requires_csrf_and_superuser_can_delete_any_status(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.regional_admin)
        self.assertEqual(
            csrf_client.post(self.delete_url(self.regional_league, self.fixture)).status_code,
            403,
        )
        self.assertTrue(Match.objects.filter(pk=self.fixture.pk).exists())

        self.client.force_login(self.superuser)
        for leg, status in enumerate(("UPCOMING", "LIVE", "ABANDONED"), start=1):
            fixture = self.make_fixture(
                self.ple_league,
                self.ple_stage,
                self.ple_teams,
                status=status,
                leg=leg,
            )
            response = self.client.post(self.delete_url(self.ple_league, fixture))
            self.assertEqual(response.status_code, 302)
            self.assertFalse(Match.objects.filter(pk=fixture.pk).exists())

    def test_connected_knockout_bracket_fixture_cannot_be_deleted(self):
        knockout_stage = Stage.objects.create(
            competition=self.regional_league,
            name="Knockout stage",
            stage_type="KNOCKOUT",
            order=2,
        )
        tie = FootballKnockoutTie.objects.create(
            stage=knockout_stage,
            position=1,
            team_one=self.regional_teams[0],
            team_two=self.regional_teams[1],
        )
        fixture = Match.objects.create(
            competition=self.regional_league,
            stage=knockout_stage,
            home_team=self.regional_teams[0],
            away_team=self.regional_teams[1],
            knockout_tie=tie,
            status="UPCOMING",
            generated_by_system=True,
            match_date=timezone.now() + timedelta(days=1),
        )
        self.client.force_login(self.regional_admin)
        url = self.delete_url(self.regional_league, fixture)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted individually")
        self.assertNotContains(response, ">Delete Fixture</button>")

        response = self.client.post(url)
        self.assertRedirects(
            response,
            reverse("league_manager:fixture_list", args=[self.regional_league.slug]),
        )
        self.assertTrue(Match.objects.filter(pk=fixture.pk).exists())
        self.assertFalse(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.FIXTURE_DELETED,
                old_values__match_id=fixture.id,
            ).exists()
        )


class ScheduleLockTests(FixtureSafetyMixin, TestCase):
    def setUp(self):
        self.region = Region.objects.create(name="Schedule Lock Region", slug="schedule-lock-region")
        self.superuser = User.objects.create_superuser(
            username="schedule-super", email="schedule-super@example.test", password="pass"
        )
        self.regional_admin = User.objects.create_user(username="schedule-regional", password="pass")
        self.owner = User.objects.create_user(username="schedule-owner", password="pass")
        self.league_admin = User.objects.create_user(username="schedule-admin", password="pass")
        self.agent = User.objects.create_user(username="schedule-agent", password="pass")
        self.organisation_admin = User.objects.create_user(username="schedule-org", password="pass")
        self.competition, self.stage, self.teams = self.make_competition(
            name="Schedule Lock League",
            slug="schedule-lock-league",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=self.region,
        )
        RegionalAdministrator.objects.create(user=self.regional_admin, region=self.region)
        OrganisationAdministrator.objects.create(
            user=self.organisation_admin,
            organisation=OrganisationAdministrator.Organisation.PLE,
        )
        for user, role in (
            (self.owner, LeagueMembership.Role.OWNER),
            (self.league_admin, LeagueMembership.Role.ADMIN),
            (self.agent, LeagueMembership.Role.AGENT),
        ):
            LeagueMembership.objects.create(user=user, competition=self.competition, role=role)
        self.fixture = self.make_fixture(
            self.competition,
            self.stage,
            self.teams,
            generated=False,
        )
        self.generated_fixture = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.teams[0],
            away_team=self.teams[1],
            gameweek=2,
            match_date=timezone.now() + timedelta(days=2),
            location="Generated Ground",
            status="UPCOMING",
            generated_by_system=True,
            leg=2,
        )

    def lock_url(self):
        return reverse("league_manager:schedule_lock", args=[self.competition.slug])

    def unlock_url(self):
        return reverse("league_manager:schedule_unlock", args=[self.competition.slug])

    def fixture_list_url(self):
        return reverse("league_manager:fixture_list", args=[self.competition.slug])

    def fixture_url(self, fixture=None):
        fixture = fixture or self.fixture
        return reverse("league_manager:fixture_update", args=[self.competition.slug, fixture.id])

    def delete_url(self, fixture=None):
        fixture = fixture or self.fixture
        return reverse("league_manager:fixture_delete", args=[self.competition.slug, fixture.id])

    def lock_directly(self):
        self.competition.refresh_from_db()
        self.competition.schedule_locked_at = timezone.now()
        self.competition.schedule_locked_by = self.superuser
        self.competition.save(update_fields=["schedule_locked_at", "schedule_locked_by"])

    def test_only_superusers_can_lock_unlock_and_the_actions_are_audited(self):
        for user in (
            self.regional_admin,
            self.organisation_admin,
            self.owner,
            self.league_admin,
            self.agent,
        ):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                self.assertEqual(self.client.get(self.lock_url()).status_code, 403)
                self.assertEqual(self.client.post(self.lock_url()).status_code, 403)
                self.assertEqual(self.client.get(self.unlock_url()).status_code, 403)

        self.client.logout()
        self.assertEqual(self.client.get(self.lock_url()).status_code, 302)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.superuser)
        self.assertEqual(csrf_client.post(self.lock_url()).status_code, 403)

        self.client.force_login(self.superuser)
        response = self.client.get(self.lock_url())
        self.assertEqual(response.status_code, 200)
        self.competition.refresh_from_db()
        self.assertIsNone(self.competition.schedule_locked_at)
        self.assertContains(response, "Schedule Lock League")
        self.assertContains(response, "2 fixtures")

        response = self.client.post(self.lock_url())
        self.assertRedirects(response, self.fixture_list_url())
        self.competition.refresh_from_db()
        self.assertTrue(self.competition.schedule_is_locked)
        self.assertEqual(self.competition.schedule_locked_by_id, self.superuser.id)
        locked_audit = ActivityLog.objects.get(action=ActivityLog.Action.SCHEDULE_LOCKED)
        self.assertEqual(locked_audit.actor_id, self.superuser.id)
        self.assertFalse(locked_audit.old_values["schedule_locked"])
        self.assertTrue(locked_audit.new_values["schedule_locked"])
        self.assertEqual(locked_audit.new_values["fixture_count"], 2)

        response = self.client.post(self.unlock_url(), {"reason": "Correct a published pairing."})
        self.assertRedirects(
            response,
            reverse("league_manager:fixture_list", args=[self.competition.slug]),
        )
        self.competition.refresh_from_db()
        self.assertFalse(self.competition.schedule_is_locked)
        unlocked_audit = ActivityLog.objects.get(action=ActivityLog.Action.SCHEDULE_UNLOCKED)
        self.assertEqual(unlocked_audit.new_values["unlock_reason"], "Correct a published pairing.")
        self.assertTrue(unlocked_audit.old_values["schedule_locked"])
        self.assertFalse(unlocked_audit.new_values["schedule_locked"])

    def test_locked_schedule_blocks_structure_but_keeps_date_location_and_lifecycle_available(self):
        self.lock_directly()
        self.client.force_login(self.owner)
        fixture_count = Match.objects.filter(competition=self.competition).count()
        response = self.client.post(
            reverse("league_manager:fixture_create", args=[self.competition.slug]),
        )
        self.assertRedirects(response, self.fixture_list_url())
        self.assertEqual(
            Match.objects.filter(competition=self.competition).count(),
            fixture_count,
        )
        new_time = timezone.localtime(timezone.now() + timedelta(days=4)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        response = self.client.post(
            self.fixture_url(),
            {"match_date": new_time, "location": "Rescheduled Ground"},
        )
        self.assertRedirects(response, self.fixture_list_url())
        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.location, "Rescheduled Ground")
        self.assertEqual(self.fixture.home_team_id, self.teams[0].id)

        forged = self.client.post(
            self.fixture_url(),
            {
                "stage": self.stage.id,
                "home_team": self.teams[2].id,
                "away_team": self.teams[3].id,
                "gameweek": 8,
                "leg": 3,
                "match_date": new_time,
                "location": "Forged Ground",
            },
        )
        self.assertEqual(forged.status_code, 200)
        self.assertContains(forged, "published schedule locks this field")
        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.location, "Rescheduled Ground")
        self.assertEqual(self.fixture.gameweek, 1)

        # Result/lifecycle fields are intentionally outside the schedule lock.
        self.fixture.status = "LIVE"
        self.fixture.phase = "FIRST_HALF"
        self.fixture.save(update_fields=["status", "phase"])
        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.status, "LIVE")

    def test_locked_schedule_blocks_model_query_and_bulk_structure_bypasses(self):
        self.lock_directly()
        self.fixture.home_team = self.teams[2]
        with self.assertRaises(ValidationError):
            self.fixture.save()
        with self.assertRaises(ValidationError):
            Match.objects.filter(pk=self.fixture.pk).update(gameweek=2)
        self.fixture.refresh_from_db()
        self.fixture.gameweek = 2
        with self.assertRaises(ValidationError):
            Match.objects.bulk_update([self.fixture], ["gameweek"])
        with self.assertRaises(ValidationError):
            Match.objects.bulk_create([
                Match(
                    competition=self.competition,
                    stage=self.stage,
                    home_team=self.teams[2],
                    away_team=self.teams[3],
                    gameweek=3,
                    leg=3,
                )
            ])
        with self.assertRaises(ValidationError):
            Match.objects.create(
                competition=self.competition,
                stage=self.stage,
                home_team=self.teams[2],
                away_team=self.teams[3],
                gameweek=3,
                leg=3,
            )

    def test_unlock_restores_manual_structure_but_not_generated_fixture_protection(self):
        self.lock_directly()
        self.competition.schedule_locked_at = None
        self.competition.schedule_locked_by = None
        self.competition.save(update_fields=["schedule_locked_at", "schedule_locked_by"])

        self.fixture.home_team = self.teams[2]
        self.fixture.away_team = self.teams[3]
        self.fixture.gameweek = 4
        self.fixture.save()
        self.generated_fixture.home_team = self.teams[2]
        with self.assertRaises(ValidationError):
            self.generated_fixture.save()

    def test_generation_and_stale_preview_are_rejected_after_locking(self):
        generation_competition, _, _ = self.make_competition(
            name="Locked Generation League",
            slug="locked-generation-league",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        preview = build_generation_preview(generation_competition, "first")
        generation_competition.schedule_locked_at = timezone.now()
        generation_competition.schedule_locked_by = self.superuser
        generation_competition.save(update_fields=["schedule_locked_at", "schedule_locked_by"])
        with self.assertRaises(ValidationError):
            confirm_generation_preview(generation_competition, preview["token"], action="first")
        with self.assertRaises(ValidationError):
            generate_first_round(generation_competition)
        self.assertFalse(Match.objects.filter(competition=generation_competition).exists())

    def test_admin_and_trusted_bracket_progression_respect_schedule_lock_boundaries(self):
        from matches.admin import MatchAdmin

        knockout_stage = Stage.objects.create(
            competition=self.competition,
            name="Locked Knockout",
            stage_type="KNOCKOUT",
            order=2,
        )
        tie = FootballKnockoutTie.objects.create(
            stage=knockout_stage,
            position=1,
            team_one=self.teams[0],
            team_two=self.teams[1],
        )
        self.lock_directly()
        readonly = MatchAdmin(Match, AdminSite()).get_readonly_fields(
            RequestFactory().get("/"), self.fixture
        )
        self.assertTrue({"home_team", "away_team", "gameweek", "leg"}.issubset(readonly))

        trusted = Match(
            competition=self.competition,
            stage=knockout_stage,
            home_team=self.teams[0],
            away_team=self.teams[1],
            knockout_tie=tie,
            leg=1,
            match_date=timezone.now() + timedelta(days=1),
            status="UPCOMING",
            phase="NONE",
            generated_by_system=True,
        )
        trusted.save(_allow_schedule_locked_bracket_creation=True)
        self.assertTrue(Match.objects.filter(pk=trusted.pk).exists())

        untrusted = Match(
            competition=self.competition,
            stage=knockout_stage,
            home_team=self.teams[0],
            away_team=self.teams[1],
            knockout_tie=tie,
            leg=2,
            match_date=timezone.now() + timedelta(days=2),
            status="UPCOMING",
            phase="NONE",
            generated_by_system=True,
        )
        with self.assertRaises(ValidationError):
            untrusted.save()

    def test_locked_bracket_rejects_management_changes_but_advances_a_completed_tie(self):
        self.competition.format_type = "KNOCKOUT"
        self.competition.save(update_fields=["format_type"])
        source_stage = Stage.objects.create(
            competition=self.competition,
            name="Locked Semi-final",
            stage_type="KNOCKOUT",
            order=2,
        )
        destination_stage = Stage.objects.create(
            competition=self.competition,
            name="Locked Final",
            stage_type="KNOCKOUT",
            order=3,
        )
        FootballKnockoutRoundRule.objects.create(stage=source_stage)
        FootballKnockoutRoundRule.objects.create(stage=destination_stage)
        destination = FootballKnockoutTie.objects.create(
            stage=destination_stage,
            position=1,
        )
        destination_slot = FootballKnockoutTieSlot.objects.create(
            tie=destination,
            slot=FootballKnockoutTieSlot.Slot.HOME,
        )
        FootballKnockoutTieSlot.objects.create(
            tie=destination,
            slot=FootballKnockoutTieSlot.Slot.AWAY,
        )
        source = FootballKnockoutTie.objects.create(
            stage=source_stage,
            position=1,
            team_one=self.teams[0],
            team_two=self.teams[1],
            winner_destination=destination,
            winner_destination_slot=FootballKnockoutTie.DestinationSlot.HOME,
        )
        source_match = Match.objects.create(
            competition=self.competition,
            stage=source_stage,
            home_team=self.teams[0],
            away_team=self.teams[1],
            knockout_tie=source,
            match_date=timezone.now() + timedelta(days=1),
            generated_by_system=True,
        )

        self.lock_directly()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("league_manager:knockout_bracket", args=[self.competition.slug]),
            {"action": "seed_random"},
        )
        self.assertRedirects(
            response,
            reverse("league_manager:knockout_bracket", args=[self.competition.slug]),
        )
        destination_slot.team = self.teams[2]
        with self.assertRaises(ValidationError):
            destination_slot.save()
        with self.assertRaises(ValidationError):
            FootballKnockoutTie.objects.filter(pk=source.pk).update(
                team_one=self.teams[2]
            )
        with self.assertRaises(ValidationError):
            FootballKnockoutRoundRule.objects.filter(stage=source_stage).update(
                number_of_legs=2
            )

        apply_official_match_result(
            source_match,
            home_score=1,
            away_score=0,
            regulation_home_score=1,
            regulation_away_score=0,
        )
        source.refresh_from_db()
        destination_slot.refresh_from_db()
        destination.refresh_from_db()
        self.assertEqual(source.winner_id, self.teams[0].id)
        self.assertEqual(destination_slot.team_id, self.teams[0].id)
        self.assertEqual(destination.team_one_id, self.teams[0].id)

    def test_authorized_locked_schedule_deletion_remains_available_and_is_audited(self):
        upcoming = self.fixture
        live = self.make_fixture(
            self.competition, self.stage, self.teams, status="LIVE", leg=3
        )
        finished = self.make_fixture(
            self.competition, self.stage, self.teams, status="FINISHED", leg=4
        )
        self.lock_directly()

        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.delete_url(upcoming)).status_code, 403)

        self.client.force_login(self.regional_admin)
        response = self.client.post(self.delete_url(upcoming))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Match.objects.filter(pk=upcoming.pk).exists())
        self.competition.refresh_from_db()
        self.assertTrue(self.competition.schedule_is_locked)
        audit = ActivityLog.objects.get(
            action=ActivityLog.Action.FIXTURE_DELETED,
            old_values__match_id=upcoming.id,
        )
        self.assertTrue(audit.old_values["schedule_locked"])
        self.assertEqual(audit.old_values["status"], "UPCOMING")

        self.client.force_login(self.superuser)
        for fixture in (live, finished):
            response = self.client.post(self.delete_url(fixture))
            self.assertEqual(response.status_code, 302)
            self.assertFalse(Match.objects.filter(pk=fixture.pk).exists())
