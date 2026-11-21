"""Focused regression coverage for safe archived pool-league seasons.

These tests intentionally stay at the pool boundary: historic data remains on
the source competition, while a next season receives only fresh,
competition-scoped setup rows.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .baselines import save_pool_standing_baselines
from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolCompetitionTeamStaffAssignment,
    PoolMatch,
    PoolMatchScorerAssignment,
    PoolPlayer,
    PoolRack,
    PoolStandingBaseline,
    PoolTeam,
    PoolTeamRoster,
)
from .permissions import (
    user_can_manage_competition,
    user_can_manage_team_lineup,
    user_can_score_pool_match,
)
from .seasons import (
    PoolSeasonValidationError,
    archive_pool_competition,
    create_next_pool_season,
)
from .services import record_rack, start_match
from .standings import (
    PoolStandingsValidationError,
    replace_pool_standings_points_configuration,
    replace_pool_tie_breakers,
    standings_for_stage,
)


User = get_user_model()


class PoolSeasonTestCase(TestCase):
    """Small factories for independent, safely-completable pool leagues."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-season-owner",
            password="test-password",
        )
        self.admin = User.objects.create_user(
            username="pool-season-admin",
            password="test-password",
        )
        self.scorer = User.objects.create_user(
            username="pool-season-scorer",
            password="test-password",
        )
        self.coach = User.objects.create_user(
            username="pool-season-coach",
            password="test-password",
        )
        self.superuser = User.objects.create_superuser(
            username="pool-season-superuser",
            email="pool-season-superuser@example.test",
            password="test-password",
        )
        self._number = 0

    def make_league(
        self,
        *,
        mode=PoolCompetition.ParticipantMode.SINGLES,
        season="2026",
        name=None,
        slug=None,
        **overrides,
    ):
        self._number += 1
        values = {
            "name": name or f"Pool Season League {self._number}",
            "slug": slug or f"pool-season-league-{self._number}",
            "season": season,
            "owner": self.owner,
            "participant_mode": mode,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "operational_state": PoolCompetition.OperationalState.UPCOMING,
            "default_race_target": 1,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    @staticmethod
    def league_stage(competition):
        return competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )

    def make_singles_entries(self, competition, names=("Alpha", "Bravo")):
        entries = []
        for name in names:
            player = PoolPlayer.objects.create(
                full_name=f"{name} {competition.pk}",
            )
            entries.append(
                PoolCompetitionEntry.objects.create(
                    competition=competition,
                    player=player,
                )
            )
        return entries

    def make_team_entry(self, competition, name, *, players=()):
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name=name),
        )
        player_rows = []
        for sort_order, player_name in enumerate(players):
            player = PoolPlayer.objects.create(full_name=player_name)
            player_rows.append(
                PoolTeamRoster.objects.create(
                    competition_entry=entry,
                    player=player,
                    sort_order=sort_order,
                )
            )
        return entry, player_rows

    def finish_singles_match(self, competition, home, away):
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home,
            away_entry=away,
            race_target=1,
        )
        start_match(pool_match, recorded_by=self.owner)
        record_rack(pool_match, winner=home, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        return pool_match

    @staticmethod
    def mark_completed(competition):
        competition.operational_state = PoolCompetition.OperationalState.COMPLETED
        competition.save(update_fields=["operational_state"])
        competition.refresh_from_db()
        return competition

    def archive(self, competition):
        self.mark_completed(competition)
        return archive_pool_competition(competition, actor=self.superuser)


class PoolSeasonArchiveTests(PoolSeasonTestCase):
    def test_archive_preserves_historic_rows_and_changes_only_archive_metadata(self):
        competition = self.make_league(
            name="Historic Singles League",
            slug="historic-singles-league",
            description="A finished historical table.",
        )
        home, away = self.make_singles_entries(competition)
        pool_match = self.finish_singles_match(competition, home, away)
        stage = self.league_stage(competition)
        save_pool_standing_baselines(
            stage,
            [
                {
                    "entry": home,
                    "played": 4,
                    "won": 3,
                    "lost": 1,
                    "racks_for": 12,
                    "racks_against": 7,
                    "points": 9,
                },
                {
                    "entry": away,
                    "played": 4,
                    "won": 1,
                    "lost": 3,
                    "racks_for": 7,
                    "racks_against": 12,
                    "points": 3,
                },
            ],
            actor=self.superuser,
        )
        # Completion is an archive prerequisite, not an archive side effect.
        # Snapshot the already-eligible historical season so this test verifies
        # that archiving itself changes archive metadata only.
        self.mark_completed(competition)
        competition.refresh_from_db()
        historical_competition_values = {
            field: getattr(competition, field)
            for field in (
                "name",
                "slug",
                "series_id",
                "season",
                "description",
                "visibility",
                "operational_state",
                "points_mode",
                "points_for_win",
                "points_for_loss",
                "updated_at",
            )
        }
        historical_match_values = (
            pool_match.status,
            pool_match.home_score,
            pool_match.away_score,
            pool_match.winner_id,
        )
        historical_baselines = list(
            PoolStandingBaseline.objects.filter(competition=competition)
            .order_by("entry_id")
            .values_list(
                "entry_id",
                "played",
                "won",
                "lost",
                "racks_for",
                "racks_against",
                "points",
            )
        )
        historical_standings = [
            (
                row.entry_id,
                row.played,
                row.won,
                row.lost,
                row.racks_for,
                row.racks_against,
                row.points,
            )
            for row in standings_for_stage(stage)
        ]
        historical_rack_count = PoolRack.objects.filter(
            match__competition=competition
        ).count()

        archived = self.archive(competition)

        archived.refresh_from_db()
        pool_match.refresh_from_db()
        self.assertTrue(archived.is_archived)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(archived.archived_by_id, self.superuser.pk)
        for field, value in historical_competition_values.items():
            self.assertEqual(getattr(archived, field), value)
        self.assertEqual(
            (
                pool_match.status,
                pool_match.home_score,
                pool_match.away_score,
                pool_match.winner_id,
            ),
            historical_match_values,
        )
        self.assertEqual(
            list(
                PoolStandingBaseline.objects.filter(competition=competition)
                .order_by("entry_id")
                .values_list(
                    "entry_id",
                    "played",
                    "won",
                    "lost",
                    "racks_for",
                    "racks_against",
                    "points",
                )
            ),
            historical_baselines,
        )
        self.assertEqual(
            [
                (
                    row.entry_id,
                    row.played,
                    row.won,
                    row.lost,
                    row.racks_for,
                    row.racks_against,
                    row.points,
                )
                for row in standings_for_stage(stage)
            ],
            historical_standings,
        )
        self.assertEqual(
            PoolRack.objects.filter(match__competition=competition).count(),
            historical_rack_count,
        )

    def test_archive_requires_superuser_completed_league_and_no_unresolved_fixtures(self):
        completed = self.make_league(slug="archive-superuser-only")
        self.mark_completed(completed)
        with self.assertRaisesMessage(PoolSeasonValidationError, "superuser"):
            archive_pool_competition(completed, actor=self.owner)
        completed.refresh_from_db()
        self.assertFalse(completed.is_archived)

        incomplete = self.make_league(slug="archive-incomplete")
        with self.assertRaisesMessage(PoolSeasonValidationError, "Complete"):
            archive_pool_competition(incomplete, actor=self.superuser)

        for status in (
            PoolMatch.Status.UPCOMING,
            PoolMatch.Status.LIVE,
            PoolMatch.Status.POSTPONED,
        ):
            with self.subTest(status=status):
                unresolved = self.make_league(slug=f"archive-{status.lower()}")
                home, away = self.make_singles_entries(unresolved)
                PoolMatch.objects.create(
                    competition=unresolved,
                    stage=self.league_stage(unresolved),
                    home_entry=home,
                    away_entry=away,
                    status=status,
                    race_target=1,
                )
                self.mark_completed(unresolved)
                with self.assertRaisesMessage(
                    PoolSeasonValidationError,
                    "Resolve every upcoming, live, or postponed fixture",
                ):
                    archive_pool_competition(unresolved, actor=self.superuser)

        non_league = self.make_league(
            slug="archive-league-knockout",
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        self.mark_completed(non_league)
        with self.assertRaisesMessage(PoolSeasonValidationError, "Only pool LEAGUE"):
            archive_pool_competition(non_league, actor=self.superuser)

    def test_archive_route_leaves_owner_unable_to_archive_and_allows_superuser(self):
        competition = self.make_league(slug="archive-route-permissions")
        self.mark_completed(competition)
        url = reverse("pool:manage_season_archive", args=[competition.slug])

        self.client.force_login(self.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        competition.refresh_from_db()
        self.assertFalse(competition.is_archived)

        self.client.force_login(self.superuser)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        competition.refresh_from_db()
        self.assertTrue(competition.is_archived)
        self.assertEqual(competition.archived_by_id, self.superuser.pk)


class PoolSinglesNextSeasonTests(PoolSeasonTestCase):
    def test_next_singles_season_reuses_players_copies_valid_configuration_and_starts_empty(self):
        source = self.make_league(
            name="City Pool League",
            slug="city-pool-league-2026",
            description="The original league rules.",
            venue="City Hall",
            rules="Best of the season.",
        )
        PoolCompetitionMembership.objects.create(
            competition=source,
            user=self.admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        home, away = self.make_singles_entries(source)
        self.finish_singles_match(source, home, away)
        source_stage = self.league_stage(source)
        save_pool_standing_baselines(
            source_stage,
            [
                {
                    "entry": home,
                    "played": 2,
                    "won": 2,
                    "lost": 0,
                    "racks_for": 6,
                    "racks_against": 1,
                    "points": 6,
                },
                {
                    "entry": away,
                    "played": 2,
                    "won": 0,
                    "lost": 2,
                    "racks_for": 1,
                    "racks_against": 6,
                    "points": 0,
                },
            ],
            actor=self.superuser,
        )
        replace_pool_tie_breakers(
            source,
            [
                (1, PoolCompetitionTieBreaker.Criterion.MATCHES_WON),
                (2, PoolCompetitionTieBreaker.Criterion.RACKS_WON),
            ],
        )
        replace_pool_standings_points_configuration(
            source,
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            points_for_win=3,
            points_for_loss=1,
            margin_rules=((1, 1, 4, 0),),
        )
        source.refresh_from_db()
        expected_tie_breakers = list(
            source.tie_breakers.order_by("order", "id").values_list("order", "criterion")
        )
        expected_margin_rules = list(
            source.margin_point_rules.order_by("minimum_margin", "id").values_list(
                "minimum_margin",
                "maximum_margin",
                "winner_points",
                "loser_points",
            )
        )
        source_player_ids = set(source.entries.values_list("player_id", flat=True))
        source_entry_ids = set(source.entries.values_list("pk", flat=True))
        self.archive(source)

        target = create_next_pool_season(
            source,
            season="2027",
            slug="city-pool-league-2027",
            actor=self.superuser,
        )

        self.assertNotEqual(target.pk, source.pk)
        self.assertEqual(target.series_id, source.series_id)
        self.assertEqual(target.season, "2027")
        self.assertFalse(target.is_archived)
        self.assertEqual(target.visibility, PoolCompetition.Visibility.DRAFT)
        self.assertEqual(
            target.operational_state,
            PoolCompetition.OperationalState.UPCOMING,
        )
        self.assertEqual(target.description, source.description)
        self.assertEqual(target.venue, source.venue)
        self.assertEqual(target.rules, source.rules)

        target_entries = list(target.entries.order_by("id"))
        self.assertEqual(
            {entry.player_id for entry in target_entries},
            source_player_ids,
        )
        self.assertTrue(source_entry_ids.isdisjoint({entry.pk for entry in target_entries}))
        self.assertEqual(target.matches.count(), 0)
        self.assertEqual(PoolRack.objects.filter(match__competition=target).count(), 0)
        self.assertFalse(PoolStandingBaseline.objects.filter(competition=target).exists())
        target_stage = self.league_stage(target)
        self.assertEqual(
            {
                entry.player_id
                for entry in target.entries.select_related("player").all()
            },
            source_player_ids,
        )
        for row in standings_for_stage(target_stage):
            self.assertEqual(
                (
                    row.played,
                    row.won,
                    row.lost,
                    row.racks_for,
                    row.racks_against,
                    row.points,
                ),
                (0, 0, 0, 0, 0, 0),
            )

        self.assertEqual(
            list(target.tie_breakers.order_by("order", "id").values_list("order", "criterion")),
            expected_tie_breakers,
        )
        target.refresh_from_db()
        self.assertEqual(target.points_mode, PoolCompetition.PointsMode.MARGIN_BASED)
        self.assertEqual((target.points_for_win, target.points_for_loss), (3, 1))
        self.assertEqual(
            list(
                target.margin_point_rules.order_by("minimum_margin", "id").values_list(
                    "minimum_margin",
                    "maximum_margin",
                    "winner_points",
                    "loser_points",
                )
            ),
            expected_margin_rules,
        )
        self.assertTrue(
            PoolCompetitionMembership.objects.filter(
                competition=target,
                user=self.admin,
                role=PoolCompetitionMembership.Role.ADMIN,
                is_active=True,
            ).exists()
        )
        self.assertTrue(
            PoolStandingBaseline.objects.filter(competition=source).exists()
        )
        self.assertTrue(PoolRack.objects.filter(match__competition=source).exists())

    def test_next_singles_season_requires_archived_source_unique_identity_and_superuser(self):
        source = self.make_league(slug="singles-next-restrictions")
        with self.assertRaisesMessage(PoolSeasonValidationError, "Archive this"):
            create_next_pool_season(
                source,
                season="2027",
                slug="singles-next-restrictions-2027",
                actor=self.superuser,
            )

        self.archive(source)
        with self.assertRaisesMessage(PoolSeasonValidationError, "superuser"):
            create_next_pool_season(
                source,
                season="2027",
                slug="singles-next-restrictions-2027",
                actor=self.owner,
            )
        create_next_pool_season(
            source,
            season="2027",
            slug="singles-next-restrictions-2027",
            actor=self.superuser,
        )
        with self.assertRaisesMessage(PoolSeasonValidationError, "already has that season"):
            create_next_pool_season(
                source,
                season="2027",
                slug="singles-next-restrictions-duplicate",
                actor=self.superuser,
            )

    def test_invalid_copied_points_policy_is_not_allowed_to_bypass_validation(self):
        source = self.make_league(slug="invalid-copied-configuration")
        self.archive(source)
        target_slug = "invalid-copied-configuration-2027"

        with patch(
            "pool.seasons.replace_pool_standings_points_configuration",
            side_effect=PoolStandingsValidationError("Invalid copied policy."),
        ) as replace_policy:
            with self.assertRaisesMessage(PoolStandingsValidationError, "Invalid copied"):
                create_next_pool_season(
                    source,
                    season="2027",
                    slug=target_slug,
                    actor=self.superuser,
                )

        replace_policy.assert_called_once()
        self.assertFalse(PoolCompetition.objects.filter(slug=target_slug).exists())


class PoolTeamNextSeasonTests(PoolSeasonTestCase):
    def test_next_team_season_reuses_global_teams_and_players_with_isolated_rosters(self):
        source = self.make_league(
            mode=PoolCompetition.ParticipantMode.TEAMS,
            name="Team League",
            slug="team-league-2026",
        )
        sharks, sharks_roster = self.make_team_entry(
            source,
            "Sharks",
            players=("Sharks One", "Sharks Two"),
        )
        eagles, eagles_roster = self.make_team_entry(
            source,
            "Eagles",
            players=("Eagles One", "Eagles Two"),
        )
        sharks_roster[1].is_active = False
        sharks_roster[1].save(update_fields=["is_active"])
        source_team_ids = set(source.entries.values_list("team_id", flat=True))
        source_entry_ids = set(source.entries.values_list("pk", flat=True))
        source_roster_ids = set(
            PoolTeamRoster.objects.filter(competition_entry__competition=source).values_list(
                "pk", flat=True
            )
        )
        source_roster_shape = {
            (row.competition_entry.team_id, row.player_id): (row.is_active, row.sort_order)
            for row in PoolTeamRoster.objects.filter(
                competition_entry__competition=source
            ).select_related("competition_entry")
        }
        self.archive(source)

        target = create_next_pool_season(
            source,
            season="2027",
            slug="team-league-2027",
            actor=self.superuser,
        )

        target_entries = list(target.entries.select_related("team").order_by("id"))
        self.assertEqual({entry.team_id for entry in target_entries}, source_team_ids)
        self.assertTrue(source_entry_ids.isdisjoint({entry.pk for entry in target_entries}))
        target_rosters = list(
            PoolTeamRoster.objects.filter(competition_entry__competition=target)
            .select_related("competition_entry")
            .order_by("id")
        )
        self.assertTrue(source_roster_ids.isdisjoint({row.pk for row in target_rosters}))
        self.assertEqual(
            {
                (row.competition_entry.team_id, row.player_id): (
                    row.is_active,
                    row.sort_order,
                )
                for row in target_rosters
            },
            source_roster_shape,
        )
        self.assertEqual(target.matches.count(), 0)
        self.assertFalse(PoolStandingBaseline.objects.filter(competition=target).exists())

        changed_target_roster = next(
            row
            for row in target_rosters
            if row.competition_entry.team_id == sharks.team_id
            and row.is_active
        )
        changed_target_roster.is_active = False
        changed_target_roster.save(update_fields=["is_active"])
        new_player = PoolPlayer.objects.create(full_name="Only 2027 Shark")
        target_sharks = next(
            entry for entry in target_entries if entry.team_id == sharks.team_id
        )
        PoolTeamRoster.objects.create(
            competition_entry=target_sharks,
            player=new_player,
            sort_order=10,
        )

        self.assertEqual(
            {
                (row.competition_entry.team_id, row.player_id): (
                    row.is_active,
                    row.sort_order,
                )
                for row in PoolTeamRoster.objects.filter(
                    competition_entry__competition=source
                ).select_related("competition_entry")
            },
            source_roster_shape,
        )
        self.assertFalse(
            PoolTeamRoster.objects.filter(
                competition_entry__competition=source,
                player=new_player,
            ).exists()
        )
        self.assertEqual(eagles.team_id, next(
            entry.team_id for entry in target_entries if entry.team_id == eagles.team_id
        ))


class PoolArchivedSeasonAccessTests(PoolSeasonTestCase):
    def test_archived_manager_scorer_and_team_lineup_posts_are_rejected(self):
        singles = self.make_league(slug="archived-singles-permissions")
        home, away = self.make_singles_entries(singles)
        singles_match = self.finish_singles_match(singles, home, away)
        PoolMatchScorerAssignment.objects.create(
            match=singles_match,
            user=self.scorer,
            assigned_by=self.owner,
        )
        self.archive(singles)

        # Manufacture an otherwise scoreable historic fixture only to prove
        # that the archived-season permission gate, not its terminal result,
        # is what rejects the delegated scorer.
        PoolMatch.objects.filter(pk=singles_match.pk).update(
            status=PoolMatch.Status.UPCOMING,
            home_score=0,
            away_score=0,
            winner=None,
            completed_at=None,
            completion_reason="",
        )
        singles_match.refresh_from_db()
        self.assertFalse(user_can_score_pool_match(self.scorer, singles_match))

        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(
                reverse("pool:manage_competition_entry_create", args=[singles.slug]),
                {},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("pool:manage_competition_dashboard", args=[singles.slug])
            ).status_code,
            200,
        )
        self.assertTrue(
            user_can_manage_competition(self.superuser, singles)
        )

        self.client.force_login(self.scorer)
        self.assertEqual(
            self.client.post(
                reverse(
                    "pool:manage_match_start",
                    args=[singles.slug, singles_match.pk],
                )
            ).status_code,
            404,
        )

        teams = self.make_league(
            mode=PoolCompetition.ParticipantMode.TEAMS,
            slug="archived-team-lineup-permissions",
        )
        home_team, _home_roster = self.make_team_entry(
            teams,
            "Lineup Sharks",
            players=("Lineup Shark One",),
        )
        away_team, _away_roster = self.make_team_entry(
            teams,
            "Lineup Eagles",
            players=("Lineup Eagle One",),
        )
        team_match = PoolMatch.objects.create(
            competition=teams,
            stage=self.league_stage(teams),
            home_entry=home_team,
            away_entry=away_team,
            status=PoolMatch.Status.FINISHED,
            race_target=1,
            home_score=1,
            away_score=0,
            winner=home_team,
        )
        PoolCompetitionTeamStaffAssignment.objects.create(
            competition_entry=home_team,
            user=self.coach,
            role=PoolCompetitionTeamStaffAssignment.Role.COACH,
        )
        self.archive(teams)
        PoolMatch.objects.filter(pk=team_match.pk).update(
            status=PoolMatch.Status.UPCOMING,
            home_score=0,
            away_score=0,
            winner=None,
            completed_at=None,
            completion_reason="",
        )
        team_match.refresh_from_db()
        self.assertFalse(
            user_can_manage_team_lineup(self.coach, team_match, home_team)
        )
        self.client.force_login(self.coach)
        self.assertEqual(
            self.client.post(
                reverse(
                    "pool:team_match_lineup",
                    args=[team_match.pk, home_team.pk],
                ),
                {},
            ).status_code,
            403,
        )

    def test_archived_public_detail_and_profiles_remain_available_but_home_hides_history(self):
        source = self.make_league(
            name="Public Archive League",
            slug="public-archive-league-2026",
        )
        home, away = self.make_singles_entries(source)
        historical_match = self.finish_singles_match(source, home, away)
        self.archive(source)
        target = create_next_pool_season(
            source,
            season="2027",
            slug="public-archive-league-2027",
            actor=self.superuser,
        )
        target.visibility = PoolCompetition.Visibility.PUBLISHED
        target.save(update_fields=["visibility"])

        detail = self.client.get(
            reverse("pool:competition_detail", args=[source.slug]),
            {"tab": "results"},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context["active_tab"], "results")
        self.assertEqual(
            {competition.pk for competition in detail.context["season_selector_competitions"]},
            {source.pk, target.pk},
        )
        self.assertContains(detail, "Season: 2026")
        self.assertContains(detail, "Season: 2027")
        self.assertContains(
            detail,
            f"{reverse('pool:competition_detail', args=[target.slug])}?tab=results",
        )
        self.assertEqual(
            self.client.get(
                reverse("pool:competition_tab_content", args=[source.slug, "results"])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("pool:match_detail", args=[historical_match.pk])).status_code,
            200,
        )

        home_response = self.client.get(reverse("pool:home"))
        self.assertEqual(home_response.status_code, 200)
        home_competition_ids = {
            competition.pk for competition in home_response.context["competitions"]
        }
        self.assertIn(target.pk, home_competition_ids)
        self.assertNotIn(source.pk, home_competition_ids)

        player_profile = self.client.get(
            reverse("pool:player_detail", args=[home.player.public_id])
        )
        self.assertEqual(player_profile.status_code, 200)
        self.assertIn(
            source.pk,
            {
                entry.competition_id
                for entry in player_profile.context["player_entries"]
            },
        )

        team_history = self.make_league(
            mode=PoolCompetition.ParticipantMode.TEAMS,
            name="Published Team History",
            slug="published-team-history",
        )
        historic_team, _ = self.make_team_entry(
            team_history,
            "Historic Team",
            players=("Historic Team Player",),
        )
        self.archive(team_history)
        team_profile = self.client.get(
            reverse("pool:team_detail", args=[historic_team.team_id])
        )
        self.assertEqual(team_profile.status_code, 200)
        self.assertIn(
            team_history.pk,
            {
                entry.competition_id for entry in team_profile.context["team_entries"]
            },
        )
