"""Focused coverage for connected football knockout bracket progression."""

from unittest.mock import patch
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import ActivityLog, LeagueMembership
from leagues.models import Competition, Stage
from matches.bracket import (
    BracketCorrectionBlocked,
    advance_completed_tie_for_match,
    create_bracket_structure,
    generate_bracket_matches,
    save_bracket_links,
    save_manual_seeding,
    save_bracket_schedule,
)
from matches.models import (
    FootballKnockoutRoundRule,
    FootballKnockoutTie,
    FootballKnockoutTieSlot,
    Goal,
    Match,
    MatchLineup,
)
from matches.services import apply_official_match_result, end_regulation
from predictions.models import Prediction, PredictionMatch
from teams.models import Team, TeamCoachAssignment


User = get_user_model()


class KnockoutPhaseTwoTests(TestCase):
    """The bracket service is exercised independently from asynchronous tasks."""

    def setUp(self):
        self.sequence = 0
        self.manager = User.objects.create_user(
            username="phase-two-manager",
            password="pass",
        )
        self.superuser = User.objects.create_superuser(
            username="phase-two-superuser",
            email="phase-two-superuser@example.test",
            password="pass",
        )

    def _new_competition(self, *, size=4):
        self.sequence += 1
        competition = Competition.objects.create(
            name=f"Phase Two Competition {self.sequence}",
            slug=f"phase-two-{self.sequence}",
            season="2026",
            format_type="KNOCKOUT",
            knockout_size=size,
        )
        LeagueMembership.objects.create(
            user=self.manager,
            competition=competition,
            role=LeagueMembership.Role.OWNER,
        )
        return competition

    def _build_empty_bracket(
        self,
        *,
        size=4,
        legs=(FootballKnockoutRoundRule.Legs.ONE, FootballKnockoutRoundRule.Legs.ONE),
        policies=None,
        team_count=None,
    ):
        competition = self._new_competition(size=size)
        policies = policies or (
            FootballKnockoutRoundRule.DeciderPolicy.EXTRA_TIME_THEN_PENALTIES,
        ) * len(legs)
        stages = []
        for index, (number_of_legs, policy) in enumerate(zip(legs, policies), start=1):
            stage = Stage.objects.create(
                competition=competition,
                name=("Semi-finals" if index == 1 and len(legs) == 2 else "Final"),
                stage_type="KNOCKOUT",
                order=index,
            )
            FootballKnockoutRoundRule.objects.create(
                stage=stage,
                number_of_legs=number_of_legs,
                decider_policy=policy,
            )
            stages.append(stage)
        teams = [
            Team.objects.create(
                competition=competition,
                name=f"Phase Two Team {self.sequence}-{index}",
            )
            for index in range(1, (team_count or size) + 1)
        ]
        create_bracket_structure(competition)
        return competition, stages, teams

    def _seed_and_generate(self, **kwargs):
        competition, stages, teams = self._build_empty_bracket(**kwargs)
        slots = list(
            FootballKnockoutTieSlot.objects.filter(
                tie__stage=stages[0],
                tie__position__isnull=False,
            ).order_by("tie__position", "slot")
        )
        selections = {
            slot.id: (teams[index] if index < len(teams) else None)
            for index, slot in enumerate(slots)
        }
        save_manual_seeding(competition, selections)
        self._save_future_schedule(competition)
        generate_bracket_matches(competition)
        return competition, stages, teams

    def _save_future_schedule(self, competition):
        schedules = {}
        kickoff = timezone.now() + timedelta(days=1)
        ties = list(
            FootballKnockoutTie.objects.filter(
                stage__competition=competition,
                position__isnull=False,
            ).select_related("stage", "stage__football_knockout_rule").order_by(
                "stage__order", "position", "id"
            )
        )
        for index, tie in enumerate(ties):
            first_leg = kickoff + timedelta(days=index * 3)
            values = {1: first_leg}
            if tie.stage.football_knockout_rule.number_of_legs == 2:
                values[2] = first_leg + timedelta(days=1)
            schedules[tie.id] = values
        save_bracket_schedule(competition, schedules)
        return schedules

    @staticmethod
    def _tie_matches(tie):
        return list(tie.matches.order_by("leg", "id"))

    @staticmethod
    def _score_for_winner(match, winner, *, winning_score=2, losing_score=0):
        if match.home_team_id == winner.id:
            return winning_score, losing_score
        return losing_score, winning_score

    def _finish_with_winner(self, match, winner):
        home_score, away_score = self._score_for_winner(match, winner)
        return apply_official_match_result(
            match,
            home_score=home_score,
            away_score=away_score,
            regulation_home_score=home_score,
            regulation_away_score=away_score,
        )

    def _first_round_ties(self, competition):
        return list(
            FootballKnockoutTie.objects.filter(
                stage__competition=competition,
                position__isnull=False,
            ).order_by("stage__order", "position")[:2]
        )

    def _ready_final(self):
        competition, stages, teams = self._seed_and_generate()
        first_tie, second_tie = self._first_round_ties(competition)
        source_match = self._tie_matches(first_tie)[0]
        other_match = self._tie_matches(second_tie)[0]
        source_winner = source_match.home_team
        self._finish_with_winner(source_match, source_winner)
        self._finish_with_winner(other_match, other_match.home_team)
        final_tie = FootballKnockoutTie.objects.get(stage=stages[-1], position=1)
        final_match = self._tie_matches(final_tie)[0]
        return source_match, source_winner, final_tie, final_match

    def _correct_source_to_other_winner(self, source_match):
        source_match.refresh_from_db()
        replacement = source_match.away_team
        self._finish_with_winner(source_match, replacement)
        return replacement

    def _assert_downstream_correction_blocked(self, dependency):
        source_match, original_winner, final_tie, final_match = self._ready_final()

        if dependency == "goal":
            final_match.status = "LIVE"
            final_match.phase = "FIRST_HALF"
            final_match.save(update_fields=["status", "phase"])
            Goal.objects.create(
                match=final_match,
                team=final_match.home_team,
                player_name="Bracket goal",
            )
            Match.objects.filter(pk=final_match.pk).update(
                status="UPCOMING",
                phase="NONE",
                lineups_locked=False,
            )
        elif dependency == "lineup":
            MatchLineup.objects.create(
                match=final_match,
                team=final_match.home_team,
                submitted_by=self.manager,
                submitted_at=timezone.now(),
            )
        elif dependency == "prediction":
            prediction_match, _ = PredictionMatch.objects.get_or_create(
                match=final_match,
                defaults={"competition": final_match.competition},
            )
            Prediction.objects.create(
                user=self.manager,
                prediction_match=prediction_match,
                predicted_result="H",
                predicted_home_goals=1,
                predicted_away_goals=0,
            )
        elif dependency == "live":
            final_match.status = "LIVE"
            final_match.phase = "FIRST_HALF"
            final_match.save(update_fields=["status", "phase"])
        elif dependency == "finished":
            Match.objects.filter(pk=final_match.pk).update(
                status="FINISHED",
                phase="NONE",
            )
        else:
            raise AssertionError(f"Unknown dependency: {dependency}")

        with self.assertRaises(BracketCorrectionBlocked):
            self._correct_source_to_other_winner(source_match)

        source_match.refresh_from_db()
        final_tie.refresh_from_db()
        self.assertEqual(source_match.home_team_id, original_winner.id)
        self.assertEqual(final_tie.winner_id, None)

    def test_single_leg_regulation_winner_advances_to_home_slot(self):
        competition, stages, _ = self._seed_and_generate()
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]

        self._finish_with_winner(match, match.home_team)

        first_tie.refresh_from_db()
        final_tie = FootballKnockoutTie.objects.get(stage=stages[-1], position=1)
        home_slot = final_tie.bracket_slots.get(slot="HOME")
        self.assertEqual(first_tie.winner_id, match.home_team_id)
        self.assertEqual(home_slot.team_id, match.home_team_id)
        self.assertFalse(final_tie.matches.exists())

    def test_normal_match_completion_path_advances_synchronously(self):
        competition, _, _ = self._seed_and_generate()
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]
        match.status = "LIVE"
        match.phase = "SECOND_HALF"
        match.home_score = 2
        match.away_score = 0
        match.save(update_fields=["status", "phase", "home_score", "away_score"])

        end_regulation(match)

        first_tie.refresh_from_db()
        self.assertEqual(first_tie.winner_id, match.home_team_id)

    def test_extra_time_winner_advances(self):
        competition, _, _ = self._seed_and_generate()
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]

        apply_official_match_result(
            match,
            home_score=2,
            away_score=1,
            regulation_home_score=1,
            regulation_away_score=1,
        )

        first_tie.refresh_from_db()
        self.assertEqual(first_tie.winner_id, match.home_team_id)
        self.assertEqual(
            first_tie.winner_resolution_method,
            FootballKnockoutTie.WinnerResolutionMethod.EXTRA_TIME,
        )

    def test_penalty_winner_advances(self):
        competition, _, _ = self._seed_and_generate()
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]

        apply_official_match_result(
            match,
            home_score=1,
            away_score=1,
            regulation_home_score=1,
            regulation_away_score=1,
            penalty_home_score=5,
            penalty_away_score=4,
        )

        first_tie.refresh_from_db()
        self.assertEqual(first_tie.winner_id, match.home_team_id)
        self.assertEqual(
            first_tie.winner_resolution_method,
            FootballKnockoutTie.WinnerResolutionMethod.PENALTIES,
        )

    def test_direct_penalty_winner_advances(self):
        policy = FootballKnockoutRoundRule.DeciderPolicy.DIRECT_TO_PENALTIES
        competition, _, _ = self._seed_and_generate(policies=(policy, policy))
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]

        apply_official_match_result(
            match,
            home_score=0,
            away_score=0,
            regulation_home_score=0,
            regulation_away_score=0,
            penalty_home_score=3,
            penalty_away_score=4,
        )

        first_tie.refresh_from_db()
        self.assertEqual(first_tie.winner_id, match.away_team_id)
        self.assertEqual(first_tie.winner_resolution_method, "PENALTIES")

    def test_two_leg_aggregate_uses_reversed_home_away_and_first_leg_does_not_advance(self):
        competition, stages, _ = self._seed_and_generate(
            legs=(FootballKnockoutRoundRule.Legs.TWO, FootballKnockoutRoundRule.Legs.ONE)
        )
        first_tie, _ = self._first_round_ties(competition)
        first_leg, second_leg = self._tie_matches(first_tie)
        winner = first_leg.home_team

        self._finish_with_winner(first_leg, winner)
        first_tie.refresh_from_db()
        self.assertIsNone(first_tie.winner_id)
        self.assertFalse(FootballKnockoutTie.objects.get(stage=stages[-1], position=1).matches.exists())

        self._finish_with_winner(second_leg, winner)
        first_tie.refresh_from_db()
        final_tie = FootballKnockoutTie.objects.get(stage=stages[-1], position=1)
        self.assertNotEqual(second_leg.home_team_id, winner.id)
        self.assertEqual(first_tie.winner_id, winner.id)
        self.assertEqual(first_tie.winner_resolution_method, "AGGREGATE")
        self.assertEqual(final_tie.bracket_slots.get(slot="HOME").team_id, winner.id)

    def test_second_source_advances_to_away_slot(self):
        competition, stages, _ = self._seed_and_generate()
        _, second_tie = self._first_round_ties(competition)
        match = self._tie_matches(second_tie)[0]

        self._finish_with_winner(match, match.away_team)

        final_tie = FootballKnockoutTie.objects.get(stage=stages[-1], position=1)
        self.assertEqual(final_tie.bracket_slots.get(slot="AWAY").team_id, match.away_team_id)

    def test_repeated_advancement_is_idempotent(self):
        competition, _, _ = self._seed_and_generate()
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]
        self._finish_with_winner(match, match.home_team)
        log_count = ActivityLog.objects.filter(
            competition=competition,
            action=ActivityLog.Action.KNOCKOUT_WINNER_ADVANCED,
        ).count()

        advance_completed_tie_for_match(match)

        self.assertEqual(
            ActivityLog.objects.filter(
                competition=competition,
                action=ActivityLog.Action.KNOCKOUT_WINNER_ADVANCED,
            ).count(),
            log_count,
        )
        self.assertEqual(Match.objects.filter(knockout_tie=first_tie).count(), 1)

    def test_final_declares_champion_once(self):
        _, _, final_tie, final_match = self._ready_final()
        self._finish_with_winner(final_match, final_match.away_team)

        final_tie.refresh_from_db()
        champion_logs = ActivityLog.objects.filter(
            action=ActivityLog.Action.KNOCKOUT_CHAMPION_DECLARED,
            competition=final_match.competition,
        )
        self.assertEqual(final_tie.winner_id, final_match.away_team_id)
        self.assertEqual(champion_logs.count(), 1)
        advance_completed_tie_for_match(final_match)
        self.assertEqual(champion_logs.count(), 1)

    def test_bye_advances_without_creating_fake_match_or_statistics(self):
        competition, stages, teams = self._seed_and_generate(team_count=3)
        first_ties = self._first_round_ties(competition)
        bye_tie = next(tie for tie in first_ties if tie.is_bye)
        bye_team = next(slot.team for slot in bye_tie.bracket_slots.all() if slot.team_id)

        bye_tie.refresh_from_db()
        final_tie = FootballKnockoutTie.objects.get(stage=stages[-1], position=1)
        self.assertEqual(bye_tie.winner_id, bye_team.id)
        self.assertEqual(bye_tie.winner_resolution_method, "BYE")
        self.assertFalse(bye_tie.matches.exists())
        self.assertFalse(Goal.objects.filter(match__knockout_tie=bye_tie).exists())
        self.assertTrue(
            final_tie.bracket_slots.filter(team=bye_team).exists(),
            "The real entrant, not a fake score, must occupy the next round slot.",
        )

    def test_corrected_result_with_same_winner_leaves_downstream_fixture_intact(self):
        source_match, source_winner, final_tie, final_match = self._ready_final()
        before_teams = (final_match.home_team_id, final_match.away_team_id)

        source_match.refresh_from_db()
        home_score, away_score = self._score_for_winner(
            source_match,
            source_winner,
            winning_score=4,
            losing_score=1,
        )
        apply_official_match_result(
            source_match,
            home_score=home_score,
            away_score=away_score,
            regulation_home_score=home_score,
            regulation_away_score=away_score,
        )

        final_tie.refresh_from_db()
        final_match.refresh_from_db()
        self.assertEqual(final_tie.winner_id, None)
        self.assertEqual((final_match.home_team_id, final_match.away_team_id), before_teams)

    def test_corrected_result_replaces_unstarted_downstream_team(self):
        source_match, _, final_tie, final_match = self._ready_final()
        replacement = self._correct_source_to_other_winner(source_match)

        final_tie.refresh_from_db()
        final_match.refresh_from_db()
        self.assertEqual(final_tie.bracket_slots.get(slot="HOME").team_id, replacement.id)
        self.assertIn(replacement.id, (final_match.home_team_id, final_match.away_team_id))
        self.assertEqual(final_match.status, "UPCOMING")

    def test_correction_is_blocked_after_downstream_goal(self):
        self._assert_downstream_correction_blocked("goal")

    def test_correction_is_blocked_after_lineup_submission(self):
        self._assert_downstream_correction_blocked("lineup")

    def test_correction_is_blocked_after_prediction_submission(self):
        self._assert_downstream_correction_blocked("prediction")

    def test_correction_is_blocked_after_destination_goes_live(self):
        self._assert_downstream_correction_blocked("live")

    def test_correction_is_blocked_after_destination_finishes(self):
        self._assert_downstream_correction_blocked("finished")

    def test_manager_result_correction_records_a_blocked_bracket_audit_entry(self):
        source_match, _, _, final_match = self._ready_final()
        prediction_match, _ = PredictionMatch.objects.get_or_create(
            match=final_match,
            defaults={"competition": final_match.competition},
        )
        Prediction.objects.create(
            user=self.manager,
            prediction_match=prediction_match,
            predicted_result="H",
            predicted_home_goals=1,
            predicted_away_goals=0,
        )
        self.client.force_login(self.manager)
        url = reverse(
            "league_manager:edit_finished_match",
            args=[source_match.competition.slug, source_match.pk],
        )
        source_match.refresh_from_db()
        replacement_home, replacement_away = self._score_for_winner(
            source_match, source_match.away_team,
        )
        response = self.client.post(url, {
            "version": self.client.get(url).context["version"],
            "home_score": replacement_home,
            "away_score": replacement_away,
            "regulation_home_score": replacement_home,
            "regulation_away_score": replacement_away,
        })

        self.assertContains(response, "Downstream data was left unchanged")
        self.assertEqual(
            ActivityLog.objects.filter(
                competition=source_match.competition,
                action=ActivityLog.Action.BRACKET_CORRECTION_BLOCKED,
            ).count(),
            1,
        )
        source_match.refresh_from_db()
        self.assertEqual(source_match.home_score, 2)

    def test_public_bracket_display_shows_explicit_slots(self):
        competition, _, _ = self._seed_and_generate()
        from leagues.views import _connected_bracket_display

        rounds, champion = _connected_bracket_display(competition)

        self.assertEqual(len(rounds), 2)
        self.assertIsNone(champion)
        self.assertEqual(rounds[-1]["ties"][0]["matches"], [])
        self.assertIsNotNone(rounds[0]["ties"][0]["home_slot"])

    def test_fixture_generation_requires_explicit_future_kickoffs(self):
        competition, stages, teams = self._build_empty_bracket()
        slots = list(
            FootballKnockoutTieSlot.objects.filter(
                tie__stage=stages[0],
            ).order_by("tie__position", "slot")
        )
        save_manual_seeding(
            competition,
            {slot.id: teams[index] for index, slot in enumerate(slots)},
        )

        with self.assertRaises(ValidationError):
            generate_bracket_matches(competition)
        self.assertFalse(Match.objects.filter(competition=competition).exists())

        schedules = self._save_future_schedule(competition)
        generate_bracket_matches(competition)
        first_tie = FootballKnockoutTie.objects.get(stage=stages[0], position=1)
        match = self._tie_matches(first_tie)[0]
        self.assertEqual(match.match_date, schedules[first_tie.id][1])
        self.assertGreater(match.match_date, timezone.now())

    def test_bracket_link_slots_can_be_swapped_before_fixture_generation(self):
        competition, stages, _ = self._build_empty_bracket()
        source_one, source_two = FootballKnockoutTie.objects.filter(
            stage=stages[0],
        ).order_by("position")
        final_tie = FootballKnockoutTie.objects.get(stage=stages[1], position=1)

        save_bracket_links(
            competition,
            {
                source_one.id: (final_tie, FootballKnockoutTie.DestinationSlot.AWAY),
                source_two.id: (final_tie, FootballKnockoutTie.DestinationSlot.HOME),
                final_tie.id: None,
            },
        )

        source_one.refresh_from_db()
        source_two.refresh_from_db()
        self.assertEqual(source_one.winner_destination_slot, "AWAY")
        self.assertEqual(source_two.winner_destination_slot, "HOME")

    def test_connected_round_leg_configuration_locks_after_fixture_generation(self):
        _, stages, _ = self._seed_and_generate()
        rule = FootballKnockoutRoundRule.objects.get(stage=stages[0])
        rule.number_of_legs = FootballKnockoutRoundRule.Legs.TWO

        with self.assertRaises(ValidationError):
            rule.save()

    def test_manual_seeding_rejects_duplicate_team(self):
        competition, stages, teams = self._build_empty_bracket()
        slots = list(
            FootballKnockoutTieSlot.objects.filter(tie__stage=stages[0]).order_by(
                "tie__position", "slot"
            )
        )
        selections = {
            slots[0].id: teams[0],
            slots[1].id: teams[0],
            slots[2].id: teams[2],
            slots[3].id: teams[3],
        }

        with self.assertRaises(ValidationError):
            save_manual_seeding(competition, selections)

    def test_cross_competition_link_is_rejected(self):
        first_competition, _, _ = self._build_empty_bracket()
        second_competition, _, _ = self._build_empty_bracket()
        source = FootballKnockoutTie.objects.filter(
            stage__competition=first_competition,
            position=1,
        ).first()
        destination = FootballKnockoutTie.objects.filter(
            stage__competition=second_competition,
            position=1,
        ).first()
        source.winner_destination = destination
        source.winner_destination_slot = "HOME"

        with self.assertRaises(ValidationError):
            source.full_clean()

    def test_bracket_management_permissions_and_creation_view(self):
        competition = self._new_competition()
        stage_one = Stage.objects.create(
            competition=competition,
            name="Semi-finals",
            stage_type="KNOCKOUT",
            order=1,
        )
        stage_two = Stage.objects.create(
            competition=competition,
            name="Final",
            stage_type="KNOCKOUT",
            order=2,
        )
        for stage in (stage_one, stage_two):
            FootballKnockoutRoundRule.objects.create(stage=stage)
        teams = [
            Team.objects.create(
                competition=competition,
                name=f"Permission Team {self.sequence}-{index}",
            )
            for index in range(1, 5)
        ]
        url = reverse("league_manager:knockout_bracket", args=[competition.slug])
        owner = self.manager
        admin = User.objects.create_user(username="phase-two-admin", password="pass")
        agent = User.objects.create_user(username="phase-two-agent", password="pass")
        coach = User.objects.create_user(username="phase-two-coach", password="pass")
        LeagueMembership.objects.create(
            user=admin,
            competition=competition,
            role=LeagueMembership.Role.ADMIN,
        )
        LeagueMembership.objects.create(
            user=agent,
            competition=competition,
            role=LeagueMembership.Role.AGENT,
        )
        TeamCoachAssignment.objects.create(team=teams[0], user=coach)

        for allowed_user in (self.superuser, owner, admin):
            self.client.force_login(allowed_user)
            self.assertEqual(self.client.get(url).status_code, 200)

        for rejected_user in (agent, coach):
            self.client.force_login(rejected_user)
            self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(owner)
        response = self.client.post(url, {"action": "create"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            FootballKnockoutTie.objects.filter(
                stage__competition=competition,
                position__isnull=False,
            ).exists()
        )

    def test_progression_works_without_celery_or_redis(self):
        competition, _, _ = self._seed_and_generate()
        first_tie, _ = self._first_round_ties(competition)
        match = self._tie_matches(first_tie)[0]

        with patch(
            "matches.tasks.recalculate_match_after_result_task.delay",
            side_effect=AssertionError("Celery must not be required for advancement"),
        ) as delay:
            self._finish_with_winner(match, match.home_team)

        first_tie.refresh_from_db()
        delay.assert_not_called()
        self.assertEqual(first_tie.winner_id, match.home_team_id)
