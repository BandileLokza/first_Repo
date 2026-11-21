"""Focused regression coverage for configurable pool standings points.

The tests intentionally use the established result and standings service
boundaries.  That keeps margin allocation coupled to authoritative rack data
and verifies group qualification sees the same calculated table as the public
and manager pages.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .brackets import (
    create_draw_preview,
    generate_bracket_from_preview,
    reset_bracket,
)
from .models import (
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMarginPointRule,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolPlayer,
    PoolRack,
)
from .services import (
    PoolResultValidationError,
    correct_rack,
    open_result_correction,
    record_non_played_result,
    record_rack,
    start_match,
    transition_match_status,
)
from .standings import (
    PoolStandingsValidationError,
    eligible_pool_matches_for_stage,
    head_to_head_mini_table,
    point_allocation_for_pool_match,
    replace_pool_standings_points_configuration,
    standings_for_group,
    standings_for_stage,
)


User = get_user_model()


class PoolMarginPointsTestCase(TestCase):
    """Small factories for independent fixed- and margin-points scenarios."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="margin-points-owner",
            password="test-password",
        )
        self._competition_number = 0
        self._player_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        defaults = {
            "name": f"Margin Points Competition {self._competition_number}",
            "slug": f"margin-points-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "default_race_target": 7,
        }
        defaults.update(overrides)
        return PoolCompetition.objects.create(**defaults)

    def make_entries(self, competition, names):
        entries = []
        for name in names:
            self._player_number += 1
            player = PoolPlayer.objects.create(
                full_name=f"{name} {self._player_number}",
            )
            entries.append(
                PoolCompetitionEntry.objects.create(
                    competition=competition,
                    player=player,
                )
            )
        return entries

    @staticmethod
    def stage_for(competition, stage_type):
        return competition.stages.get(stage_type=stage_type)

    @staticmethod
    def rows_by_entry(rows):
        return {row.entry_id: row for row in rows}

    def make_match(self, competition, stage, home, away, *, group=None):
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            group=group,
            home_entry=home,
            away_entry=away,
        )

    def finish_played(self, pool_match, winner, *, loser_racks=0):
        """Finish a match from authoritative rack records only."""
        start_match(pool_match, recorded_by=self.owner)
        loser = (
            pool_match.away_entry
            if winner.pk == pool_match.home_entry_id
            else pool_match.home_entry
        )
        for _number in range(loser_racks):
            record_rack(pool_match, winner=loser, recorded_by=self.owner)
        for _number in range(pool_match.race_target):
            record_rack(pool_match, winner=winner, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.result_method, PoolMatch.ResultMethod.PLAYED)
        self.assertEqual(pool_match.winner_id, winner.pk)
        return pool_match

    def finish_non_played(self, pool_match, winner, result_method):
        result = record_non_played_result(
            pool_match,
            result_method=result_method,
            winner=winner,
            recorded_by=self.owner,
            reason="Margin-points test result",
        )
        result.refresh_from_db()
        return result

    @staticmethod
    def margin_bands(competition):
        """Return the legacy test allocation bounded by this race target."""
        target = competition.default_race_target
        if target == 1:
            return ((1, 1, 3, 1),)
        if target <= 3:
            return ((1, target, 3, 1),)
        return (
            (1, 3, 3, 1),
            (4, target, 4, 0),
        )

    def configure_margin_points(self, competition, *, win_points=2, loss_points=0):
        return replace_pool_standings_points_configuration(
            competition,
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            points_for_win=win_points,
            points_for_loss=loss_points,
            margin_rules=self.margin_bands(competition),
        )


class PoolFixedPointsRegressionTests(PoolMarginPointsTestCase):
    def test_fixed_points_keep_existing_two_zero_behavior_and_allow_three_zero(self):
        competition = self.make_competition(default_race_target=7)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))

        pool_match = self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=5,
        )
        self.assertEqual(point_allocation_for_pool_match(competition, pool_match), (2, 0))
        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (2, 0))

        competition, rules = replace_pool_standings_points_configuration(
            competition,
            points_mode=PoolCompetition.PointsMode.FIXED,
            points_for_win=3,
            points_for_loss=0,
        )
        self.assertEqual(rules, [])
        self.assertEqual(competition.points_mode, PoolCompetition.PointsMode.FIXED)
        self.assertEqual(point_allocation_for_pool_match(competition, pool_match), (3, 0))
        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (3, 0))


