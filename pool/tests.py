"""Focused regression coverage for the 8-ball pool foundation."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import Competition as FootballCompetition
from leagues.models import Stage as FootballStage
from matches.models import Match as FootballMatch
from pool.models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamRoster,
)
from pool.permissions import user_manages_pool_competition
from pool.queries import competition_statistics, match_rack_summary
from pool.services import create_default_stages, record_rack, start_match
from teams.models import Team as FootballTeam


User = get_user_model()


class PoolFoundationTestCase(TestCase):
    """Shared factories that use only pool-specific data structures."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        defaults = {
            "name": f"Pool Competition {self._competition_number}",
            "slug": f"pool-competition-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "visibility": PoolCompetition.Visibility.DRAFT,
        }
        defaults.update(overrides)
        return PoolCompetition.objects.create(**defaults)

    def make_player(self, name="Pool Player", **overrides):
        defaults = {"full_name": name}
        defaults.update(overrides)
        return PoolPlayer.objects.create(**defaults)

    def make_singles_entry(self, competition, name="Pool Player"):
        player = self.make_player(name)
        return player, PoolCompetitionEntry.objects.create(
            competition=competition,
            player=player,
        )

    def stage_for(self, competition, stage_type):
        """Use an existing service-created stage where available."""
        stage = competition.stages.filter(stage_type=stage_type).first()
        if stage:
            return stage

        names = {
            PoolCompetitionStage.StageType.LEAGUE: "League stage",
            PoolCompetitionStage.StageType.GROUP: "Group stage",
            PoolCompetitionStage.StageType.KNOCKOUT: "Knockout stage",
        }
        return PoolCompetitionStage.objects.create(
            competition=competition,
            name=names[stage_type],
            stage_type=stage_type,
            order=competition.stages.count() + 1,
        )

    @staticmethod
    def statistic_value(statistic, key):
        if isinstance(statistic, dict):
            return statistic[key]
        return getattr(statistic, key)


class PoolModelChoiceTests(PoolFoundationTestCase):
    def test_competition_match_and_rack_choices_cover_phase_one_domain(self):
        self.assertEqual(
            {choice[0] for choice in PoolCompetition.ParticipantMode.choices},
            {"SINGLES", "TEAMS"},
        )
        self.assertEqual(
            {choice[0] for choice in PoolCompetition.FormatType.choices},
            {"LEAGUE", "LEAGUE_KNOCKOUT", "KNOCKOUT"},
        )
        self.assertEqual(
            {choice[0] for choice in PoolCompetition.Visibility.choices},
            {"DRAFT", "PUBLISHED"},
        )
        self.assertEqual(
            {choice[0] for choice in PoolCompetition.OperationalState.choices},
            {"UPCOMING", "ACTIVE", "COMPLETED"},
        )
        self.assertEqual(
            {choice[0] for choice in PoolMatch.Status.choices},
            {
                "UPCOMING",
                "LIVE",
                "FINISHED",
                "POSTPONED",
                "CANCELLED",
                "ABANDONED",
            },
        )
        self.assertEqual(
            {choice[0] for choice in PoolRack.OutcomeType.choices},
            {
                "NORMAL",
                "GREEN_APPLE",
                "RED_APPLE",
                "ONE_VISIT",
                "LINEUP_VACANCY",
            },
        )

    def test_default_stage_service_creates_an_explicit_idempotent_structure(self):
        expected_stage_types = {
            PoolCompetition.FormatType.LEAGUE: [
                PoolCompetitionStage.StageType.LEAGUE,
            ],
            PoolCompetition.FormatType.LEAGUE_KNOCKOUT: [
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.KNOCKOUT,
            ],
            PoolCompetition.FormatType.KNOCKOUT: [
                PoolCompetitionStage.StageType.KNOCKOUT,
            ],
        }

        for format_type, expected in expected_stage_types.items():
            competition = self.make_competition(format_type=format_type)
            create_default_stages(competition)
            create_default_stages(competition)
            self.assertEqual(
                list(
                    competition.stages.order_by("order").values_list(
                        "stage_type",
                        flat=True,
                    )
                ),
                expected,
            )

    def test_format_cannot_change_after_default_stages_are_provisioned(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE,
        )
        competition.format_type = PoolCompetition.FormatType.KNOCKOUT

        with self.assertRaises(ValidationError):
            competition.full_clean()


