"""Focused regression coverage for football knockout Phase One.

These tests deliberately exercise the public services and result-entry routes
rather than duplicating their implementation details.  They require the Phase
One model migrations to have been generated and applied before they can run.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import LeagueMembership
from league_manager.permissions import can_control_match, can_manage_team_lineup
from leagues.models import Competition, Group, Stage, Standing
from matches.forms import MatchAdminForm
from matches.control_state import get_match_control_state
from matches.knockout import aggregate_scores, validate_fixture_knockout_config
from matches.models import (
    FootballKnockoutRoundRule,
    FootballKnockoutTie,
    Goal,
    Match,
    MatchAdminAssignment,
    MatchResultProcessingOutbox as Outbox,
    Player,
)
from matches.services import (
    apply_match_to_standings_incrementally,
    apply_official_match_result,
    end_extra_time,
    end_extra_time_first_half,
    end_regulation,
    finish_penalty_shootout,
    start_extra_time,
    start_extra_time_second_half,
    start_penalty_shootout,
)
from predictions.models import Prediction, PredictionMatch
from predictions.utils import calculate_predictions_for_match
from teams.models import Team, TeamCoachAssignment


User = get_user_model()


class KnockoutPhaseOneTests(TestCase):
    """Regression tests for Phase One's football-specific knockout workflow."""

    def setUp(self):
        self.sequence = 0
        self.manager = User.objects.create_user(
            username="phase-one-manager",
            password="pass",
        )
        self.superuser = User.objects.create_superuser(
            username="phase-one-superuser",
            email="phase-one-superuser@example.test",
            password="pass",
        )

    def make_competition(self, *, format_type="KNOCKOUT"):
        self.sequence += 1
        competition = Competition.objects.create(
            name=f"Phase One Competition {self.sequence}",
            slug=f"phase-one-competition-{self.sequence}",
            season="2026",
            format_type=format_type,
        )
        LeagueMembership.objects.create(
            user=self.manager,
            competition=competition,
            role=LeagueMembership.Role.ADMIN,
        )
        return competition

    def make_stage(self, competition, stage_type, *, name=None):
        return Stage.objects.create(
            competition=competition,
            name=name or f"{stage_type.title()} stage {self.sequence}",
            stage_type=stage_type,
            order=1,
        )

    def make_teams(self, competition):
        return (
            Team.objects.create(
                name=f"Home FC {self.sequence}",
                competition=competition,
            ),
            Team.objects.create(
                name=f"Away FC {self.sequence}",
                competition=competition,
            ),
        )

    def make_match(self, stage, home_team, away_team, *, group=None, leg=1):
        return Match.objects.create(
            competition=stage.competition,
            stage=stage,
            group=group,
            home_team=home_team,
            away_team=away_team,
            leg=leg,
            match_date=timezone.now() - timedelta(minutes=5),
        )

    def make_knockout_context(
        self,
        *,
        legs=FootballKnockoutRoundRule.Legs.ONE,
        policy=FootballKnockoutRoundRule.DeciderPolicy.EXTRA_TIME_THEN_PENALTIES,
    ):
        competition = self.make_competition()
        stage = self.make_stage(competition, "KNOCKOUT", name="Knockout round")
        home_team, away_team = self.make_teams(competition)
        rule = FootballKnockoutRoundRule.objects.create(
            stage=stage,
            number_of_legs=legs,
            decider_policy=policy,
        )
        tie = FootballKnockoutTie.objects.create(
            stage=stage,
            team_one=home_team,
            team_two=away_team,
        )
        return competition, stage, home_team, away_team, rule, tie

    def make_knockout_match(self, stage, home_team, away_team, tie, *, leg=1):
        match = self.make_match(stage, home_team, away_team, leg=leg)
        match.knockout_tie = tie
        match.save(update_fields=["knockout_tie"])
        return match

    def put_match_in_phase(self, match, phase, *, home_score=0, away_score=0, **scores):
        """Place a fixture in a valid persisted state for transition testing."""
        match.status = "LIVE"
        match.phase = phase
        match.phase_start_time = (
            None if phase in {"REGULATION_ENDED", "EXTRA_ENDED", "PENALTIES"}
            else timezone.now()
        )
        match.home_score = home_score
        match.away_score = away_score
        for field_name, value in scores.items():
            setattr(match, field_name, value)
        update_fields = [
            "status",
            "phase",
            "phase_start_time",
            "home_score",
            "away_score",
            *scores.keys(),
        ]
        match.save(update_fields=update_fields)
        match.refresh_from_db()
        return match

    def start_unresolved_regulation(self, match, *, home_score=1, away_score=1):
        return self.put_match_in_phase(
            match,
            "SECOND_HALF",
            home_score=home_score,
            away_score=away_score,
        )

    def take_unresolved_match_to_extra_time_end(self, match):
        self.start_unresolved_regulation(match)
        match = end_regulation(match)
        match = start_extra_time(match)
        match = end_extra_time_first_half(match)
        match = start_extra_time_second_half(match)
        return end_extra_time(match)

    def result_post_data(self, match, *, penalties=None):
        data = {
            f"home_score_{match.id}": "1",
            f"away_score_{match.id}": "1",
            f"regulation_home_score_{match.id}": "1",
            f"regulation_away_score_{match.id}": "1",
            f"penalty_home_score_{match.id}": "",
            f"penalty_away_score_{match.id}": "",
        }
        if penalties is not None:
            data[f"penalty_home_score_{match.id}"] = str(penalties[0])
            data[f"penalty_away_score_{match.id}"] = str(penalties[1])
        return data

    def test_league_and_group_draws_finish_after_regulation_without_deciders(self):
        """League and group draws remain ordinary completed draws."""
        for stage_type, format_type in (
            ("LEAGUE", "TABLE"),
            ("GROUP", "GROUP_KNOCKOUT"),
        ):
            with self.subTest(stage_type=stage_type):
                competition = self.make_competition(format_type=format_type)
                stage = self.make_stage(competition, stage_type)
                home_team, away_team = self.make_teams(competition)
                group = None
                if stage_type == "GROUP":
                    group = Group.objects.create(stage=stage, name="A")
                    group.teams.add(home_team, away_team)

                match = self.make_match(stage, home_team, away_team, group=group)
                self.start_unresolved_regulation(match)
                match = end_regulation(match)

                self.assertEqual(match.status, "FINISHED")
                self.assertEqual(match.resolution_method, "REGULATION")
                self.assertEqual(match.regulation_home_score, 1)
                self.assertEqual(match.regulation_away_score, 1)
                self.assertIsNone(match.penalty_home_score)
                self.assertIsNone(match.penalty_away_score)

    def test_extra_time_and_penalty_urls_reject_league_and_group_matches(self):
        """Hidden controls cannot be invoked directly for non-knockout stages."""
        self.client.force_login(self.manager)

        for stage_type, format_type in (
            ("LEAGUE", "TABLE"),
            ("GROUP", "GROUP_KNOCKOUT"),
        ):
            with self.subTest(stage_type=stage_type):
                competition = self.make_competition(format_type=format_type)
                stage = self.make_stage(competition, stage_type)
                home_team, away_team = self.make_teams(competition)
                group = None
                if stage_type == "GROUP":
                    group = Group.objects.create(stage=stage, name="A")
                    group.teams.add(home_team, away_team)
                match = self.make_match(stage, home_team, away_team, group=group)
                self.put_match_in_phase(
                    match,
                    "REGULATION_ENDED",
                    home_score=1,
                    away_score=1,
                    regulation_home_score=1,
                    regulation_away_score=1,
                )

                for view_name in ("start_extra_time", "start_penalties"):
                    response = self.client.post(reverse(view_name, args=[match.id]))
                    self.assertEqual(response.status_code, 400)
                    match.refresh_from_db()
                    self.assertEqual(match.phase, "REGULATION_ENDED")

    def test_knockout_fixture_validation_rejects_reversed_duplicate_and_extra_leg(self):
        """A round cannot receive a duplicate/reversed fixture or a third leg."""
        competition, stage, team_one, team_two, _, _ = self.make_knockout_context(
            legs=FootballKnockoutRoundRule.Legs.TWO,
        )
        self.make_match(stage, team_one, team_two, leg=1)

        reversed_duplicate = Match(
            competition=competition,
            stage=stage,
            home_team=team_two,
            away_team=team_one,
            leg=1,
            match_date=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            validate_fixture_knockout_config(reversed_duplicate)

        extra_leg = Match(
            competition=competition,
            stage=stage,
            home_team=team_one,
            away_team=team_two,
            leg=3,
            match_date=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            validate_fixture_knockout_config(extra_leg)

    def test_drawn_first_leg_finishes_normally_and_rejects_decider_actions(self):
        """A two-leg tie's first match cannot be extended or sent to penalties."""
        _, stage, team_one, team_two, _, tie = self.make_knockout_context(
            legs=FootballKnockoutRoundRule.Legs.TWO,
        )
        match = self.make_knockout_match(stage, team_one, team_two, tie, leg=1)
        self.put_match_in_phase(
            match,
            "REGULATION_ENDED",
            home_score=1,
            away_score=1,
            regulation_home_score=1,
            regulation_away_score=1,
        )

        state = get_match_control_state(match)
        self.assertFalse(state.can_start_extra_time)
        self.assertFalse(state.can_start_penalties)
        with self.assertRaises(ValidationError):
            start_extra_time(match)
        with self.assertRaises(ValidationError):
            start_penalty_shootout(match)
        with self.assertRaises(ValidationError):
            apply_official_match_result(
                match,
                home_score=2,
                away_score=1,
                regulation_home_score=1,
                regulation_away_score=1,
            )

        match = apply_official_match_result(
            match,
            home_score=1,
            away_score=1,
            regulation_home_score=1,
            regulation_away_score=1,
        )
        self.assertEqual(match.status, "FINISHED")
        self.assertEqual(match.resolution_method, "REGULATION")

    def test_aggregate_uses_tie_team_order_when_second_leg_home_teams_reverse(self):
        """Aggregate totals remain correct when the second leg reverses home/away."""
        _, stage, team_one, team_two, _, tie = self.make_knockout_context(
            legs=FootballKnockoutRoundRule.Legs.TWO,
        )
        first_leg = self.make_knockout_match(
            stage,
            team_one,
            team_two,
            tie,
            leg=1,
        )
        second_leg = self.make_knockout_match(
            stage,
            team_two,
            team_one,
            tie,
            leg=2,
        )

        apply_official_match_result(first_leg, home_score=2, away_score=0)
        second_leg = apply_official_match_result(
            second_leg,
            home_score=0,
            away_score=1,
        )

        self.assertEqual(aggregate_scores(second_leg), (3, 0))
        self.assertEqual(second_leg.resolution_method, "AGGREGATE")
        self.assertEqual(second_leg.status, "FINISHED")

    def test_direct_to_penalties_skips_extra_time_and_finishes_with_separate_kicks(self):
        """Direct-to-penalties rules leave football goals separate from shootout kicks."""
        _, stage, team_one, team_two, _, tie = self.make_knockout_context(
            policy=FootballKnockoutRoundRule.DeciderPolicy.DIRECT_TO_PENALTIES,
        )
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.start_unresolved_regulation(match)
        match = end_regulation(match)

        state = get_match_control_state(match)
        self.assertFalse(state.can_start_extra_time)
        self.assertTrue(state.can_start_penalties)
        with self.assertRaises(ValidationError):
            start_extra_time(match)

        match = start_penalty_shootout(match)
        self.assertEqual(match.phase, "PENALTIES")
        match = finish_penalty_shootout(match, 5, 4)

        self.assertEqual(match.status, "FINISHED")
        self.assertEqual(match.home_score, 1)
        self.assertEqual(match.away_score, 1)
        self.assertEqual(match.penalty_home_score, 5)
        self.assertEqual(match.penalty_away_score, 4)
        self.assertEqual(match.resolution_method, "PENALTIES")

    def test_single_leg_regulation_winner_finishes_immediately(self):
        """A deciding knockout fixture needs no extra-time transition after a win."""
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.start_unresolved_regulation(match, home_score=2, away_score=1)

        match = end_regulation(match)

        self.assertEqual(match.status, "FINISHED")
        self.assertEqual(match.resolution_method, "REGULATION")
        self.assertEqual(match.regulation_home_score, 2)
        self.assertEqual(match.regulation_away_score, 1)

    def test_single_leg_draw_can_start_extra_time_when_configured(self):
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.start_unresolved_regulation(match)

        match = end_regulation(match)
        self.assertEqual(match.phase, "REGULATION_ENDED")
        self.assertTrue(get_match_control_state(match).can_start_extra_time)

        match = start_extra_time(match)
        self.assertEqual(match.phase, "EXTRA_FIRST_HALF")
        self.assertEqual(match.status, "LIVE")

    def test_extra_time_winner_finishes_with_extra_time_resolution(self):
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.start_unresolved_regulation(match)
        match = end_regulation(match)
        match = start_extra_time(match)
        match.home_score = 2
        match.save(update_fields=["home_score"])
        match = end_extra_time_first_half(match)
        match = start_extra_time_second_half(match)
        match = end_extra_time(match)

        self.assertEqual(match.status, "FINISHED")
        self.assertEqual(match.resolution_method, "EXTRA_TIME")
        self.assertEqual(match.regulation_home_score, 1)
        self.assertEqual(match.regulation_away_score, 1)
        self.assertEqual((match.home_score, match.away_score), (2, 1))

    def test_tie_after_extra_time_can_only_continue_to_penalties(self):
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)

        match = self.take_unresolved_match_to_extra_time_end(match)

        self.assertEqual(match.status, "LIVE")
        self.assertEqual(match.phase, "EXTRA_ENDED")
        state = get_match_control_state(match)
        self.assertTrue(state.can_start_penalties)
        self.assertFalse(state.can_finish_match)

    def test_tied_penalty_shootout_score_is_rejected(self):
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        match = self.take_unresolved_match_to_extra_time_end(match)
        match = start_penalty_shootout(match)

        with self.assertRaises(ValidationError):
            finish_penalty_shootout(match, 4, 4)

        match.refresh_from_db()
        self.assertEqual(match.status, "LIVE")
        self.assertEqual(match.phase, "PENALTIES")
        self.assertIsNone(match.penalty_home_score)
        self.assertIsNone(match.penalty_away_score)

    def test_extra_time_goals_count_but_penalty_kicks_cannot_be_match_goals(self):
        """Goal periods drive both scorekeeping and scorer statistics correctly."""
        competition, stage, team_one, team_two, _, tie = self.make_knockout_context()
        manager = self.manager
        scorer = Player.objects.create(name="Extra scorer", team=team_one)
        extra_time_match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.put_match_in_phase(
            extra_time_match,
            "EXTRA_FIRST_HALF",
            regulation_home_score=0,
            regulation_away_score=0,
        )

        self.client.force_login(manager)
        response = self.client.post(
            reverse("add_goal", args=[extra_time_match.id, team_one.id]),
            {"player": scorer.id},
        )
        self.assertEqual(response.status_code, 302)
        extra_time_match.refresh_from_db()
        goal = Goal.objects.get(match=extra_time_match)
        scorer.refresh_from_db()
        self.assertEqual(goal.period, Goal.Period.EXTRA_FIRST_HALF)
        self.assertEqual(extra_time_match.home_score, 1)
        self.assertEqual(scorer.recorded_goals, 1)

        # Use a separate configured fixture so a shootout state cannot be
        # mistaken for an ordinary live football period.
        _, penalty_stage, penalty_home, penalty_away, _, penalty_tie = (
            self.make_knockout_context()
        )
        penalty_match = self.make_knockout_match(
            penalty_stage,
            penalty_home,
            penalty_away,
            penalty_tie,
        )
        penalty_scorer = Player.objects.create(
            name="Penalty scorer",
            team=penalty_home,
        )
        self.put_match_in_phase(penalty_match, "PENALTIES")

        self.assertFalse(get_match_control_state(penalty_match).can_add_goal)
        response = self.client.post(
            reverse("add_goal", args=[penalty_match.id, penalty_home.id]),
            {"player": penalty_scorer.id},
        )
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(ValidationError):
            Goal.objects.create(
                match=penalty_match,
                team=penalty_home,
                player=penalty_scorer,
            )
        penalty_match.refresh_from_db()
        self.assertEqual(penalty_match.home_score, 0)
        self.assertFalse(Goal.objects.filter(match=penalty_match).exists())
        self.assertEqual(penalty_scorer.recorded_goals, 0)

    def test_predictions_use_regulation_score_not_extra_time_or_penalties(self):
        competition, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        prediction_match, _ = PredictionMatch.objects.get_or_create(
            match=match,
            defaults={"competition": competition},
        )
        predictor = User.objects.create_user(
            username="phase-one-predictor",
            password="pass",
        )
        prediction = Prediction.objects.create(
            user=predictor,
            prediction_match=prediction_match,
            predicted_result="D",
            predicted_home_goals=1,
            predicted_away_goals=1,
        )

        match = apply_official_match_result(
            match,
            home_score=2,
            away_score=1,
            regulation_home_score=1,
            regulation_away_score=1,
        )
        calculate_predictions_for_match(match)
        prediction.refresh_from_db()

        self.assertEqual(match.resolution_method, "EXTRA_TIME")
        self.assertEqual(prediction.points_awarded, 20)

    def test_knockout_matches_do_not_update_standings_in_service_or_legacy_method(self):
        competition, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        match = apply_official_match_result(match, home_score=2, away_score=0)

        apply_match_to_standings_incrementally(match)
        match.update_table()

        self.assertFalse(Standing.objects.filter(competition=competition).exists())
        match.refresh_from_db()
        self.assertFalse(match.table_updated)

    def test_both_bulk_result_entry_routes_reject_an_unresolved_knockout_result(self):
        """The legacy and league-manager pages both use central validation."""
        for route_kind in ("legacy", "manager"):
            with self.subTest(route=route_kind):
                competition, stage, team_one, team_two, _, tie = (
                    self.make_knockout_context()
                )
                match = self.make_knockout_match(stage, team_one, team_two, tie)
                if route_kind == "legacy":
                    self.client.force_login(self.superuser)
                    url = (
                        reverse("update_results_for_league", args=[competition.slug])
                        + "?filter=pending"
                    )
                else:
                    self.client.force_login(self.manager)
                    url = (
                        reverse("league_manager:result_update", args=[competition.slug])
                        + "?tab=pending"
                    )

                response = self.client.post(url, self.result_post_data(match))

                self.assertEqual(response.status_code, 200)
                match.refresh_from_db()
                self.assertNotEqual(match.status, "FINISHED")
                self.assertFalse(Outbox.objects.filter(match=match).exists())

    def test_django_admin_form_rejects_an_unresolved_knockout_result(self):
        """Direct Django-admin result editing shares the central validator."""
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)

        form = MatchAdminForm(
            data={
                "competition": match.competition_id,
                "stage": stage.id,
                "group": "",
                "leg": "1",
                "home_team": team_one.id,
                "away_team": team_two.id,
                "home_score": "1",
                "away_score": "1",
                "regulation_home_score": "",
                "regulation_away_score": "",
                "penalty_home_score": "",
                "penalty_away_score": "",
                "resolution_method": "",
                "knockout_tie": tie.id,
                "match_date": match.match_date.strftime("%Y-%m-%d %H:%M:%S"),
                "location": "",
                "abandon_reason": "",
                "status": "FINISHED",
                "phase": "NONE",
                "phase_start_time": "",
                "minute": "0",
                "table_updated": "",
                "lineups_locked": "",
                "table_home_score": "",
                "table_away_score": "",
            },
            instance=match,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)

    def test_manager_result_entry_finalizes_valid_penalty_result_and_queues_once(self):
        """A valid bulk result follows the one official result path once."""
        competition, stage, team_one, team_two, _, tie = self.make_knockout_context(
            policy=FootballKnockoutRoundRule.DeciderPolicy.DIRECT_TO_PENALTIES,
        )
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.client.force_login(self.manager)
        url = (
            reverse("league_manager:result_update", args=[competition.slug])
            + "?tab=pending"
        )

        response = self.client.post(url, self.result_post_data(match, penalties=(5, 4)))

        self.assertEqual(response.status_code, 302)
        match.refresh_from_db()
        self.assertEqual(match.status, "FINISHED")
        self.assertEqual(match.resolution_method, "PENALTIES")
        self.assertEqual((match.penalty_home_score, match.penalty_away_score), (5, 4))
        self.assertEqual(Outbox.objects.get(match=match).requested_revision, 1)

    def test_repeated_finalization_does_not_duplicate_standing_side_effects(self):
        competition = self.make_competition(format_type="TABLE")
        stage = self.make_stage(competition, "LEAGUE")
        home_team, away_team = self.make_teams(competition)
        match = self.make_match(stage, home_team, away_team)

        apply_official_match_result(match, home_score=2, away_score=0)
        apply_official_match_result(match, home_score=2, away_score=0)
        apply_match_to_standings_incrementally(match)
        apply_match_to_standings_incrementally(match)

        home_standing = Standing.objects.get(
            competition=competition,
            stage=stage,
            team=home_team,
        )
        away_standing = Standing.objects.get(
            competition=competition,
            stage=stage,
            team=away_team,
        )
        self.assertEqual((home_standing.played, home_standing.points), (1, 3))
        self.assertEqual((away_standing.played, away_standing.points), (1, 0))

    def test_extra_time_and_penalty_states_keep_lineups_locked_and_permissions_scoped(self):
        """Knockout timing phases do not loosen coach or match-agent permissions."""
        competition, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        coach = User.objects.create_user(username="phase-one-coach", password="pass")
        agent = User.objects.create_user(username="phase-one-agent", password="pass")
        TeamCoachAssignment.objects.create(team=team_one, user=coach)
        LeagueMembership.objects.create(
            user=agent,
            competition=competition,
            role=LeagueMembership.Role.AGENT,
        )
        MatchAdminAssignment.objects.create(match=match, admin=agent)

        self.put_match_in_phase(match, "EXTRA_FIRST_HALF")
        self.assertTrue(match.lineups_locked)
        self.assertTrue(can_control_match(self.manager, match))
        self.assertTrue(can_control_match(agent, match))
        self.assertFalse(can_control_match(coach, match))
        self.assertTrue(can_manage_team_lineup(coach, match, team_one))

        lineup_url = reverse("add_lineup", args=[match.id, team_one.id])
        self.client.force_login(coach)
        self.assertEqual(self.client.post(lineup_url, {}).status_code, 403)

        match.phase = "PENALTIES"
        match.save(update_fields=["phase"])
        match.refresh_from_db()
        self.assertTrue(match.lineups_locked)
        self.assertEqual(self.client.post(lineup_url, {}).status_code, 403)

    def test_knockout_transitions_relock_a_lineup_unlocked_before_extra_time_or_penalties(self):
        """An administrative unlock cannot leak into a later knockout phase."""
        _, stage, team_one, team_two, _, tie = self.make_knockout_context()
        match = self.make_knockout_match(stage, team_one, team_two, tie)
        self.start_unresolved_regulation(match)
        match = end_regulation(match)

        with self.assertRaises(ValidationError):
            match.unlock_lineups()
        self.assertTrue(match.lineups_locked)

        # Defend the transition itself too, so an old/inconsistent unlocked
        # state cannot leak into extra time or the shootout.
        match.lineups_locked = False
        match.save(update_fields=["lineups_locked"])
        match = start_extra_time(match)
        self.assertTrue(match.lineups_locked)

        match = end_extra_time_first_half(match)
        match = start_extra_time_second_half(match)
        match = end_extra_time(match)
        self.assertEqual(match.phase, "EXTRA_ENDED")
        match.lineups_locked = False
        match.save(update_fields=["lineups_locked"])
        match = start_penalty_shootout(match)
        self.assertTrue(match.lineups_locked)