class PoolMarginBasedPointsTests(PoolMarginPointsTestCase):
    def test_race_to_seven_all_margins_and_away_winner_use_the_same_bands(self):
        competition = self.make_competition(default_race_target=7)
        competition, rules = self.configure_margin_points(competition)
        self.assertEqual(
            [
                (rule.minimum_margin, rule.maximum_margin, rule.winner_points, rule.loser_points)
                for rule in rules
            ],
            [(1, 3, 3, 1), (4, 7, 4, 0)],
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))

        expected_by_loser_racks = {
            6: (3, 1),
            5: (3, 1),
            4: (3, 1),
            3: (4, 0),
            2: (4, 0),
            1: (4, 0),
            0: (4, 0),
        }
        for loser_racks, expected in expected_by_loser_racks.items():
            pool_match = self.finish_played(
                self.make_match(competition, stage, alpha, bravo),
                alpha,
                loser_racks=loser_racks,
            )
            self.assertEqual(
                point_allocation_for_pool_match(competition, pool_match),
                expected,
            )

        # A close result with the away entry winning uses its rack margin, not
        # home/away fixture orientation.
        away_winner = self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            bravo,
            loser_racks=5,
        )
        self.assertEqual(
            (away_winner.home_score, away_winner.away_score),
            (5, 7),
        )
        self.assertEqual(
            point_allocation_for_pool_match(competition, away_winner),
            (3, 1),
        )

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual(
            (rows[alpha.pk].played, rows[alpha.pk].won, rows[alpha.pk].lost),
            (8, 7, 1),
        )
        self.assertEqual(
            (rows[bravo.pk].played, rows[bravo.pk].won, rows[bravo.pk].lost),
            (8, 1, 7),
        )
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (26, 6))
        self.assertEqual(rows[alpha.pk].recent_form[0], "L")
        self.assertEqual(rows[bravo.pk].recent_form[0], "W")

    def test_margin_bands_depend_on_rack_margin_not_the_race_target(self):
        competition = self.make_competition(default_race_target=5)
        competition, _rules = self.configure_margin_points(competition)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))

        for loser_racks, expected in ((4, (3, 1)), (2, (3, 1)), (1, (4, 0))):
            pool_match = self.finish_played(
                self.make_match(competition, stage, alpha, bravo),
                alpha,
                loser_racks=loser_racks,
            )
            self.assertEqual(
                point_allocation_for_pool_match(competition, pool_match),
                expected,
            )

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (10, 2))

    def test_walkover_withdrawal_disqualification_and_bye_do_not_use_margin_bands(self):
        competition = self.make_competition(default_race_target=7)
        competition, _rules = self.configure_margin_points(competition)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))

        walkover = self.finish_non_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            PoolMatch.ResultMethod.WALKOVER,
        )
        self.assertEqual((walkover.home_score, walkover.away_score), (0, 0))
        self.assertEqual(walkover.racks.count(), 0)

        withdrawal = self.make_match(competition, stage, alpha, bravo)
        start_match(withdrawal, recorded_by=self.owner)
        # This genuine close rack score must stay in statistics, but a
        # withdrawal still takes the ordinary configured 2/0 table points.
        record_rack(withdrawal, winner=bravo, recorded_by=self.owner)
        withdrawal = self.finish_non_played(
            withdrawal,
            alpha,
            PoolMatch.ResultMethod.WITHDRAWAL,
        )

        disqualification = self.finish_non_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            PoolMatch.ResultMethod.DISQUALIFICATION,
        )
        for pool_match in (walkover, withdrawal, disqualification):
            self.assertEqual(
                point_allocation_for_pool_match(competition, pool_match),
                (2, 0),
            )

        bye_attempt = self.make_match(competition, stage, alpha, bravo)
        with self.assertRaises(PoolResultValidationError):
            record_non_played_result(
                bye_attempt,
                result_method=PoolMatch.ResultMethod.BYE,
                winner=alpha,
                recorded_by=self.owner,
                reason="A bye must remain bracket topology.",
            )
        bye_attempt.refresh_from_db()
        self.assertEqual(bye_attempt.status, PoolMatch.Status.UPCOMING)

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (6, 0))
        self.assertEqual((rows[alpha.pk].racks_for, rows[alpha.pk].racks_against), (0, 1))
        self.assertEqual((rows[bravo.pk].racks_for, rows[bravo.pk].racks_against), (1, 0))

    def test_non_eligible_states_and_correction_open_results_remain_excluded(self):
        competition = self.make_competition(default_race_target=7)
        competition, _rules = self.configure_margin_points(competition)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))

        upcoming = self.make_match(competition, stage, alpha, bravo)
        live = self.make_match(competition, stage, alpha, bravo)
        start_match(live, recorded_by=self.owner)
        postponed = self.make_match(competition, stage, alpha, bravo)
        transition_match_status(
            postponed,
            status=PoolMatch.Status.POSTPONED,
            recorded_by=self.owner,
        )
        cancelled = self.make_match(competition, stage, alpha, bravo)
        transition_match_status(
            cancelled,
            status=PoolMatch.Status.CANCELLED,
            recorded_by=self.owner,
        )
        abandoned = self.make_match(competition, stage, alpha, bravo)
        start_match(abandoned, recorded_by=self.owner)
        transition_match_status(
            abandoned,
            status=PoolMatch.Status.ABANDONED,
            recorded_by=self.owner,
        )
        correction_open = self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=5,
        )
        open_result_correction(
            correction_open,
            recorded_by=self.owner,
            reason="Exclude while its audited correction is open.",
        )

        rows = self.rows_by_entry(standings_for_stage(stage))
        for entry in (alpha, bravo):
            self.assertEqual(rows[entry.pk].played, 0)
            self.assertEqual(rows[entry.pk].points, 0)
        self.assertEqual(upcoming.status, PoolMatch.Status.UPCOMING)

    def test_direct_point_allocation_rejects_unfinalized_or_open_correction_results(self):
        """The exported allocation helper is as strict as standings itself."""
        competition = self.make_competition(default_race_target=7)
        competition, _rules = self.configure_margin_points(competition)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))

        upcoming = self.make_match(competition, stage, alpha, bravo)
        with self.assertRaises(PoolStandingsValidationError):
            point_allocation_for_pool_match(competition, upcoming)

        live = self.make_match(competition, stage, alpha, bravo)
        start_match(live, recorded_by=self.owner)
        record_rack(live, winner=alpha, recorded_by=self.owner)
        with self.assertRaises(PoolStandingsValidationError):
            point_allocation_for_pool_match(competition, live)

        correcting = self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=5,
        )
        correcting = open_result_correction(
            correcting,
            recorded_by=self.owner,
            reason="The result is not final while correction is open.",
        )
        with self.assertRaises(PoolStandingsValidationError):
            point_allocation_for_pool_match(competition, correcting)

    def test_group_standings_use_the_same_margin_points_policy(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            default_race_target=7,
        )
        competition, _rules = self.configure_margin_points(competition)
        group_stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Group Stage"
        group_stage.save()
        group = PoolGroup.objects.create(stage=group_stage, name="Group A", order=1)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))
        PoolGroupEntry.objects.create(stage=group_stage, group=group, entry=alpha)
        PoolGroupEntry.objects.create(stage=group_stage, group=group, entry=bravo)

        self.finish_played(
            self.make_match(competition, group_stage, alpha, bravo, group=group),
            alpha,
            loser_racks=5,
        )
        rows = self.rows_by_entry(standings_for_group(group))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (3, 1))
        self.assertEqual((rows[alpha.pk].racks_for, rows[bravo.pk].racks_for), (7, 5))

    def test_margin_points_drive_group_qualification_slots(self):
        """Qualification reads the same bonus-point table, not a fixed clone."""
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            default_race_target=7,
        )
        competition, _rules = self.configure_margin_points(competition)
        group_stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Qualifying groups"
        group_stage.save()
        knockout_stage = self.stage_for(
            competition,
            PoolCompetitionStage.StageType.KNOCKOUT,
        )
        group_a = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=2,
        )
        group_b = PoolGroup.objects.create(
            stage=group_stage,
            name="Group B",
            order=2,
            qualifiers=2,
        )
        alpha, bravo, charlie, delta, echo = self.make_entries(
            competition,
            ("Alpha", "Bravo", "Charlie", "Delta", "Echo"),
        )
        for group, entries in (
            (group_a, (alpha, bravo, charlie)),
            (group_b, (delta, echo)),
        ):
            for order, entry in enumerate(entries, start=1):
                PoolGroupEntry.objects.create(
                    stage=group_stage,
                    group=group,
                    entry=entry,
                    sort_order=order,
                )

        # All three Group A players win once.  The bonus allocation makes
        # Alpha (4 + 1) first and Charlie (3 + 1) second, ahead of Bravo (3).
        self.finish_played(
            self.make_match(competition, group_stage, alpha, bravo, group=group_a),
            alpha,
            loser_racks=0,
        )
        self.finish_played(
            self.make_match(competition, group_stage, bravo, charlie, group=group_a),
            bravo,
            loser_racks=6,
        )
        self.finish_played(
            self.make_match(competition, group_stage, charlie, alpha, group=group_a),
            charlie,
            loser_racks=6,
        )
        self.finish_played(
            self.make_match(competition, group_stage, delta, echo, group=group_b),
            delta,
            loser_racks=5,
        )
        group_a_rows = self.rows_by_entry(standings_for_group(group_a))
        self.assertEqual(
            [row.entry_id for row in standings_for_group(group_a)],
            [alpha.pk, charlie.pk, bravo.pk],
        )
        self.assertEqual(
            (group_a_rows[alpha.pk].points, group_a_rows[charlie.pk].points, group_a_rows[bravo.pk].points),
            (5, 4, 3),
        )

        for group, position, destination_slot in (
            (group_a, 1, 1),
            (group_b, 2, 2),
            (group_b, 1, 3),
            (group_a, 2, 4),
        ):
            PoolGroupQualificationRule.objects.create(
                source_stage=group_stage,
                group=group,
                knockout_stage=knockout_stage,
                position=position,
                destination_slot=destination_slot,
            )
        preview = create_draw_preview(
            competition,
            stage=knockout_stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            bracket_size=4,
            actor=self.owner,
        )
        bracket = generate_bracket_from_preview(preview, actor=self.owner)
        group_a_slots = {
            slot.source_position: slot.entry_id
            for slot in PoolKnockoutTieSlot.objects.filter(
                tie__bracket=bracket,
                source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
                source_group=group_a,
            )
        }
        self.assertEqual(group_a_slots, {1: alpha.pk, 2: charlie.pk})

    def test_two_entry_and_multi_entry_head_to_head_use_margin_allocations(self):
        competition = self.make_competition(default_race_target=7)
        competition, _rules = self.configure_margin_points(competition)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo, charlie = self.make_entries(
            competition,
            ("Alpha", "Bravo", "Charlie"),
        )

        # A 3/1 close result followed by a 4/0 convincing result proves the
        # mini-table does not silently use the historic fixed 2/0 allocation.
        self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=5,
        )
        self.finish_played(
            self.make_match(competition, stage, bravo, charlie),
            bravo,
            loser_racks=5,
        )
        self.finish_played(
            self.make_match(competition, stage, charlie, alpha),
            charlie,
            loser_racks=3,
        )

        rows = self.rows_by_entry(standings_for_stage(stage))
        source_matches = eligible_pool_matches_for_stage(stage)
        two_entry = head_to_head_mini_table(
            [rows[alpha.pk], rows[bravo.pk]],
            source_matches,
            competition=competition,
        )
        self.assertEqual((two_entry[alpha.pk].points, two_entry[bravo.pk].points), (3, 1))

        multi_entry = head_to_head_mini_table(
            [rows[alpha.pk], rows[bravo.pk], rows[charlie.pk]],
            source_matches,
            competition=competition,
        )
        self.assertEqual(
            (
                multi_entry[alpha.pk].points,
                multi_entry[bravo.pk].points,
                multi_entry[charlie.pk].points,
            ),
            (3, 4, 5),
        )

    def test_a_rack_correction_recalculates_margin_points_without_saved_totals(self):
        competition = self.make_competition(default_race_target=7)
        competition, _rules = self.configure_margin_points(competition)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))
        pool_match = self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=3,
        )
        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (4, 0))

        open_result_correction(
            pool_match,
            recorded_by=self.owner,
            reason="Correct the final score from 7-3 to 7-5.",
        )
        alpha_racks = list(
            PoolRack.objects.filter(match=pool_match, winner=alpha).order_by("rack_number")
        )
        for rack in alpha_racks[:2]:
            correct_rack(
                pool_match,
                rack,
                winner=bravo,
                recorded_by=self.owner,
                reason="Correct this rack winner.",
            )
        # The correction temporarily returns the fixture to live play at 5-5;
        # authoritative replacement racks complete it as 7-5.
        record_rack(pool_match, winner=alpha, recorded_by=self.owner)
        record_rack(pool_match, winner=alpha, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual((pool_match.home_score, pool_match.away_score), (7, 5))
        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (3, 1))