class PoolCompetitionEntryValidationTests(PoolFoundationTestCase):
    def test_entry_requires_exactly_one_participant_of_the_right_mode(self):
        singles = self.make_competition()
        teams = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        player = self.make_player("Singles Player")
        team = PoolTeam.objects.create(name="Cue Masters")

        with self.assertRaises(ValidationError):
            PoolCompetitionEntry(competition=singles).full_clean()
        with self.assertRaises(ValidationError):
            PoolCompetitionEntry(
                competition=singles,
                player=player,
                team=team,
            ).full_clean()
        with self.assertRaises(ValidationError):
            PoolCompetitionEntry(competition=singles, team=team).full_clean()
        with self.assertRaises(ValidationError):
            PoolCompetitionEntry(competition=teams, player=player).full_clean()

        PoolCompetitionEntry(competition=singles, player=player).full_clean()
        PoolCompetitionEntry(competition=teams, team=team).full_clean()

    def test_duplicate_player_entry_is_rejected_by_validation_and_database(self):
        competition = self.make_competition()
        player, _ = self.make_singles_entry(competition, "Only Once")
        duplicate = PoolCompetitionEntry(competition=competition, player=player)

        with self.assertRaises(ValidationError):
            duplicate.full_clean()

        # bulk_create bypasses model validation, so this proves the partial
        # unique database constraint also protects imported/admin data.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PoolCompetitionEntry.objects.bulk_create([duplicate])

    def test_duplicate_team_entry_is_rejected_by_validation_and_database(self):
        competition = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        team = PoolTeam.objects.create(name="One Team Only")
        PoolCompetitionEntry.objects.create(competition=competition, team=team)
        duplicate = PoolCompetitionEntry(competition=competition, team=team)

        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PoolCompetitionEntry.objects.bulk_create([duplicate])

    def test_team_roster_cannot_add_the_same_player_twice_to_one_entry(self):
        competition = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        team = PoolTeam.objects.create(name="Roster Guard Team")
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=team,
        )
        player = self.make_player("Roster Guard Player")
        PoolTeamRoster.objects.create(competition_entry=entry, player=player)

        duplicate = PoolTeamRoster(competition_entry=entry, player=player)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()


class PoolMatchValidationTests(PoolFoundationTestCase):
    def setUp(self):
        super().setUp()
        self.competition = self.make_competition()
        self.stage = self.stage_for(
            self.competition,
            PoolCompetitionStage.StageType.LEAGUE,
        )
        self.home_player, self.home_entry = self.make_singles_entry(
            self.competition,
            "Home Cueist",
        )
        self.away_player, self.away_entry = self.make_singles_entry(
            self.competition,
            "Away Cueist",
        )

    def candidate_match(self, **overrides):
        defaults = {
            "competition": self.competition,
            "stage": self.stage,
            "home_entry": self.home_entry,
            "away_entry": self.away_entry,
            "scheduled_at": timezone.now(),
        }
        defaults.update(overrides)
        return PoolMatch(**defaults)

    def test_match_rejects_wrong_stage_participant_or_self_pairing(self):
        other_competition = self.make_competition()
        other_stage = self.stage_for(
            other_competition,
            PoolCompetitionStage.StageType.LEAGUE,
        )
        _, other_entry = self.make_singles_entry(other_competition, "Outsider")

        with self.assertRaises(ValidationError):
            self.candidate_match(stage=other_stage).full_clean()
        with self.assertRaises(ValidationError):
            self.candidate_match(away_entry=other_entry).full_clean()
        with self.assertRaises(ValidationError):
            self.candidate_match(away_entry=self.home_entry).full_clean()

    def test_match_rejects_negative_scores_and_invalid_finished_winners(self):
        _, third_entry = self.make_singles_entry(self.competition, "Third Cueist")

        with self.assertRaises(ValidationError):
            self.candidate_match(home_score=-1).full_clean()
        with self.assertRaises(ValidationError):
            self.candidate_match(
                status=PoolMatch.Status.FINISHED,
                home_score=7,
                away_score=4,
                result_method=PoolMatch.ResultMethod.PLAYED,
            ).full_clean()
        with self.assertRaises(ValidationError):
            self.candidate_match(
                status=PoolMatch.Status.FINISHED,
                home_score=7,
                away_score=4,
                winner=third_entry,
                result_method=PoolMatch.ResultMethod.PLAYED,
            ).full_clean()


