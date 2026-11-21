"""Focused regression coverage for safe football league seasons."""

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from allauth.socialaccount.models import SocialApp

from league_manager.models import LeagueMembership
from leagues.models import Competition, CompetitionTieBreaker, Stage, Standing
from leagues.seasons import (
    archive_competition_season,
    create_next_competition_season,
)
from leagues.services import replace_competition_tie_breakers
from matches.models import Goal, Match, YellowCard
from matches.models import Player
from teams.models import Team


User = get_user_model()


class FootballSeasonServiceTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="season-superuser",
            email="season-superuser@example.com",
            password="pass",
        )

    def make_table_competition(self, *, name="Archive League", slug="archive-league", season="2026"):
        competition = Competition.objects.create(
            name=name,
            slug=slug,
            season=season,
            format_type="TABLE",
            predictions_enabled=True,
        )
        stage = Stage.objects.create(
            competition=competition,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        home_team = Team.objects.create(
            name="Home FC",
            home_ground="Home Ground",
            competition=competition,
        )
        away_team = Team.objects.create(name="Away FC", competition=competition)
        home_player = Player.objects.create(
            name="Home Player",
            team=home_team,
            jersey_number=9,
            starting_goals=6,
        )
        away_player = Player.objects.create(
            name="Away Player",
            team=away_team,
            jersey_number=10,
            starting_goals=2,
        )
        return competition, stage, home_team, away_team, home_player, away_player

    def add_finished_history(self, competition, stage, home_team, away_team, home_player, away_player):
        match = Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=home_team,
            away_team=away_team,
            match_date=timezone.now(),
            status="LIVE",
            phase="SECOND_HALF",
            phase_start_time=timezone.now(),
            home_score=1,
            away_score=0,
        )
        goal = Goal.objects.create(
            match=match,
            team=home_team,
            player=home_player,
            minute=65,
        )
        card = YellowCard.objects.create(
            match=match,
            team=away_team,
            player=away_player,
            minute=67,
        )
        match.status = "FINISHED"
        match.phase = "NONE"
        match.phase_start_time = None
        match.save(update_fields=["status", "phase", "phase_start_time"])

        home_standing = Standing.objects.get(
            competition=competition,
            stage=stage,
            team=home_team,
        )
        home_standing.played = 1
        home_standing.won = 1
        home_standing.goals_for = 1
        home_standing.points = 3
        home_standing.save()
        return match, goal, card, home_standing

    def test_archive_table_league_changes_only_archive_metadata(self):
        (
            competition,
            stage,
            home_team,
            away_team,
            home_player,
            away_player,
        ) = self.make_table_competition()
        match, goal, card, standing = self.add_finished_history(
            competition,
            stage,
            home_team,
            away_team,
            home_player,
            away_player,
        )
        original_values = {
            "match": (match.status, match.home_score, match.away_score),
            "standing": (
                standing.played,
                standing.won,
                standing.goals_for,
                standing.points,
            ),
            "player": home_player.starting_goals,
            "goal_count": Goal.objects.filter(match__competition=competition).count(),
            "card_count": YellowCard.objects.filter(match__competition=competition).count(),
        }

        archived = archive_competition_season(competition, actor=self.superuser)

        archived.refresh_from_db()
        match.refresh_from_db()
        standing.refresh_from_db()
        home_player.refresh_from_db()
        self.assertTrue(archived.is_archived)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(archived.archived_by, self.superuser)
        self.assertEqual(
            (match.status, match.home_score, match.away_score),
            original_values["match"],
        )
        self.assertEqual(
            (standing.played, standing.won, standing.goals_for, standing.points),
            original_values["standing"],
        )
        self.assertEqual(home_player.starting_goals, original_values["player"])
        self.assertEqual(
            Goal.objects.filter(match__competition=competition).count(),
            original_values["goal_count"],
        )
        self.assertEqual(
            YellowCard.objects.filter(match__competition=competition).count(),
            original_values["card_count"],
        )
        self.assertEqual(goal.match_id, match.id)
        self.assertEqual(card.match_id, match.id)

    def test_archive_rejects_non_superuser_live_upcoming_and_non_table(self):
        competition, stage, home_team, away_team, *_ = self.make_table_competition()
        regular_user = User.objects.create_user(username="regular", password="pass")
        with self.assertRaises(PermissionDenied):
            archive_competition_season(competition, actor=regular_user)

        Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=home_team,
            away_team=away_team,
            match_date=timezone.now(),
            status="UPCOMING",
        )
        with self.assertRaisesMessage(ValidationError, "live, upcoming, or unresolved abandoned"):
            archive_competition_season(competition, actor=self.superuser)

        group_competition = Competition.objects.create(
            name="Groups League",
            slug="groups-league",
            season="2026",
            format_type="GROUP_KNOCKOUT",
        )
        with self.assertRaisesMessage(ValidationError, "Only Table Only"):
            archive_competition_season(group_competition, actor=self.superuser)

    def test_new_season_clones_safe_identity_and_configuration_only(self):
        (
            competition,
            stage,
            home_team,
            away_team,
            home_player,
            away_player,
        ) = self.make_table_competition()
        self.add_finished_history(
            competition,
            stage,
            home_team,
            away_team,
            home_player,
            away_player,
        )
        replace_competition_tie_breakers(
            competition,
            [
                (1, CompetitionTieBreaker.Criterion.POINTS),
                (2, CompetitionTieBreaker.Criterion.WINS),
            ],
        )
        owner = User.objects.create_user(username="owner", password="pass")
        administrator = User.objects.create_user(username="administrator", password="pass")
        agent = User.objects.create_user(username="agent", password="pass")
        LeagueMembership.objects.create(
            user=owner,
            competition=competition,
            role=LeagueMembership.Role.OWNER,
        )
        LeagueMembership.objects.create(
            user=administrator,
            competition=competition,
            role=LeagueMembership.Role.ADMIN,
        )
        LeagueMembership.objects.create(
            user=agent,
            competition=competition,
            role=LeagueMembership.Role.AGENT,
        )
        archive_competition_season(competition, actor=self.superuser)

        next_competition = create_next_competition_season(
            competition,
            actor=self.superuser,
            season="2027",
            slug="archive-league-2027",
        )

        self.assertEqual(next_competition.series_id, competition.series_id)
        self.assertEqual(next_competition.format_type, "TABLE")
        self.assertTrue(next_competition.predictions_enabled)
        self.assertFalse(next_competition.is_archived)
        self.assertEqual(next_competition.matches.count(), 0)
        self.assertFalse(Goal.objects.filter(match__competition=next_competition).exists())
        self.assertFalse(
            YellowCard.objects.filter(match__competition=next_competition).exists()
        )

        source_teams = {team.name: team for team in competition.teams.all()}
        next_teams = {team.name: team for team in next_competition.teams.all()}
        self.assertEqual(set(next_teams), set(source_teams))
        for name, next_team in next_teams.items():
            self.assertNotEqual(next_team.pk, source_teams[name].pk)

        next_players = list(
            Player.objects.filter(team__competition=next_competition).select_related("team")
        )
        self.assertEqual(len(next_players), 2)
        self.assertTrue(all(player.starting_goals == 0 for player in next_players))
        self.assertTrue(
            all(player.team_id in {team.id for team in next_teams.values()} for player in next_players)
        )

        next_standings = Standing.objects.filter(competition=next_competition)
        self.assertEqual(next_standings.count(), 2)
        for standing in next_standings:
            self.assertEqual(
                (
                    standing.played,
                    standing.won,
                    standing.drawn,
                    standing.lost,
                    standing.goals_for,
                    standing.goals_against,
                    standing.points,
                    standing.baseline_played,
                    standing.baseline_points,
                ),
                (0, 0, 0, 0, 0, 0, 0, 0, 0),
            )

        self.assertEqual(
            list(
                next_competition.tie_breakers.order_by("order").values_list(
                    "order", "criterion"
                )
            ),
            [
                (1, CompetitionTieBreaker.Criterion.POINTS),
                (2, CompetitionTieBreaker.Criterion.WINS),
            ],
        )
        copied_memberships = set(
            next_competition.memberships.values_list("user_id", "role")
        )
        self.assertEqual(
            copied_memberships,
            {
                (owner.id, LeagueMembership.Role.OWNER),
                (administrator.id, LeagueMembership.Role.ADMIN),
            },
        )

        next_home_team = next_teams[home_team.name]
        next_home_player = Player.objects.get(
            team=next_home_team,
            name=home_player.name,
        )
        self.assertEqual(next_home_team.home_ground, home_team.home_ground)
        next_home_team.name = "Changed New Team"
        next_home_team.save()
        next_home_player.starting_goals = 4
        next_home_player.save()
        home_team.refresh_from_db()
        home_player.refresh_from_db()
        self.assertEqual(home_team.name, "Home FC")
        self.assertEqual(home_player.starting_goals, 6)

        with self.assertRaisesMessage(ValidationError, "already has that season"):
            create_next_competition_season(
                competition,
                actor=self.superuser,
                season="2027",
                slug="archive-league-2027-second",
            )

    def test_next_season_rejects_a_slug_used_by_another_series(self):
        competition, *_ = self.make_table_competition()
        Competition.objects.create(
            name="Other League",
            slug="already-taken-season-slug",
            season="2027",
            format_type="TABLE",
        )
        archive_competition_season(competition, actor=self.superuser)

        with self.assertRaisesMessage(ValidationError, "slug is already in use"):
            create_next_competition_season(
                competition,
                actor=self.superuser,
                season="2027",
                slug="already-taken-season-slug",
            )

        self.assertEqual(Competition.objects.filter(series=competition.series).count(), 1)

    def test_next_season_rolls_back_everything_when_roster_copy_fails(self):
        competition, *_ = self.make_table_competition()
        archive_competition_season(competition, actor=self.superuser)

        with patch(
            "leagues.seasons.Player.objects.bulk_create",
            side_effect=RuntimeError("database write failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "database write failed"):
                create_next_competition_season(
                    competition,
                    actor=self.superuser,
                    season="2027",
                    slug="archive-league-2027",
                )

        self.assertFalse(Competition.objects.filter(slug="archive-league-2027").exists())
        self.assertEqual(Team.objects.filter(competition__series=competition.series).count(), 2)
        self.assertEqual(Player.objects.filter(team__competition__series=competition.series).count(), 2)

    def test_large_roster_clone_uses_a_bounded_number_of_queries(self):
        competition, *_ = self.make_table_competition()
        for team_number in range(3, 19):
            team = Team.objects.create(
                name=f"Bulk Team {team_number}",
                home_ground=f"Ground {team_number}",
                competition=competition,
            )
            Player.objects.bulk_create(
                [
                    Player(
                        name=f"Player {team_number}-{player_number}",
                        team=team,
                        jersey_number=player_number,
                        starting_goals=player_number,
                    )
                    for player_number in range(1, 26)
                ]
            )
        source_team_count = competition.teams.count()
        source_player_count = Player.objects.filter(team__competition=competition).count()
        archive_competition_season(competition, actor=self.superuser)

        with CaptureQueriesContext(connection) as queries:
            next_competition = create_next_competition_season(
                competition,
                actor=self.superuser,
                season="2027",
                slug="archive-league-2027",
            )

        # SQLite has a 999-parameter limit and therefore splits the 402-player
        # insert into three statements. PostgreSQL needs fewer statements, but
        # both backends must stay bounded rather than grow with roster size.
        self.assertLessEqual(len(queries), 28, len(queries))
        self.assertEqual(next_competition.teams.count(), source_team_count)
        self.assertEqual(
            Player.objects.filter(team__competition=next_competition).count(),
            source_player_count,
        )
        self.assertEqual(
            Standing.objects.filter(competition=next_competition).count(),
            source_team_count,
        )
        self.assertFalse(
            Player.objects.filter(
                team__competition=next_competition,
                starting_goals__gt=0,
            ).exists()
        )


class FootballSeasonPublicNavigationTests(TestCase):
    def setUp(self):
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-client",
            secret="test-secret",
        )
        social_app.sites.add(Site.objects.get_current())
        self.superuser = User.objects.create_superuser(
            username="navigation-superuser",
            email="navigation-superuser@example.com",
            password="pass",
        )
        self.archived = Competition.objects.create(
            name="Navigation League",
            slug="navigation-league-2026",
            season="2026",
            format_type="TABLE",
        )
        Stage.objects.create(
            competition=self.archived,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        archive_competition_season(self.archived, actor=self.superuser)
        self.current = create_next_competition_season(
            self.archived,
            actor=self.superuser,
            season="2027",
            slug="navigation-league-2027",
        )

    def test_public_selector_keeps_current_tab_and_archive_is_hidden_from_home(self):
        archived_results = self.client.get(
            reverse("league_results", args=[self.archived.slug])
        )
        self.assertEqual(archived_results.status_code, 200)
        self.assertContains(archived_results, "Archived season — historical data is read-only.")
        self.assertContains(
            archived_results,
            reverse("league_results", args=[self.current.slug]),
        )
        self.assertEqual(archived_results.context["active_tab"], "results")
        self.assertEqual(len(archived_results.context["season_options"]), 2)

        home = self.client.get(reverse("league_list"))
        self.assertEqual(home.status_code, 200)
        displayed_ids = [item["league"].id for item in home.context["league_data"]]
        self.assertIn(self.current.id, displayed_ids)
        self.assertNotIn(self.archived.id, displayed_ids)