class PoolMarginPointsValidationAndLockTests(PoolMarginPointsTestCase):
    def test_margin_configuration_rejects_invalid_or_incomplete_bands(self):
        competition = self.make_competition()
        invalid_policies = {
            "overlap": ((1, 3, 3, 1), (3, 7, 4, 0)),
            "gap": ((1, 2, 3, 1), (4, 7, 4, 0)),
            "minimum-zero": ((0, 7, 3, 1),),
            "maximum-before-minimum": ((4, 3, 3, 1),),
            "duplicate-range-start": ((1, 2, 3, 1), (1, 7, 4, 0)),
            "missing-terminal-race-margin": ((1, 3, 3, 1), (4, 6, 4, 0)),
            "no-rules": (),
            "negative-points": ((1, 7, -1, 0),),
        }

        for label, rules in invalid_policies.items():
            with self.subTest(label=label), self.assertRaises(PoolStandingsValidationError):
                replace_pool_standings_points_configuration(
                    competition,
                    points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
                    points_for_win=2,
                    points_for_loss=0,
                    margin_rules=rules,
                )

        competition.refresh_from_db()
        self.assertEqual(competition.points_mode, PoolCompetition.PointsMode.FIXED)
        self.assertFalse(competition.margin_point_rules.exists())

    def test_generated_group_qualification_bracket_locks_margin_configuration_until_reset(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            default_race_target=1,
        )
        competition, rules = self.configure_margin_points(competition)
        group_stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Qualifying Groups"
        group_stage.save()
        knockout_stage = self.stage_for(
            competition,
            PoolCompetitionStage.StageType.KNOCKOUT,
        )
        group_a = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=2,
        )
        group_b = PoolGroup.objects.create(
            stage=group_stage,
            name="Group B",
            order=2,
            qualifiers=2,
        )
        a_one, a_two, b_one, b_two = self.make_entries(
            competition,
            ("A One", "A Two", "B One", "B Two"),
        )
        for group, entries in ((group_a, (a_one, a_two)), (group_b, (b_one, b_two))):
            for order, entry in enumerate(entries, start=1):
                PoolGroupEntry.objects.create(
                    stage=group_stage,
                    group=group,
                    entry=entry,
                    sort_order=order,
                )

        self.finish_non_played(
            self.make_match(competition, group_stage, a_one, a_two, group=group_a),
            a_one,
            PoolMatch.ResultMethod.WALKOVER,
        )
        self.finish_non_played(
            self.make_match(competition, group_stage, b_one, b_two, group=group_b),
            b_one,
            PoolMatch.ResultMethod.WALKOVER,
        )
        for group, position, destination_slot in (
            (group_a, 1, 1),
            (group_b, 2, 2),
            (group_b, 1, 3),
            (group_a, 2, 4),
        ):
            PoolGroupQualificationRule.objects.create(
                source_stage=group_stage,
                group=group,
                knockout_stage=knockout_stage,
                position=position,
                destination_slot=destination_slot,
            )

        preview = create_draw_preview(
            competition,
            stage=knockout_stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            bracket_size=4,
            actor=self.owner,
        )
        bracket = generate_bracket_from_preview(preview, actor=self.owner)

        with self.assertRaises(PoolStandingsValidationError):
            replace_pool_standings_points_configuration(
                competition,
                points_mode=PoolCompetition.PointsMode.FIXED,
                points_for_win=3,
                points_for_loss=0,
            )

        first_rule = rules[0]
        first_rule.winner_points = 5
        with self.assertRaises(ValidationError):
            first_rule.full_clean()
        with self.assertRaises(ValidationError):
            first_rule.delete()
        with self.assertRaises(ValidationError):
            PoolCompetitionMarginPointRule(
                competition=competition,
                minimum_margin=8,
                maximum_margin=8,
                winner_points=5,
                loser_points=0,
            ).full_clean()

        # A generated but untouched qualifying bracket can be reset through
        # the existing protected workflow; the policy becomes editable again.
        self.assertTrue(reset_bracket(bracket, actor=self.owner, reason="Test reset"))
        competition, replacement_rules = replace_pool_standings_points_configuration(
            competition,
            points_mode=PoolCompetition.PointsMode.FIXED,
            points_for_win=3,
            points_for_loss=0,
        )
        self.assertEqual(competition.points_mode, PoolCompetition.PointsMode.FIXED)
        self.assertEqual(competition.points_for_win, 3)
        self.assertEqual(replacement_rules, [])
        self.assertFalse(PoolCompetitionMarginPointRule.objects.filter(competition=competition).exists())

    def test_margin_rule_cannot_be_moved_away_from_an_active_policy(self):
        competition = self.make_competition()
        competition, rules = self.configure_margin_points(competition)
        other_competition = self.make_competition()
        rule = rules[0]
        rule.competition = other_competition

        with self.assertRaises(ValidationError):
            rule.save()

        rule.refresh_from_db()
        self.assertEqual(rule.competition_id, competition.pk)