class PoolPublicCompetitionTests(PoolFoundationTestCase):
    def test_home_shows_published_competitions_and_filters_real_urls(self):
        singles_league = self.make_competition(
            name="Singles League",
            slug="singles-league",
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        self.stage_for(singles_league, PoolCompetitionStage.StageType.LEAGUE)
        team_tournament = self.make_competition(
            name="Team Knockout",
            slug="team-knockout",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        self.stage_for(team_tournament, PoolCompetitionStage.StageType.KNOCKOUT)
        self.make_competition(
            name="Unpublished Draft",
            slug="unpublished-draft",
            visibility=PoolCompetition.Visibility.DRAFT,
        )

        response = self.client.get(
            reverse("pool:home"),
            {"category": "leagues", "mode": "singles"},
        )
        self.assertContains(response, singles_league.name)
        self.assertContains(response, team_tournament.name)
        html = response.content.decode()

        self.assertIn(
            'data-category="tournaments"',
            html,
        )
        self.assertIn(
            'data-mode="teams"',
            html,
        )
        self.assertNotContains(response, "Unpublished Draft")
        self.assertContains(response, "Football")
        self.assertContains(response, "8-Ball Pool")

        response = self.client.get(
            reverse("pool:home"),
            {"category": "tournaments", "mode": "teams"},
        )
        self.assertContains(response, team_tournament.name)
        self.assertContains(response, singles_league.name)
        self.assertNotContains(response, "Unpublished Draft")

    def test_standings_tab_only_appears_for_league_or_group_competitions(self):
        league = self.make_competition(
            name="League With Standings",
            slug="league-with-standings",
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        self.stage_for(league, PoolCompetitionStage.StageType.LEAGUE)
        knockout = self.make_competition(
            name="Knockout Without Standings",
            slug="knockout-without-standings",
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        self.stage_for(knockout, PoolCompetitionStage.StageType.KNOCKOUT)

        response = self.client.get(
            reverse("pool:competition_detail", args=[league.slug]),
            {"tab": "standings"},
        )
        self.assertContains(response, "?tab=standings")
        self.assertContains(response, "Standings will appear once competition play begins.")

        response = self.client.get(
            reverse("pool:competition_detail", args=[knockout.slug]),
        )
        self.assertNotContains(response, "?tab=standings")


class PoolPublicMatchAndProfileTests(PoolFoundationTestCase):
    def published_match(self):
        competition = self.make_competition(
            name="Public Pool Event",
            slug="public-pool-event",
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        home_player, home_entry = self.make_singles_entry(competition, "Public Home")
        away_player, away_entry = self.make_singles_entry(competition, "Public Away")
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )
        return competition, home_player, home_entry, away_player, away_entry, pool_match

    def test_public_match_detail_is_read_only_and_draft_match_is_not_public(self):
        _, home_player, home_entry, _, away_entry, pool_match = self.published_match()
        recorder = User.objects.create_user(
            username="private-rack-recorder",
            password="test-password",
        )
        start_match(pool_match, recorded_by=recorder)
        record_rack(
            pool_match,
            winner=home_entry,
            recorded_by=recorder,
        )

        response = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, home_player.full_name)
        self.assertContains(response, away_entry.display_name)
        self.assertContains(response, f"Race to {pool_match.race_target}")
        self.assertNotContains(response, recorder.username)

        draft = self.make_competition(name="Private Event", slug="private-event")
        draft_stage = self.stage_for(draft, PoolCompetitionStage.StageType.LEAGUE)
        _, draft_home = self.make_singles_entry(draft, "Private Home")
        _, draft_away = self.make_singles_entry(draft, "Private Away")
        draft_match = PoolMatch.objects.create(
            competition=draft,
            stage=draft_stage,
            home_entry=draft_home,
            away_entry=draft_away,
            scheduled_at=timezone.now(),
        )
        self.assertEqual(
            self.client.get(reverse("pool:match_detail", args=[draft_match.pk])).status_code,
            404,
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse("pool:match_detail", args=[draft_match.pk])).status_code,
            200,
        )

    def test_player_page_never_exposes_private_contact_fields(self):
        competition = self.make_competition(
            name="Player Public Event",
            slug="player-public-event",
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        player = self.make_player(
            "Private Contact Player",
            contact_email="private.pool.player@example.test",
            contact_phone="+268-555-0101",
        )
        PoolCompetitionEntry.objects.create(competition=competition, player=player)

        response = self.client.get(
            reverse("pool:player_detail", args=[player.public_id]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, player.full_name)
        self.assertNotContains(response, player.contact_email)
        self.assertNotContains(response, player.contact_phone)

    def test_team_page_uses_competition_specific_roster(self):
        competition = self.make_competition(
            name="Team Event",
            slug="team-event",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        team = PoolTeam.objects.create(name="The Breakers")
        team_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=team,
        )
        roster_player = self.make_player("Roster Player")
        PoolTeamRoster.objects.create(
            competition_entry=team_entry,
            player=roster_player,
        )

        response = self.client.get(
            reverse("pool:team_detail", args=[team.pk]),
            {"competition": competition.slug},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, team.name)
        self.assertContains(response, roster_player.full_name)


class PoolStatisticsAndPermissionTests(PoolFoundationTestCase):
    def finished_match_with_racks(self):
        competition = self.make_competition(
            name="Statistics Event",
            slug="statistics-event",
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=2,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        home_player, home_entry = self.make_singles_entry(competition, "Stats Home")
        away_player, away_entry = self.make_singles_entry(competition, "Stats Away")
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )
        start_match(pool_match)
        record_rack(
            pool_match,
            winner=home_entry,
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
        )
        record_rack(
            pool_match,
            winner=away_entry,
            outcome_type=PoolRack.OutcomeType.RED_APPLE,
        )
        record_rack(
            pool_match,
            winner=home_entry,
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
        )
        pool_match.refresh_from_db()
        return competition, home_player, away_player, pool_match

    def test_rack_outcomes_and_competition_statistics_are_derived(self):
        competition, home_player, away_player, pool_match = self.finished_match_with_racks()

        summary = match_rack_summary(pool_match)
        self.assertEqual(self.statistic_value(summary, "green_apples"), 1)
        self.assertEqual(self.statistic_value(summary, "red_apples"), 1)
        self.assertEqual(self.statistic_value(summary, "one_visits"), 1)

        statistics = competition_statistics(competition)
        by_player_id = {
            self.statistic_value(statistic, "participant").pk: statistic
            for statistic in statistics
        }
        home = by_player_id[home_player.pk]
        away = by_player_id[away_player.pk]
        self.assertEqual(self.statistic_value(home, "matches_played"), 1)
        self.assertEqual(self.statistic_value(home, "won"), 1)
        self.assertEqual(self.statistic_value(home, "lost"), 0)
        self.assertEqual(self.statistic_value(home, "racks_won"), 2)
        self.assertEqual(self.statistic_value(home, "racks_lost"), 1)
        self.assertEqual(self.statistic_value(home, "green_apples"), 1)
        self.assertEqual(self.statistic_value(home, "one_visits"), 1)
        self.assertEqual(self.statistic_value(away, "won"), 0)
        self.assertEqual(self.statistic_value(away, "lost"), 1)
        self.assertEqual(self.statistic_value(away, "racks_won"), 1)
        self.assertEqual(self.statistic_value(away, "red_apples"), 1)

    def test_owner_and_admin_permissions_do_not_cross_competitions(self):
        competition = self.make_competition()
        other_competition = self.make_competition(owner=self.owner)
        admin = User.objects.create_user(username="pool-admin", password="test-password")
        outsider = User.objects.create_user(
            username="pool-outsider",
            password="test-password",
        )
        superuser = User.objects.create_superuser(
            username="pool-superuser",
            email="superuser@example.test",
            password="test-password",
        )
        PoolCompetitionMembership.objects.create(
            user=admin,
            competition=competition,
            role=PoolCompetitionMembership.Role.ADMIN,
        )

        self.assertTrue(user_manages_pool_competition(self.owner, competition))
        self.assertTrue(user_manages_pool_competition(admin, competition))
        self.assertTrue(user_manages_pool_competition(superuser, competition))
        self.assertFalse(user_manages_pool_competition(outsider, competition))
        self.assertFalse(user_manages_pool_competition(admin, other_competition))


class PoolAdminSecurityTests(PoolFoundationTestCase):
    def test_pool_admin_setup_is_limited_to_superusers(self):
        staff_user = User.objects.create_user(
            username="pool-staff-user",
            password="test-password",
            is_staff=True,
        )
        superuser = User.objects.create_superuser(
            username="pool-admin-superuser",
            email="pool-admin@example.test",
            password="test-password",
        )
        changelist_url = reverse("admin:pool_poolplayer_changelist")

        self.client.force_login(staff_user)
        self.assertEqual(self.client.get(changelist_url).status_code, 403)

        self.client.force_login(superuser)
        self.assertEqual(self.client.get(changelist_url).status_code, 200)


class FootballRegressionTests(PoolFoundationTestCase):
    def test_existing_football_homepage_and_primary_detail_urls_resolve(self):
        competition = FootballCompetition.objects.create(
            name="Football Regression League",
            slug="football-regression-league",
            season="2026",
        )
        stage = FootballStage.objects.create(
            competition=competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        home_team = FootballTeam.objects.create(
            name="Football Home",
            competition=competition,
        )
        away_team = FootballTeam.objects.create(
            name="Football Away",
            competition=competition,
        )
        match = FootballMatch.objects.create(
            competition=competition,
            stage=stage,
            home_team=home_team,
            away_team=away_team,
            match_date=timezone.now(),
        )

        for url in (
            reverse("league_list"),
            reverse("league_detail", args=[competition.slug]),
            reverse("match_detail", args=[match.pk]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
