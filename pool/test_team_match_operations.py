"""Focused coverage for fixed-position pool team-match operations.

These tests deliberately use the public transactional services for domain
behaviour and the narrow manager/lineup endpoints for authorisation.  They do
not depend on fixture scheduling time: the fixture status is the sole lineup
lock boundary.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolCompetitionTeamStaffAssignment,
    PoolMatch,
    PoolMatchScorerAssignment,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchAuditEvent,
    PoolTeamMatchLineup,
    PoolTeamMatchScheduledRack,
    PoolTeamMatchSubstitution,
    PoolTeamRoster,
)
from .permissions import user_can_manage_team_lineup
from .queries import team_entry_rack_statistics
from .services import (
    PoolResultValidationError,
    expire_match_if_due,
    open_result_correction,
    record_non_played_result,
    record_rack,
    start_match,
    transition_match_status,
)
from .team_operations import (
    TEAM_MATCH_SCORE_SHEET,
    complete_team_match_rack,
    correct_completed_team_match_rack,
    ensure_team_match_schedule,
    fill_team_match_vacancy,
    start_next_eligible_team_rack,
    submit_team_match_lineup,
    substitute_team_match_player,
    team_match_schedule_state,
)


User = get_user_model()


class TeamMatchOperationsTests(TestCase):
    """Team fixture services, scoping, and public presentation."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="team-operations-owner",
            password="test-password",
        )
        self.home_coach = User.objects.create_user(
            username="home-team-coach",
            password="test-password",
        )
        self.away_coach = User.objects.create_user(
            username="away-team-coach",
            password="test-password",
        )
        self.outsider = User.objects.create_user(
            username="team-operations-outsider",
            password="test-password",
        )
        self.competition = PoolCompetition.objects.create(
            name="Team Match Operations Cup",
            slug="team-match-operations-cup",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=7,
        )
        self.stage = self.competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        self.home_entry, self.home_players = self.make_team_entry("Home Sharks")
        self.away_entry, self.away_players = self.make_team_entry("Away Eagles")
        self.pool_match = self.make_match()

        PoolCompetitionTeamStaffAssignment.objects.create(
            competition_entry=self.home_entry,
            user=self.home_coach,
            role=PoolCompetitionTeamStaffAssignment.Role.COACH,
        )
        PoolCompetitionTeamStaffAssignment.objects.create(
            competition_entry=self.away_entry,
            user=self.away_coach,
            role=PoolCompetitionTeamStaffAssignment.Role.CAPTAIN,
        )

    def make_team_entry(self, name, *, player_count=6):
        team = PoolTeam.objects.create(name=name)
        entry = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            team=team,
        )
        players = []
        for position in range(1, player_count + 1):
            player = PoolPlayer.objects.create(full_name=f"{name} Player {position}")
            PoolTeamRoster.objects.create(
                competition_entry=entry,
                player=player,
                sort_order=position,
            )
            players.append(player)
        return entry, players

    def make_match(self, *, race_target=7, table_count=1, published=True):
        if not published:
            self.competition.visibility = PoolCompetition.Visibility.DRAFT
            self.competition.save()
        return PoolMatch.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_entry=self.home_entry,
            away_entry=self.away_entry,
            race_target=race_target,
            table_count=table_count,
            scheduled_at=timezone.now() + timedelta(days=14),
        )

    def lineup_payload(self, players):
        return {
            **{f"starter_{position}": players[position - 1].pk for position in range(1, 6)},
            "substitutes": [players[5].pk],
        }

    def submit_lineup(self, entry, players, *, match=None, actor=None, **kwargs):
        return submit_team_match_lineup(
            match or self.pool_match,
            entry,
            starters={position: players[position - 1] for position in range(1, 6)},
            substitutes=[players[5]],
            actor=actor or self.owner,
            **kwargs,
        )

    def submit_positioned_lineup(
        self,
        entry,
        starters,
        *,
        substitutes=(),
        match=None,
        actor=None,
        **kwargs,
    ):
        """Submit all structural positions while allowing honest vacancies."""

        return submit_team_match_lineup(
            match or self.pool_match,
            entry,
            starters=starters,
            substitutes=substitutes,
            actor=actor or self.owner,
            **kwargs,
        )

    def submit_both_lineups(self, *, match=None):
        pool_match = match or self.pool_match
        self.submit_lineup(self.home_entry, self.home_players, match=pool_match)
        self.submit_lineup(self.away_entry, self.away_players, match=pool_match)

    def start_with_lineups(self, *, match=None):
        pool_match = match or self.pool_match
        self.submit_both_lineups(match=pool_match)
        start_match(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()
        return pool_match

    def expire_team_timer(self, *, match=None):
        """Persist an already-due timer without relying on browser time."""

        pool_match = match or self.pool_match
        started_at = timezone.now() - timedelta(minutes=2)
        PoolMatch.objects.filter(pk=pool_match.pk).update(
            timer_snapshot_at=started_at,
            applied_time_limit_minutes=1,
            timer_started_at=started_at,
            timer_deadline=started_at + timedelta(minutes=1),
        )
        pool_match.refresh_from_db()
        return expire_match_if_due(pool_match, recorded_by=self.owner)

    def test_coach_is_scoped_to_own_entry_and_never_gets_manager_access(self):
        home_url = reverse(
            "pool:team_match_lineup",
            args=[self.pool_match.pk, self.home_entry.pk],
        )
        away_url = reverse(
            "pool:team_match_lineup",
            args=[self.pool_match.pk, self.away_entry.pk],
        )
        manager_url = reverse(
            "pool:manage_match",
            args=[self.competition.slug, self.pool_match.pk],
        )
        start_url = reverse(
            "pool:manage_match_start",
            args=[self.competition.slug, self.pool_match.pk],
        )

        self.assertTrue(
            user_can_manage_team_lineup(
                self.home_coach,
                self.pool_match,
                self.home_entry,
            )
        )
        self.assertFalse(
            user_can_manage_team_lineup(
                self.home_coach,
                self.pool_match,
                self.away_entry,
            )
        )

        self.client.force_login(self.home_coach)
        self.assertEqual(self.client.get(home_url).status_code, 200)
        self.assertEqual(self.client.get(away_url).status_code, 403)
        self.assertEqual(self.client.get(manager_url).status_code, 404)
        self.assertEqual(
            self.client.post(start_url, follow=False).status_code,
            404,
        )

        response = self.client.post(home_url, self.lineup_payload(self.home_players))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            PoolTeamMatchLineup.objects.filter(
                match=self.pool_match,
                team_entry=self.home_entry,
                submitted_by=self.home_coach,
            ).exists()
        )

        # An entry belonging to the competition but not this fixture is not
        # accepted by a modified lineup URL either.
        third_team = PoolTeam.objects.create(name="Third Team")
        third_entry = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            team=third_team,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "pool:team_match_lineup",
                    args=[self.pool_match.pk, third_entry.pk],
                )
            ).status_code,
            404,
        )

    def test_lineup_editability_uses_status_not_scheduled_datetime(self):
        self.pool_match.scheduled_at = timezone.now() - timedelta(days=90)
        self.pool_match.save()
        lineup_url = reverse(
            "pool:team_match_lineup",
            args=[self.pool_match.pk, self.home_entry.pk],
        )
        self.client.force_login(self.home_coach)
        response = self.client.post(lineup_url, self.lineup_payload(self.home_players))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            PoolTeamMatchLineup.objects.filter(
                match=self.pool_match,
                team_entry=self.home_entry,
                submitted_by=self.home_coach,
            ).exists()
        )
        self.submit_lineup(self.home_entry, self.home_players)

        self.pool_match.status = PoolMatch.Status.POSTPONED
        self.pool_match.save()
        self.submit_lineup(self.home_entry, self.home_players)

        self.submit_lineup(self.away_entry, self.away_players)
        start_match(self.pool_match, recorded_by=self.owner)
        self.pool_match.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.LIVE)

        with self.assertRaises(PoolResultValidationError):
            self.submit_lineup(self.home_entry, self.home_players)

        # An organiser can still correct a live sheet before any individual
        # score-sheet row begins. A coach cannot obtain that override.
        self.submit_lineup(
            self.home_entry,
            self.home_players,
            allow_live_correction=True,
        )
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).count(),
            25,
        )
        self.client.force_login(self.home_coach)
        response = self.client.get(lineup_url)
        self.assertContains(response, "This lineup is locked because the fixture is live.")
        self.assertNotContains(response, "scheduled time")

    def test_start_requires_both_valid_structural_lineups(self):
        self.submit_lineup(self.home_entry, self.home_players)
        with self.assertRaises(PoolResultValidationError):
            start_match(self.pool_match, recorded_by=self.owner)
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).exists()
        )

        with self.assertRaises(PoolResultValidationError):
            submit_team_match_lineup(
                self.pool_match,
                self.away_entry,
                starters={position: self.away_players[0] for position in range(1, 6)},
                actor=self.owner,
            )

    def test_three_and_four_player_lineups_start_with_five_structural_positions(self):
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: self.home_players[0],
                2: self.home_players[1],
                3: self.home_players[2],
                4: None,
                5: None,
            },
            substitutes=[self.home_players[5]],
        )
        self.submit_positioned_lineup(
            self.away_entry,
            {
                1: self.away_players[0],
                2: self.away_players[1],
                3: self.away_players[2],
                4: self.away_players[3],
                5: None,
            },
            substitutes=[self.away_players[5]],
        )

        start_match(self.pool_match, recorded_by=self.owner)
        self.pool_match.refresh_from_db()

        self.assertEqual(self.pool_match.status, PoolMatch.Status.LIVE)
        home_lineup = PoolTeamMatchLineup.objects.get(
            match=self.pool_match,
            team_entry=self.home_entry,
        )
        away_lineup = PoolTeamMatchLineup.objects.get(
            match=self.pool_match,
            team_entry=self.away_entry,
        )
        self.assertTrue(home_lineup.is_complete)
        self.assertTrue(away_lineup.is_complete)
        self.assertEqual(home_lineup.filled_position_count, 3)
        self.assertEqual(away_lineup.filled_position_count, 4)
        self.assertEqual(
            dict(home_lineup.positions.values_list("position", "player_id")),
            {
                1: self.home_players[0].pk,
                2: self.home_players[1].pk,
                3: self.home_players[2].pk,
                4: None,
                5: None,
            },
        )
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).count(),
            25,
        )

    def test_lineup_endpoint_accepts_vacant_starting_positions(self):
        lineup_url = reverse(
            "pool:team_match_lineup",
            args=[self.pool_match.pk, self.home_entry.pk],
        )
        payload = self.lineup_payload(self.home_players)
        payload["starter_4"] = ""
        payload["starter_5"] = ""

        self.client.force_login(self.home_coach)
        response = self.client.post(lineup_url, payload)

        self.assertEqual(response.status_code, 302)
        lineup = PoolTeamMatchLineup.objects.get(
            match=self.pool_match,
            team_entry=self.home_entry,
        )
        self.assertEqual(lineup.filled_position_count, 3)
        self.assertIsNone(lineup.positions.get(position=4).player_id)
        self.assertIsNone(lineup.positions.get(position=5).player_id)

    def test_start_creates_exact_persistent_25_rack_score_sheet_idempotently(self):
        self.start_with_lineups()
        schedules = list(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).order_by(
                "sequence_number"
            )
        )

        self.assertEqual(len(schedules), 25)
        self.assertEqual(
            [
                (row.session_number, row.home_position, row.away_position)
                for row in schedules
            ],
            list(TEAM_MATCH_SCORE_SHEET),
        )
        self.assertEqual([row.sequence_number for row in schedules], list(range(1, 26)))
        self.assertTrue(
            all(row.status == PoolTeamMatchScheduledRack.Status.PENDING for row in schedules)
        )
        self.assertTrue(
            all(
                not row.home_player_id
                and not row.away_player_id
                and not row.result_id
                for row in schedules
            )
        )
        saved_ids = [row.pk for row in schedules]

        again = ensure_team_match_schedule(self.pool_match, actor=self.owner)
        self.assertEqual([row.pk for row in again], saved_ids)
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).count(),
            25,
        )
        self.assertEqual(
            PoolTeamMatchAuditEvent.objects.filter(
                match=self.pool_match,
                action=PoolTeamMatchAuditEvent.Action.SCHEDULE_GENERATED,
            ).count(),
            1,
        )

    def test_due_one_sided_vacancy_awards_a_team_point_without_player_attribution(self):
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: None,
                2: self.home_players[1],
                3: self.home_players[2],
                4: self.home_players[3],
                5: self.home_players[4],
            },
        )
        self.submit_lineup(self.away_entry, self.away_players)
        start_match(self.pool_match, recorded_by=self.owner)

        scheduled_rack = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.pool_match.refresh_from_db()
        scheduled_rack.refresh_from_db()

        self.assertEqual(scheduled_rack.sequence_number, 1)
        self.assertEqual(
            scheduled_rack.status,
            PoolTeamMatchScheduledRack.Status.COMPLETED,
        )
        self.assertIsNone(scheduled_rack.home_player_id)
        self.assertEqual(scheduled_rack.away_player_id, self.away_players[0].pk)
        self.assertIsNone(scheduled_rack.table_number)
        self.assertIsNotNone(scheduled_rack.result_id)
        self.assertEqual(
            scheduled_rack.result.outcome_type,
            PoolRack.OutcomeType.LINEUP_VACANCY,
        )
        self.assertEqual(scheduled_rack.result.winner_id, self.away_entry.pk)
        self.assertIsNone(scheduled_rack.result.winning_player_id)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (0, 1))
        scheduled_rack.full_clean()

        home_stats = team_entry_rack_statistics(self.home_entry)
        away_stats = team_entry_rack_statistics(self.away_entry)
        self.assertEqual(away_stats["racks_won"], 1)
        self.assertEqual(away_stats["unattributed_racks_won"], 1)
        self.assertEqual(away_stats["tracked_racks_played"], 0)
        self.assertTrue(
            all(
                row["tracked_racks_played"] == 0
                and row["tracked_racks_won"] == 0
                and row["tracked_racks_lost"] == 0
                for row in (*home_stats["player_rows"], *away_stats["player_rows"])
            )
        )

    def test_due_both_vacant_rack_is_void_and_the_schedule_moves_on(self):
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: None,
                2: self.home_players[1],
                3: self.home_players[2],
                4: self.home_players[3],
                5: self.home_players[4],
            },
        )
        self.submit_positioned_lineup(
            self.away_entry,
            {
                1: None,
                2: self.away_players[1],
                3: self.away_players[2],
                4: self.away_players[3],
                5: self.away_players[4],
            },
        )
        start_match(self.pool_match, recorded_by=self.owner)

        void_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.pool_match.refresh_from_db()
        void_row.refresh_from_db()

        self.assertEqual(void_row.sequence_number, 1)
        self.assertEqual(void_row.status, PoolTeamMatchScheduledRack.Status.VOID)
        self.assertIsNone(void_row.result_id)
        self.assertIsNone(void_row.home_player_id)
        self.assertIsNone(void_row.away_player_id)
        self.assertIsNone(void_row.table_number)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (0, 0))
        self.assertEqual(self.pool_match.racks.count(), 0)
        void_row.full_clean()

        next_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(next_row.sequence_number, 2)
        self.assertEqual(next_row.status, PoolTeamMatchScheduledRack.Status.LIVE)

    def test_vacancy_award_can_finish_the_team_fixture_at_its_race_target(self):
        self.pool_match.race_target = 1
        self.pool_match.save()
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: None,
                2: self.home_players[1],
                3: self.home_players[2],
                4: self.home_players[3],
                5: self.home_players[4],
            },
        )
        self.submit_lineup(self.away_entry, self.away_players)
        start_match(self.pool_match, recorded_by=self.owner)

        winning_vacancy = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.pool_match.refresh_from_db()

        self.assertEqual(
            winning_vacancy.result.outcome_type,
            PoolRack.OutcomeType.LINEUP_VACANCY,
        )
        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(self.pool_match.winner_id, self.away_entry.pk)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (0, 1))
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED,
            ).count(),
            24,
        )

    def test_live_vacancy_fill_preserves_prior_forfeit_and_applies_to_future_racks(self):
        self.pool_match.race_target = 13
        self.pool_match.save()
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: None,
                2: self.home_players[1],
                3: self.home_players[2],
                4: self.home_players[3],
                5: self.home_players[4],
            },
        )
        self.submit_lineup(self.away_entry, self.away_players)
        start_match(self.pool_match, recorded_by=self.owner)

        forfeited_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(
            forfeited_row.result.outcome_type,
            PoolRack.OutcomeType.LINEUP_VACANCY,
        )
        self.assertIsNone(forfeited_row.home_player_id)

        filled_position = fill_team_match_vacancy(
            self.pool_match,
            self.home_entry,
            position=1,
            incoming_player=self.home_players[5],
            actor=self.owner,
            reason="Arrived after the first scheduled rack.",
        )
        forfeited_row.refresh_from_db()
        self.assertEqual(filled_position.player_id, self.home_players[5].pk)
        self.assertTrue(
            PoolTeamMatchLineup.objects.get(
                match=self.pool_match,
                team_entry=self.home_entry,
            ).members.filter(player=self.home_players[5]).exists()
        )
        self.assertIsNone(forfeited_row.home_player_id)
        self.assertEqual(
            forfeited_row.result.outcome_type,
            PoolRack.OutcomeType.LINEUP_VACANCY,
        )
        self.assertFalse(
            PoolTeamMatchSubstitution.objects.filter(match=self.pool_match).exists()
        )

        schedule_state = team_match_schedule_state(self.pool_match)
        next_home_position_one = next(
            row
            for row in schedule_state["pending"]
            if row.home_position == 1
        )
        self.assertEqual(
            next_home_position_one.current_home_player_id,
            self.home_players[5].pk,
        )

        for sequence_number in range(2, 10):
            scheduled_rack = start_next_eligible_team_rack(
                self.pool_match,
                table_number=1,
                actor=self.owner,
            )
            self.assertEqual(scheduled_rack.sequence_number, sequence_number)
            complete_team_match_rack(
                self.pool_match,
                scheduled_rack,
                winning_player=scheduled_rack.home_player,
                actor=self.owner,
            )

        future_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(future_row.sequence_number, 10)
        self.assertEqual(future_row.home_player_id, self.home_players[5].pk)

    def test_live_vacancy_fill_rejects_other_team_unregistered_and_occupied_players(self):
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: None,
                2: self.home_players[1],
                3: self.home_players[2],
                4: self.home_players[3],
                5: self.home_players[4],
            },
        )
        self.submit_lineup(self.away_entry, self.away_players)
        start_match(self.pool_match, recorded_by=self.owner)
        unrelated_player = PoolPlayer.objects.create(full_name="Unrelated arrival")

        with self.assertRaises(PoolResultValidationError):
            fill_team_match_vacancy(
                self.pool_match,
                self.home_entry,
                position=1,
                incoming_player=self.away_players[5],
                actor=self.owner,
            )
        with self.assertRaises(PoolResultValidationError):
            fill_team_match_vacancy(
                self.pool_match,
                self.home_entry,
                position=1,
                incoming_player=unrelated_player,
                actor=self.owner,
            )

        fill_team_match_vacancy(
            self.pool_match,
            self.home_entry,
            position=1,
            incoming_player=self.home_players[5],
            actor=self.owner,
        )
        with self.assertRaises(PoolResultValidationError):
            fill_team_match_vacancy(
                self.pool_match,
                self.home_entry,
                position=1,
                incoming_player=self.home_players[4],
                actor=self.owner,
            )
        self.assertFalse(
            PoolTeamMatchSubstitution.objects.filter(match=self.pool_match).exists()
        )

    def test_exact_match_scorer_can_fill_an_eligible_live_vacancy_only(self):
        self.submit_positioned_lineup(
            self.home_entry,
            {
                1: None,
                2: self.home_players[1],
                3: self.home_players[2],
                4: self.home_players[3],
                5: self.home_players[4],
            },
        )
        self.submit_lineup(self.away_entry, self.away_players)
        start_match(self.pool_match, recorded_by=self.owner)
        PoolMatchScorerAssignment.objects.create(
            match=self.pool_match,
            user=self.outsider,
            assigned_by=self.owner,
        )

        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse(
                "pool:manage_team_match_fill_vacancy",
                args=[
                    self.competition.slug,
                    self.pool_match.pk,
                    self.home_entry.pk,
                ],
            ),
            {
                "position": "1",
                "incoming_player": str(self.home_players[5].pk),
                "reason": "Player has arrived.",
            },
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        lineup = PoolTeamMatchLineup.objects.get(
            match=self.pool_match,
            team_entry=self.home_entry,
        )
        self.assertEqual(
            lineup.positions.get(position=1).player_id,
            self.home_players[5].pk,
        )
        self.assertFalse(
            PoolTeamMatchSubstitution.objects.filter(match=self.pool_match).exists()
        )

    def test_team_scoring_surface_returns_to_match_detail_for_start_and_racks(self):
        self.submit_both_lineups()
        detail_url = reverse("pool:match_detail", args=[self.pool_match.pk])
        self.client.force_login(self.owner)

        started = self.client.post(
            reverse(
                "pool:manage_match_start",
                args=[self.competition.slug, self.pool_match.pk],
            ),
            {"scoring_surface": "match_detail"},
            follow=False,
        )
        self.assertRedirects(started, detail_url)
        detail = self.client.get(detail_url)
        self.assertContains(detail, "Start next rack on table 1")

        rack_started = self.client.post(
            reverse(
                "pool:manage_team_match_rack_start",
                args=[self.competition.slug, self.pool_match.pk],
            ),
            {"table_number": "1", "scoring_surface": "match_detail"},
            follow=False,
        )
        self.assertRedirects(rack_started, detail_url)
        scheduled_rack = team_match_schedule_state(self.pool_match)["live"][0]

        completed = self.client.post(
            reverse(
                "pool:manage_team_match_rack_complete",
                args=[self.competition.slug, self.pool_match.pk, scheduled_rack.pk],
            ),
            {
                "winner_player": scheduled_rack.home_player_id,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "reason": "",
                "scoring_surface": "match_detail",
            },
            follow=False,
        )
        self.assertRedirects(completed, detail_url)
        self.assertContains(self.client.get(detail_url), "Start next rack on table 1")

    def test_substitution_preserves_live_snapshot_and_changes_future_position_only(self):
        self.start_with_lineups()
        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(first.sequence_number, 1)
        self.assertEqual(first.home_player_id, self.home_players[0].pk)
        self.assertEqual(first.away_player_id, self.away_players[0].pk)

        substitution = substitute_team_match_player(
            self.pool_match,
            self.home_entry,
            position=1,
            incoming_player=self.home_players[5],
            actor=self.owner,
            reason="Fresh player",
        )
        self.assertEqual(substitution.outgoing_player_id, self.home_players[0].pk)
        self.assertEqual(substitution.incoming_player_id, self.home_players[5].pk)
        self.assertTrue(
            PoolTeamMatchSubstitution.objects.filter(
                match=self.pool_match,
                position=1,
            ).exists()
        )
        audit = PoolTeamMatchAuditEvent.objects.get(
            match=self.pool_match,
            action=PoolTeamMatchAuditEvent.Action.SUBSTITUTION,
        )
        self.assertEqual(audit.team_entry_id, self.home_entry.pk)
        self.assertEqual(audit.payload["incoming_player_id"], self.home_players[5].pk)

        first.refresh_from_db()
        self.assertEqual(first.home_player_id, self.home_players[0].pk)
        schedule_state = team_match_schedule_state(self.pool_match)
        future_position_one = next(
            row
            for row in schedule_state["pending"]
            if row.sequence_number == 10  # session two: home position one
        )
        self.assertEqual(future_position_one.current_home_player.pk, self.home_players[5].pk)

        # A currently live player cannot immediately re-enter through a
        # different current position. Once their live rack completes, their
        # submitted squad membership keeps re-entry available.
        with self.assertRaises(PoolResultValidationError):
            substitute_team_match_player(
                self.pool_match,
                self.home_entry,
                position=2,
                incoming_player=self.home_players[0],
                actor=self.owner,
            )
        complete_team_match_rack(
            self.pool_match,
            first,
            winning_player=self.home_players[0],
            actor=self.owner,
        )
        substitute_team_match_player(
            self.pool_match,
            self.home_entry,
            position=2,
            incoming_player=self.home_players[0],
            actor=self.owner,
        )

    def test_two_table_operation_never_reuses_an_occupied_table_or_live_player(self):
        self.pool_match.table_count = 2
        self.pool_match.save()
        self.start_with_lineups()

        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        second = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        self.assertEqual((first.sequence_number, second.sequence_number), (1, 2))
        self.assertNotEqual(first.home_player_id, second.home_player_id)
        self.assertNotEqual(first.away_player_id, second.away_player_id)

        with self.assertRaises(PoolResultValidationError):
            start_next_eligible_team_rack(
                self.pool_match,
                table_number=1,
                actor=self.owner,
            )
        with self.assertRaises(PoolResultValidationError):
            start_next_eligible_team_rack(
                self.pool_match,
                table_number=3,
                actor=self.owner,
            )
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.LIVE,
            ).count(),
            2,
        )

    def test_live_player_in_another_team_fixture_is_skipped_globally(self):
        """A shared roster player cannot be double-booked across fixtures."""

        other_entry, other_players = self.make_team_entry("Other Otters")
        other_match = PoolMatch.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_entry=self.home_entry,
            away_entry=other_entry,
            race_target=7,
        )
        self.start_with_lineups()
        self.submit_lineup(self.home_entry, self.home_players, match=other_match)
        self.submit_lineup(other_entry, other_players, match=other_match)
        start_match(other_match, recorded_by=self.owner)

        first_fixture_live = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(first_fixture_live.home_player_id, self.home_players[0].pk)

        other_fixture_live = start_next_eligible_team_rack(
            other_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(other_fixture_live.sequence_number, 2)
        self.assertEqual(other_fixture_live.home_player_id, self.home_players[1].pk)
        self.assertNotEqual(
            other_fixture_live.home_player_id,
            first_fixture_live.home_player_id,
        )

    def test_near_clinch_keeps_configured_table_capacity_and_stops_the_other_table(self):
        self.pool_match.race_target = 3
        self.pool_match.table_count = 2
        self.pool_match.save()
        self.start_with_lineups()

        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        second = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            first,
            winning_player=self.home_players[0],
            actor=self.owner,
        )
        # Two physical tables remain usable while the parent fixture is live.
        third = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        self.assertEqual(third.sequence_number, 3)
        complete_team_match_rack(
            self.pool_match,
            second,
            winning_player=self.home_players[1],
            actor=self.owner,
        )
        self.pool_match.refresh_from_db()
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (2, 0))
        self.assertEqual(self.pool_match.status, PoolMatch.Status.LIVE)

        fourth = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        self.assertEqual(fourth.sequence_number, 4)
        third.refresh_from_db()
        self.assertEqual(third.status, PoolTeamMatchScheduledRack.Status.LIVE)
        self.assertEqual(fourth.status, PoolTeamMatchScheduledRack.Status.LIVE)

        # The first actual clinching completion finalises the parent and
        # discards the still-live rack on the other physical table.
        complete_team_match_rack(
            self.pool_match,
            third,
            winning_player=self.home_players[2],
            actor=self.owner,
        )
        self.pool_match.refresh_from_db()
        fourth.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (3, 0))
        self.assertEqual(fourth.status, PoolTeamMatchScheduledRack.Status.STOPPED)
        self.assertIsNone(fourth.result_id)
        with self.assertRaises(PoolResultValidationError):
            start_next_eligible_team_rack(
                self.pool_match,
                table_number=2,
                actor=self.owner,
            )

    def test_two_table_manager_capacity_is_not_reduced_at_12_8(self):
        """The manager view and service both retain two-table capacity at 12-8."""

        self.pool_match.race_target = 13
        self.pool_match.table_count = 2
        self.pool_match.save()
        self.start_with_lineups()

        for winning_side in (["home"] * 12) + (["away"] * 8):
            scheduled_rack = start_next_eligible_team_rack(
                self.pool_match,
                table_number=1,
                actor=self.owner,
            )
            complete_team_match_rack(
                self.pool_match,
                scheduled_rack,
                winning_player=(
                    scheduled_rack.home_player
                    if winning_side == "home"
                    else scheduled_rack.away_player
                ),
                actor=self.owner,
            )

        self.pool_match.refresh_from_db()
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (12, 8))
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse(
                "pool:manage_match",
                args=[self.competition.slug, self.pool_match.pk],
            )
        )
        self.assertEqual(response.context["available_tables"], [1, 2])

        clinching_rack = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        discarded_rack = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        discarded_snapshots = (
            discarded_rack.home_player_id,
            discarded_rack.away_player_id,
            discarded_rack.table_number,
            discarded_rack.started_at,
        )

        complete_team_match_rack(
            self.pool_match,
            clinching_rack,
            winning_player=clinching_rack.home_player,
            actor=self.owner,
        )

        self.pool_match.refresh_from_db()
        discarded_rack.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (13, 8))
        self.assertEqual(self.pool_match.racks.count(), 21)
        self.assertEqual(discarded_rack.status, PoolTeamMatchScheduledRack.Status.STOPPED)
        self.assertIsNone(discarded_rack.result_id)
        self.assertEqual(
            (
                discarded_rack.home_player_id,
                discarded_rack.away_player_id,
                discarded_rack.table_number,
                discarded_rack.started_at,
            ),
            discarded_snapshots,
        )
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.LIVE,
            ).exists()
        )

        home_stats = team_entry_rack_statistics(self.home_entry)
        away_stats = team_entry_rack_statistics(self.away_entry)
        self.assertEqual((home_stats["racks_won"], away_stats["racks_won"]), (13, 8))
        self.assertEqual((home_stats["racks_lost"], away_stats["racks_lost"]), (8, 13))
        with self.assertRaises(PoolResultValidationError):
            complete_team_match_rack(
                self.pool_match,
                discarded_rack,
                winning_player=discarded_rack.away_player,
                actor=self.owner,
            )
        self.assertEqual(self.pool_match.racks.count(), 21)

    def test_live_correction_cannot_clinch_while_another_table_is_live(self):
        """A correction cannot strand an already-live physical table."""

        self.pool_match.race_target = 4
        self.pool_match.table_count = 2
        self.pool_match.save()
        self.start_with_lineups()

        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        second = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            first,
            winning_player=self.away_players[0],
            actor=self.owner,
        )
        third = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            third,
            winning_player=self.home_players[2],
            actor=self.owner,
        )
        fourth = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            fourth,
            winning_player=self.home_players[3],
            actor=self.owner,
        )
        fifth = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            fifth,
            winning_player=self.home_players[4],
            actor=self.owner,
        )

        self.pool_match.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (3, 1))
        self.assertEqual(second.status, PoolTeamMatchScheduledRack.Status.LIVE)

        with self.assertRaises(PoolResultValidationError):
            correct_completed_team_match_rack(
                self.pool_match,
                first,
                winning_player=self.home_players[0],
                actor=self.owner,
                reason="This would prematurely clinch the team fixture.",
            )

        self.pool_match.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (3, 1))
        self.assertEqual(first.result.winner_id, self.away_entry.pk)
        self.assertEqual(second.status, PoolTeamMatchScheduledRack.Status.LIVE)

    def test_team_staff_cannot_use_scoring_endpoints_but_manager_can(self):
        self.start_with_lineups()
        start_url = reverse(
            "pool:manage_team_match_rack_start",
            args=[self.competition.slug, self.pool_match.pk],
        )
        self.client.force_login(self.home_coach)
        self.assertEqual(
            self.client.post(start_url, {"table_number": 1}).status_code,
            404,
        )

        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(start_url, {"table_number": 1}).status_code,
            302,
        )
        scheduled_rack = PoolTeamMatchScheduledRack.objects.get(
            match=self.pool_match,
            status=PoolTeamMatchScheduledRack.Status.LIVE,
        )
        complete_url = reverse(
            "pool:manage_team_match_rack_complete",
            args=[self.competition.slug, self.pool_match.pk, scheduled_rack.pk],
        )
        response = self.client.post(
            complete_url,
            {
                "winner_player": scheduled_rack.home_player_id,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "reason": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.pool_match.racks.count(), 1)

    def test_clinching_marks_only_pending_rows_not_required_and_creates_no_fake_racks(self):
        self.pool_match.race_target = 2
        self.pool_match.save()
        self.start_with_lineups()

        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            first,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            actor=self.owner,
        )
        second = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            second,
            winning_player=self.home_players[1],
            actor=self.owner,
        )

        self.pool_match.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(self.pool_match.winner_id, self.home_entry.pk)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (2, 0))
        self.assertEqual(self.pool_match.racks.count(), 2)
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED,
            ).count(),
            23,
        )
        with self.assertRaises(PoolResultValidationError):
            start_next_eligible_team_rack(
                self.pool_match,
                table_number=1,
                actor=self.owner,
            )

    def test_time_limit_leader_stops_other_live_table_without_fabricating_a_result(self):
        """Expiry must not leave a stale LIVE row under a finished parent."""

        self.pool_match.table_count = 2
        self.pool_match.save()
        self.start_with_lineups()
        completed_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        stopped_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        stopped_snapshots = (
            stopped_row.home_player_id,
            stopped_row.away_player_id,
            stopped_row.table_number,
            stopped_row.started_at,
        )
        complete_team_match_rack(
            self.pool_match,
            completed_row,
            winning_player=completed_row.home_player,
            actor=self.owner,
        )

        self.expire_team_timer()
        self.pool_match.refresh_from_db()
        stopped_row.refresh_from_db()

        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(
            self.pool_match.completion_reason,
            PoolMatch.CompletionReason.TIME_LIMIT,
        )
        self.assertEqual(
            stopped_row.status,
            PoolTeamMatchScheduledRack.Status.STOPPED,
        )
        self.assertEqual(
            (
                stopped_row.home_player_id,
                stopped_row.away_player_id,
                stopped_row.table_number,
                stopped_row.started_at,
            ),
            stopped_snapshots,
        )
        self.assertIsNone(stopped_row.result_id)
        self.assertEqual(self.pool_match.racks.count(), 1)
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.LIVE,
            ).exists()
        )
        stopped_row.full_clean()
        with self.assertRaises(PoolResultValidationError):
            complete_team_match_rack(
                self.pool_match,
                stopped_row,
                winning_player=stopped_row.home_player,
                actor=self.owner,
            )

    def test_tied_expiry_with_no_live_table_blocks_scoring_and_allows_abandonment(self):
        """A persisted sheet cannot invent a deciding rack after expiry."""

        self.start_with_lineups()
        self.expire_team_timer()
        self.pool_match.refresh_from_db()

        self.assertEqual(self.pool_match.status, PoolMatch.Status.LIVE)
        self.assertTrue(self.pool_match.time_expired_tied)
        with self.assertRaises(PoolResultValidationError) as blocked:
            start_next_eligible_team_rack(
                self.pool_match,
                table_number=1,
                actor=self.owner,
            )
        self.assertIn("no team rack live", str(blocked.exception).lower())
        self.assertEqual(self.pool_match.racks.count(), 0)

        # This is an organiser-only terminal policy in the view layer. The
        # service keeps it narrow: a tied team sheet with no live row may be
        # abandoned, but it cannot create a new deciding table.
        transition_match_status(
            self.pool_match,
            status=PoolMatch.Status.ABANDONED,
            recorded_by=self.owner,
            reason="Timer expired before a deciding table was live.",
        )
        self.pool_match.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.ABANDONED)
        self.assertFalse(self.pool_match.time_expired_tied)
        self.assertEqual(self.pool_match.racks.count(), 0)
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED,
            ).count(),
            25,
        )

    def test_tied_expiry_with_no_live_table_allows_genuine_manager_non_played_result(self):
        self.start_with_lineups()
        self.expire_team_timer()

        record_non_played_result(
            self.pool_match,
            result_method=PoolMatch.ResultMethod.WITHDRAWAL,
            winner=self.home_entry,
            recorded_by=self.owner,
            reason="Away team withdrew after the timer expired tied.",
        )
        self.pool_match.refresh_from_db()

        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(
            self.pool_match.result_method,
            PoolMatch.ResultMethod.WITHDRAWAL,
        )
        self.assertEqual(self.pool_match.winner_id, self.home_entry.pk)
        self.assertEqual(self.pool_match.racks.count(), 0)

    def test_time_limit_correction_to_tie_restores_only_the_stopped_deciding_table(self):
        """A correction cannot restart the timer or manufacture a new table."""

        self.pool_match.table_count = 2
        self.pool_match.save()
        self.start_with_lineups()
        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        deciding_table = start_next_eligible_team_rack(
            self.pool_match,
            table_number=2,
            actor=self.owner,
        )
        deciding_snapshots = (
            deciding_table.home_player_id,
            deciding_table.away_player_id,
            deciding_table.table_number,
            deciding_table.started_at,
        )
        complete_team_match_rack(
            self.pool_match,
            first,
            winning_player=first.home_player,
            actor=self.owner,
        )
        second = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            second,
            winning_player=second.home_player,
            actor=self.owner,
        )
        self.expire_team_timer()
        deciding_table.refresh_from_db()
        self.assertEqual(
            deciding_table.status,
            PoolTeamMatchScheduledRack.Status.STOPPED,
        )

        open_result_correction(
            self.pool_match,
            recorded_by=self.owner,
            reason="Correct the earlier team rack after time-limit completion.",
        )
        correct_completed_team_match_rack(
            self.pool_match,
            first,
            winning_player=first.away_player,
            actor=self.owner,
            reason="The away player won this rack.",
        )
        self.pool_match.refresh_from_db()
        deciding_table.refresh_from_db()

        self.assertEqual(self.pool_match.status, PoolMatch.Status.LIVE)
        self.assertTrue(self.pool_match.time_expired_tied)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (1, 1))
        self.assertEqual(
            deciding_table.status,
            PoolTeamMatchScheduledRack.Status.LIVE,
        )
        self.assertEqual(
            (
                deciding_table.home_player_id,
                deciding_table.away_player_id,
                deciding_table.table_number,
                deciding_table.started_at,
            ),
            deciding_snapshots,
        )
        self.assertEqual(
            PoolTeamMatchScheduledRack.objects.filter(
                match=self.pool_match,
                status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED,
            ).count(),
            22,
        )

        complete_team_match_rack(
            self.pool_match,
            deciding_table,
            winning_player=deciding_table.home_player,
            actor=self.owner,
        )
        self.pool_match.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(
            self.pool_match.completion_reason,
            PoolMatch.CompletionReason.TIME_LIMIT,
        )
        self.assertEqual(self.pool_match.racks.count(), 3)

    def test_scheduled_rack_rejects_outsider_and_completion_retry_is_idempotent(self):
        self.start_with_lineups()
        scheduled_rack = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        outsider_player = PoolPlayer.objects.create(full_name="Unrelated Player")

        with self.assertRaises(PoolResultValidationError):
            complete_team_match_rack(
                self.pool_match,
                scheduled_rack,
                winning_player=outsider_player,
                actor=self.owner,
            )
        self.assertFalse(self.pool_match.racks.exists())

        completed = complete_team_match_rack(
            self.pool_match,
            scheduled_rack,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            actor=self.owner,
        )
        retried = complete_team_match_rack(
            self.pool_match,
            completed,
            winning_player=self.home_players[0],
            actor=self.owner,
        )
        self.assertEqual(retried.pk, completed.pk)
        self.assertEqual(self.pool_match.racks.count(), 1)

        with self.assertRaises(PoolResultValidationError):
            record_rack(
                self.pool_match,
                winning_player=self.home_players[0],
                recorded_by=self.owner,
            )

    def test_live_legacy_team_fixture_without_schedule_keeps_generic_scoring_path(self):
        """Phase-two fixtures are not retrofitted with a score sheet on read."""

        self.pool_match.status = PoolMatch.Status.LIVE
        self.pool_match.save()
        rack = record_rack(
            self.pool_match,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        self.pool_match.refresh_from_db()
        self.assertEqual(rack.winner_id, self.home_entry.pk)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (1, 0))
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).exists()
        )

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse(
                "pool:manage_match",
                args=[self.competition.slug, self.pool_match.pk],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Record rack")
        self.assertNotContains(response, "Fixed rack schedule")

    def test_non_played_result_cannot_strand_a_live_scheduled_table(self):
        self.start_with_lineups()
        live_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        with self.assertRaises(PoolResultValidationError):
            record_non_played_result(
                self.pool_match,
                result_method=PoolMatch.ResultMethod.WALKOVER,
                winner=self.home_entry,
                recorded_by=self.owner,
                reason="This must not strand a live table.",
            )
        self.pool_match.refresh_from_db()
        live_row.refresh_from_db()
        self.assertEqual(self.pool_match.status, PoolMatch.Status.LIVE)
        self.assertEqual(live_row.status, PoolTeamMatchScheduledRack.Status.LIVE)

    def test_scheduled_rack_correction_updates_tracked_player_statistics(self):
        self.start_with_lineups()
        scheduled_rack = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            scheduled_rack,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            actor=self.owner,
        )
        before_home = {
            row["player"].pk: row
            for row in team_entry_rack_statistics(self.home_entry)["player_rows"]
        }
        self.assertEqual(before_home[self.home_players[0].pk]["tracked_racks_won"], 1)

        correct_completed_team_match_rack(
            self.pool_match,
            scheduled_rack,
            winning_player=self.away_players[0],
            outcome_type=PoolRack.OutcomeType.RED_APPLE,
            actor=self.owner,
            reason="Correct scorecard winner",
        )
        self.pool_match.refresh_from_db()
        home_rows = {
            row["player"].pk: row
            for row in team_entry_rack_statistics(self.home_entry)["player_rows"]
        }
        away_rows = {
            row["player"].pk: row
            for row in team_entry_rack_statistics(self.away_entry)["player_rows"]
        }
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (0, 1))
        self.assertEqual(home_rows[self.home_players[0].pk]["tracked_racks_played"], 1)
        self.assertEqual(home_rows[self.home_players[0].pk]["tracked_racks_lost"], 1)
        self.assertEqual(away_rows[self.away_players[0].pk]["tracked_racks_won"], 1)
        self.assertEqual(away_rows[self.away_players[0].pk]["red_apples"], 1)

    def test_staff_assignment_routes_are_manager_scoped_and_keep_anonymous_next(self):
        staff_url = reverse(
            "pool:manage_competition_team_staff",
            args=[self.competition.slug, self.home_entry.pk],
        )
        add_url = reverse(
            "pool:manage_competition_team_staff_add",
            args=[self.competition.slug, self.home_entry.pk],
        )
        anonymous = self.client.get(staff_url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("next=", anonymous["Location"])

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(staff_url).status_code, 404)

        pool_admin = User.objects.create_user(
            username="team-operations-admin",
            password="test-password",
        )
        PoolCompetitionMembership.objects.create(
            competition=self.competition,
            user=pool_admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        self.client.force_login(pool_admin)
        self.assertEqual(self.client.get(staff_url).status_code, 200)
        staff_user = User.objects.create_user(
            username="team-operations-new-captain",
            password="test-password",
        )
        response = self.client.post(
            add_url,
            {
                "identifier": staff_user.username,
                "role": PoolCompetitionTeamStaffAssignment.Role.CAPTAIN,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            PoolCompetitionTeamStaffAssignment.objects.filter(
                competition_entry=self.home_entry,
                user=staff_user,
                role=PoolCompetitionTeamStaffAssignment.Role.CAPTAIN,
                is_active=True,
            ).exists()
        )

        superuser = User.objects.create_superuser(
            username="team-operations-superuser",
            email="team-operations-superuser@example.test",
            password="test-password",
        )
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(staff_url).status_code, 200)

    def test_superuser_parent_delete_collects_team_operation_children(self):
        self.start_with_lineups()
        scheduled_rack = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            scheduled_rack,
            winning_player=self.home_players[0],
            actor=self.owner,
        )
        lineup_ids = list(
            PoolTeamMatchLineup.objects.filter(match=self.pool_match).values_list("pk", flat=True)
        )
        scheduled_ids = list(
            PoolTeamMatchScheduledRack.objects.filter(match=self.pool_match).values_list("pk", flat=True)
        )
        audit_ids = list(
            PoolTeamMatchAuditEvent.objects.filter(match=self.pool_match).values_list("pk", flat=True)
        )
        superuser = User.objects.create_superuser(
            username="team-operation-delete-superuser",
            email="team-operation-delete-superuser@example.test",
            password="test-password",
        )
        self.client.force_login(superuser)
        delete_url = reverse("admin:pool_poolcompetition_delete", args=[self.competition.pk])
        confirmation = self.client.get(delete_url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertFalse(confirmation.context["perms_lacking"])
        self.assertFalse(confirmation.context["protected"])
        deleted = self.client.post(delete_url, {"post": "yes"})
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(PoolCompetition.objects.filter(pk=self.competition.pk).exists())
        self.assertFalse(PoolTeamMatchLineup.objects.filter(pk__in=lineup_ids).exists())
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(pk__in=scheduled_ids).exists()
        )
        self.assertFalse(PoolTeamMatchAuditEvent.objects.filter(pk__in=audit_ids).exists())

    def test_tracked_stats_use_both_snapshotted_players_without_fabricating_legacy_losses(self):
        self.start_with_lineups()
        first = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            first,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            actor=self.owner,
        )
        second = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            self.pool_match,
            second,
            winning_player=self.away_players[1],
            outcome_type=PoolRack.OutcomeType.RED_APPLE,
            actor=self.owner,
        )

        # Simulate a winner-only imported record. Its legitimate win remains
        # visible, but no made-up opposing player loss or denominator appears.
        PoolRack.objects.bulk_create(
            [
                PoolRack(
                    match=self.pool_match,
                    rack_number=99,
                    winner=self.home_entry,
                    winning_player=self.home_players[0],
                    outcome_type=PoolRack.OutcomeType.NORMAL,
                )
            ]
        )

        home_stats = team_entry_rack_statistics(self.home_entry)
        away_stats = team_entry_rack_statistics(self.away_entry)
        home_rows = {row["player"].pk: row for row in home_stats["player_rows"]}
        away_rows = {row["player"].pk: row for row in away_stats["player_rows"]}

        self.assertEqual(home_rows[self.home_players[0].pk]["racks_won"], 2)
        self.assertEqual(home_rows[self.home_players[0].pk]["tracked_racks_played"], 1)
        self.assertEqual(home_rows[self.home_players[0].pk]["tracked_racks_won"], 1)
        self.assertEqual(home_rows[self.home_players[0].pk]["tracked_racks_lost"], 0)
        self.assertEqual(home_rows[self.home_players[0].pk]["tracked_rack_win_percentage"], 100.0)
        self.assertEqual(home_rows[self.home_players[1].pk]["tracked_racks_played"], 1)
        self.assertEqual(home_rows[self.home_players[1].pk]["tracked_racks_lost"], 1)
        self.assertEqual(away_rows[self.away_players[0].pk]["tracked_racks_lost"], 1)
        self.assertEqual(away_rows[self.away_players[1].pk]["tracked_racks_won"], 1)
        self.assertEqual(home_stats["tracked_racks_played"], 2)
        self.assertEqual(home_stats["tracked_racks_won"], 1)
        self.assertEqual(home_stats["tracked_racks_lost"], 1)

    def test_public_team_match_shows_schedule_without_manager_controls_and_draft_is_private(self):
        self.start_with_lineups()
        live_row = start_next_eligible_team_rack(
            self.pool_match,
            table_number=1,
            actor=self.owner,
        )
        public_url = reverse("pool:match_detail", args=[self.pool_match.pk])

        self.client.logout()
        response = self.client.get(public_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Team lineups")
        self.assertContains(response, "Live tables")
        self.assertContains(response, live_row.home_player.full_name)
        self.assertContains(response, "Table 1")
        self.assertNotContains(response, "Complete rack")
        self.assertNotContains(response, "Save substitution")

        self.competition.visibility = PoolCompetition.Visibility.DRAFT
        self.competition.save()
        self.assertEqual(self.client.get(public_url).status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(public_url).status_code, 200)

    def test_public_lineup_keeps_starting_snapshot_after_substitution(self):
        self.start_with_lineups()
        substitute_team_match_player(
            self.pool_match,
            self.home_entry,
            position=1,
            incoming_player=self.home_players[5],
            actor=self.owner,
        )
        current_position = PoolTeamMatchLineup.objects.get(
            match=self.pool_match, team_entry=self.home_entry
        ).positions.get(position=1)
        self.assertEqual(current_position.player_id, self.home_players[5].pk)

        self.client.logout()
        response = self.client.get(reverse("pool:match_detail", args=[self.pool_match.pk]))

        self.assertEqual(response.status_code, 200)
        home_card = next(
            card for card in response.context["team_lineup_cards"]
            if card["entry"].pk == self.home_entry.pk
        )
        self.assertEqual(
            home_card["starting_positions"][0]["player"].pk,
            self.home_players[0].pk,
        )
        self.assertEqual(
            [player.pk for player in home_card["submitted_substitutes"]],
            [self.home_players[5].pk],
        )
        self.assertContains(response, "Starting lineup")
        self.assertContains(response, self.home_players[0].full_name)
        self.assertContains(
            response,
            f"{self.home_players[0].full_name} → {self.home_players[5].full_name}",
            html=False,
        )