class PoolMarginPointsManagerTests(PoolMarginPointsTestCase):
    def test_owner_can_render_and_save_a_complete_margin_policy(self):
        competition = self.make_competition()
        url = reverse("pool:manage_competition_standings", args=[competition.slug])
        self.client.force_login(self.owner)

        response = self.client.get(f"{url}?points_mode=MARGIN_BASED")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Points system")
        self.assertContains(response, "Played-match points")

        response = self.client.post(
            url,
            {
                "action": "points",
                "points_mode": PoolCompetition.PointsMode.MARGIN_BASED,
                "points_for_win": 2,
                "points_for_loss": 0,
                "margin_rules-TOTAL_FORMS": 2,
                "margin_rules-INITIAL_FORMS": 0,
                "margin_rules-MIN_NUM_FORMS": 0,
                "margin_rules-MAX_NUM_FORMS": 1000,
                "margin_rules-0-up_to_margin": 3,
                "margin_rules-0-winner_points": 3,
                "margin_rules-0-loser_points": 1,
                "margin_rules-1-up_to_margin": 7,
                "margin_rules-1-winner_points": 4,
                "margin_rules-1-loser_points": 0,
            },
        )
        self.assertRedirects(response, url)
        competition.refresh_from_db()
        self.assertEqual(
            competition.points_mode,
            PoolCompetition.PointsMode.MARGIN_BASED,
        )
        self.assertEqual(
            list(
                competition.margin_point_rules.order_by("minimum_margin").values_list(
                    "minimum_margin",
                    "maximum_margin",
                    "winner_points",
                    "loser_points",
                )
            ),
            [(1, 3, 3, 1), (4, 7, 4, 0)],
        )

    def test_knockout_only_competition_has_no_standings_points_manager_page(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("pool:manage_competition_standings", args=[competition.slug])
        )
        self.assertEqual(response.status_code, 404)

    def test_damaged_active_margin_policy_shows_a_repair_state_not_a_server_error(self):
        competition = self.make_competition(default_race_target=7)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ("Alpha", "Bravo"))
        self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=5,
        )
        # Simulate a damaged imported database row.  Normal model/service
        # paths cannot create this active policy without complete bands.
        PoolCompetition._base_manager.filter(pk=competition.pk).update(
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
        )

        public_response = self.client.get(
            f"{reverse('pool:competition_detail', args=[competition.slug])}?tab=standings"
        )
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "Standings are temporarily unavailable")

        self.client.force_login(self.owner)
        manager_response = self.client.get(
            reverse("pool:manage_competition_standings", args=[competition.slug])
        )
        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, "active points policy is incomplete or invalid")

    def test_assigned_admin_and_superuser_can_post_standings_points(self):
        competition = self.make_competition()
        url = reverse("pool:manage_competition_standings", args=[competition.slug])
        assigned_admin = User.objects.create_user(
            username="margin-points-admin",
            password="test-password",
        )
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=assigned_admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        superuser = User.objects.create_superuser(
            username="margin-points-superuser",
            email="margin-points-superuser@example.test",
            password="test-password",
        )

        self.client.force_login(assigned_admin)
        response = self.client.post(
            url,
            {
                "action": "points",
                "points_mode": PoolCompetition.PointsMode.FIXED,
                "points_for_win": 3,
                "points_for_loss": 0,
            },
        )
        self.assertRedirects(response, url)
        competition.refresh_from_db()
        self.assertEqual(competition.points_for_win, 3)

        self.client.force_login(superuser)
        response = self.client.post(
            url,
            {
                "action": "points",
                "points_mode": PoolCompetition.PointsMode.FIXED,
                "points_for_win": 4,
                "points_for_loss": 1,
            },
        )
        self.assertRedirects(response, url)
        competition.refresh_from_db()
        self.assertEqual(
            (competition.points_for_win, competition.points_for_loss),
            (4, 1),
        )

    def test_anonymous_ordinary_and_other_competition_managers_are_scoped_out(self):
        competition = self.make_competition()
        url = reverse("pool:manage_competition_standings", args=[competition.slug])
        ordinary_user = User.objects.create_user(
            username="margin-points-ordinary",
            password="test-password",
        )
        assigned_admin = User.objects.create_user(
            username="margin-points-assigned-admin",
            password="test-password",
        )
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=assigned_admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        foreign_owner = User.objects.create_user(
            username="margin-points-foreign-owner",
            password="test-password",
        )
        foreign_competition = self.make_competition(owner=foreign_owner)
        foreign_url = reverse(
            "pool:manage_competition_standings",
            args=[foreign_competition.slug],
        )

        anonymous_response = self.client.get(url)
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("next=", anonymous_response.url)
        self.assertIn(url, anonymous_response.url)

        self.client.force_login(ordinary_user)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(assigned_admin)
        self.assertEqual(self.client.get(foreign_url).status_code, 404)

    def test_foreign_margin_rule_identifier_cannot_modify_a_rule_outside_competition(self):
        competition = self.make_competition()
        url = reverse("pool:manage_competition_standings", args=[competition.slug])
        foreign_owner = User.objects.create_user(
            username="margin-points-foreign-rule-owner",
            password="test-password",
        )
        foreign_competition = self.make_competition(owner=foreign_owner)
        foreign_competition, foreign_rules = self.configure_margin_points(
            foreign_competition,
        )
        foreign_rule = foreign_rules[0]
        original = (
            foreign_rule.minimum_margin,
            foreign_rule.maximum_margin,
            foreign_rule.winner_points,
            foreign_rule.loser_points,
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            url,
            {
                "action": "points",
                "points_mode": PoolCompetition.PointsMode.MARGIN_BASED,
                "points_for_win": 2,
                "points_for_loss": 0,
                # The compact card form has no model identity field at all.
                # A forged foreign identifier must be rejected rather than
                # updated, deleted, or silently reused.
                "margin_rules-TOTAL_FORMS": 1,
                "margin_rules-INITIAL_FORMS": 1,
                "margin_rules-MIN_NUM_FORMS": 0,
                "margin_rules-MAX_NUM_FORMS": 1000,
                "margin_rules-0-id": foreign_rule.pk,
                "margin_rules-0-up_to_margin": 3,
                "margin_rules-0-winner_points": 99,
                "margin_rules-0-loser_points": 98,
            },
        )
        self.assertEqual(response.status_code, 200)
        foreign_rule.refresh_from_db()
        self.assertEqual(
            (
                foreign_rule.minimum_margin,
                foreign_rule.maximum_margin,
                foreign_rule.winner_points,
                foreign_rule.loser_points,
            ),
            original,
        )
        self.assertFalse(
            PoolCompetitionMarginPointRule.objects.filter(competition=competition).exists()
        )
