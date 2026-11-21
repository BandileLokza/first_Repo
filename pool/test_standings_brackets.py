"""Focused Phase Three coverage for calculated pool tables and brackets.

The tests deliberately exercise the public service boundaries instead of
writing finished scores or bracket relationships directly.  That keeps this
module useful as a regression suite for authoritative racks, corrections, and
the persistent bracket graph.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, reset_queries
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .brackets import (
    PoolBracketValidationError,
    create_draw_preview,
    generate_bracket_from_preview,
    generate_random_bracket,
    reset_bracket,
    update_bracket_round_targets,
)
from .forms import PoolBracketDrawForm
from .models import (
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolKnockoutBracket,
    PoolKnockoutRoundRule,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeam,
)
from .services import (
    PoolResultValidationError,
    open_result_correction,
    record_non_played_result,
    record_rack,
    start_match,
    transition_match_status,
)
from .standings import (
    DEFAULT_TIE_BREAKERS,
    PoolStandingsValidationError,
    configured_pool_tie_breakers,
    replace_pool_tie_breakers,
    standings_for_stage,
)


User = get_user_model()


class PoolPhaseThreeTestCase(TestCase):
    """Small factories for independent pool-only Phase Three scenarios."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="phase-three-owner",
            password="test-password",
        )
        self.manager = User.objects.create_user(
            username="phase-three-manager",
            password="test-password",
        )
        self.outsider = User.objects.create_user(
            username="phase-three-outsider",
            password="test-password",
        )
        self._competition_number = 0
        self._player_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        defaults = {
            "name": f"Phase Three Competition {self._competition_number}",
            "slug": f"phase-three-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "default_race_target": 1,
        }
        defaults.update(overrides)
        return PoolCompetition.objects.create(**defaults)

    def make_entries(self, competition, names):
        entries = []
        for name in names:
            self._player_number += 1
            player = PoolPlayer.objects.create(full_name=name)
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

    def make_match(self, competition, stage, home, away, *, group=None):
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            group=group,
            home_entry=home,
            away_entry=away,
        )

    def finish_played(self, pool_match, winner, *, loser_racks=0):
        """Finish a race using recorded racks, rather than synthetic scores."""
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
        return pool_match

    def finish_special(self, pool_match, winner, method=PoolMatch.ResultMethod.WALKOVER):
        result = record_non_played_result(
            pool_match,
            result_method=method,
            winner=winner,
            recorded_by=self.owner,
            reason="Phase Three test result",
        )
        result.refresh_from_db()
        return result

    @staticmethod
    def rows_by_entry(rows):
        return {row.entry_id: row for row in rows}


class PoolCalculatedStandingsTests(PoolPhaseThreeTestCase):
    def test_default_points_authoritative_racks_and_special_result_eligibility(self):
        competition = self.make_competition(default_race_target=2)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo, charlie, delta = self.make_entries(
            competition,
            ["Alpha", "Bravo", "Charlie", "Delta"],
        )

        # A normal played result supplies the real rack totals.
        self.finish_played(
            self.make_match(competition, stage, alpha, bravo),
            alpha,
            loser_racks=1,
        )
        # A walkover supplies a win/loss and points but deliberately no racks.
        self.finish_special(
            self.make_match(competition, stage, charlie, delta),
            charlie,
        )
        # A withdrawal preserves the genuine rack already recorded.
        withdrawal = self.make_match(competition, stage, bravo, charlie)
        start_match(withdrawal, recorded_by=self.owner)
        record_rack(
            withdrawal,
            winner=bravo,
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        self.finish_special(
            withdrawal,
            charlie,
            PoolMatch.ResultMethod.WITHDRAWAL,
        )

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual(
            (rows[alpha.pk].played, rows[alpha.pk].won, rows[alpha.pk].lost),
            (1, 1, 0),
        )
        self.assertEqual(
            (rows[alpha.pk].racks_for, rows[alpha.pk].racks_against, rows[alpha.pk].points),
            (2, 1, 2),
        )
        self.assertEqual(
            (rows[bravo.pk].played, rows[bravo.pk].won, rows[bravo.pk].lost),
            (2, 0, 2),
        )
        self.assertEqual(
            (rows[bravo.pk].racks_for, rows[bravo.pk].racks_against, rows[bravo.pk].points),
            (2, 2, 0),
        )
        self.assertEqual(
            (rows[charlie.pk].played, rows[charlie.pk].won, rows[charlie.pk].lost),
            (2, 2, 0),
        )
        self.assertEqual(
            (rows[charlie.pk].racks_for, rows[charlie.pk].racks_against, rows[charlie.pk].points),
            (0, 1, 4),
        )
        self.assertEqual(
            (rows[delta.pk].played, rows[delta.pk].racks_for, rows[delta.pk].racks_against),
            (1, 0, 0),
        )
        self.assertEqual(withdrawal.racks.count(), 1)

    def test_configurable_win_and_loss_points_and_correction_recalculate_immediately(self):
        competition = self.make_competition(points_for_win=3, points_for_loss=1)
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ["Alpha", "Bravo"])
        pool_match = self.finish_special(
            self.make_match(competition, stage, alpha, bravo), alpha
        )

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (3, 1))

        # An open audit correction removes the result from the derived table;
        # replacing the result updates it without a stored-table repair job.
        open_result_correction(pool_match, recorded_by=self.owner, reason="Correct winner")
        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].played, rows[bravo.pk].played), (0, 0))

        self.finish_special(pool_match, bravo, PoolMatch.ResultMethod.DISQUALIFICATION)
        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (1, 3))

    def test_non_counting_states_do_not_enter_the_table(self):
        competition = self.make_competition()
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ["Alpha", "Bravo"])

        upcoming = self.make_match(competition, stage, alpha, bravo)
        postponed = self.make_match(competition, stage, alpha, bravo)
        transition_match_status(postponed, status=PoolMatch.Status.POSTPONED)
        cancelled = self.make_match(competition, stage, alpha, bravo)
        transition_match_status(cancelled, status=PoolMatch.Status.CANCELLED)
        live = self.make_match(competition, stage, alpha, bravo)
        start_match(live, recorded_by=self.owner)
        abandoned = self.make_match(competition, stage, alpha, bravo)
        start_match(abandoned, recorded_by=self.owner)
        transition_match_status(abandoned, status=PoolMatch.Status.ABANDONED)

        for pool_match in (upcoming, postponed, cancelled, live, abandoned):
            pool_match.refresh_from_db()

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual({match.status for match in [upcoming, postponed, cancelled, live, abandoned]}, {
            PoolMatch.Status.UPCOMING,
            PoolMatch.Status.POSTPONED,
            PoolMatch.Status.CANCELLED,
            PoolMatch.Status.LIVE,
            PoolMatch.Status.ABANDONED,
        })
        self.assertEqual((rows[alpha.pk].played, rows[bravo.pk].played), (0, 0))
        self.assertEqual((rows[alpha.pk].points, rows[bravo.pk].points), (0, 0))

    def test_team_tables_and_group_tables_are_strictly_scoped(self):
        team_competition = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            slug="phase-three-team-table",
            name="Phase Three Team Table",
        )
        team_stage = self.stage_for(team_competition, PoolCompetitionStage.StageType.LEAGUE)
        red = PoolCompetitionEntry.objects.create(
            competition=team_competition,
            team=PoolTeam.objects.create(name="Red Team"),
        )
        blue = PoolCompetitionEntry.objects.create(
            competition=team_competition,
            team=PoolTeam.objects.create(name="Blue Team"),
        )
        self.finish_special(self.make_match(team_competition, team_stage, red, blue), red)
        team_rows = self.rows_by_entry(standings_for_stage(team_stage))
        self.assertEqual(team_rows[red.pk].display_name, "Red Team")
        self.assertEqual((team_rows[red.pk].won, team_rows[blue.pk].lost), (1, 1))

        group_competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            slug="phase-three-group-table",
            name="Phase Three Group Table",
        )
        group_stage = self.stage_for(group_competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Groups"
        group_stage.save()
        group_a = PoolGroup.objects.create(stage=group_stage, name="Group A", order=1)
        group_b = PoolGroup.objects.create(stage=group_stage, name="Group B", order=2)
        a_one, a_two, b_one, b_two = self.make_entries(
            group_competition,
            ["A One", "A Two", "B One", "B Two"],
        )
        for group, entries in ((group_a, (a_one, a_two)), (group_b, (b_one, b_two))):
            for order, entry in enumerate(entries, start=1):
                PoolGroupEntry.objects.create(
                    stage=group_stage,
                    group=group,
                    entry=entry,
                    sort_order=order,
                )
        self.finish_special(
            self.make_match(group_competition, group_stage, a_one, a_two, group=group_a),
            a_one,
        )
        self.finish_special(
            self.make_match(group_competition, group_stage, b_one, b_two, group=group_b),
            b_two,
        )

        rows_a = self.rows_by_entry(standings_for_stage(group_stage, group=group_a))
        rows_b = self.rows_by_entry(standings_for_stage(group_stage, group=group_b))
        self.assertEqual(set(rows_a), {a_one.pk, a_two.pk})
        self.assertEqual(set(rows_b), {b_one.pk, b_two.pk})
        self.assertEqual((rows_a[a_one.pk].won, rows_b[b_two.pk].won), (1, 1))
        with self.assertRaises(PoolStandingsValidationError):
            standings_for_stage(group_stage)

    def test_head_to_head_mini_table_and_deterministic_fallback(self):
        competition = self.make_competition()
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo, charlie, delta, echo = self.make_entries(
            competition,
            ["Alpha", "Bravo", "Charlie", "Delta", "Echo"],
        )
        replace_pool_tie_breakers(
            competition,
            [PoolCompetitionTieBreaker.Criterion.HEAD_TO_HEAD],
        )

        # Alpha, Bravo, and Charlie all finish on four points, but their
        # direct three-entry mini-table is Alpha (4), Bravo (2), Charlie (0).
        for home, away, winner in (
            (alpha, bravo, alpha),
            (alpha, charlie, alpha),
            (bravo, charlie, bravo),
            (delta, alpha, delta),
            (delta, alpha, delta),
            (bravo, delta, bravo),
            (charlie, delta, charlie),
            (charlie, delta, charlie),
            (delta, echo, delta),
        ):
            self.finish_played(
                self.make_match(competition, stage, home, away),
                winner,
            )

        rows = standings_for_stage(stage)
        tied_names = [row.display_name for row in rows if row.points == 4]
        self.assertEqual(tied_names, ["Alpha", "Bravo", "Charlie"])

        fallback_competition = self.make_competition(
            slug="phase-three-h2h-fallback",
            name="Phase Three H2H Fallback",
        )
        fallback_stage = self.stage_for(
            fallback_competition,
            PoolCompetitionStage.StageType.LEAGUE,
        )
        self.make_entries(fallback_competition, ["Zulu", "Able"])
        replace_pool_tie_breakers(
            fallback_competition,
            [PoolCompetitionTieBreaker.Criterion.HEAD_TO_HEAD],
        )
        self.assertEqual(
            [row.display_name for row in standings_for_stage(fallback_stage)],
            ["Able", "Zulu"],
        )

    def test_tie_breaker_replacement_reorders_atomically(self):
        competition = self.make_competition()
        self.assertEqual(configured_pool_tie_breakers(competition), DEFAULT_TIE_BREAKERS)

        first = [
            PoolCompetitionTieBreaker.Criterion.RACKS_WON,
            PoolCompetitionTieBreaker.Criterion.MATCHES_WON,
            PoolCompetitionTieBreaker.Criterion.HEAD_TO_HEAD,
        ]
        replace_pool_tie_breakers(competition, first)
        self.assertEqual(configured_pool_tie_breakers(competition), tuple(first))

        # This is a direct swap of unique order values.  The service performs
        # an atomic replacement rather than a collision-prone row update.
        second = [
            PoolCompetitionTieBreaker.Criterion.MATCHES_WON,
            PoolCompetitionTieBreaker.Criterion.RACKS_WON,
        ]
        replace_pool_tie_breakers(competition, second)
        self.assertEqual(configured_pool_tie_breakers(competition), tuple(second))
        self.assertEqual(
            list(
                competition.tie_breakers.order_by("order").values_list(
                    "order", "criterion"
                )
            ),
            [(1, second[0]), (2, second[1])],
        )

    def test_recent_form_uses_the_latest_five_eligible_results(self):
        competition = self.make_competition(
            slug="phase-three-recent-form",
            name="Phase Three Recent Form",
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ["Alpha", "Bravo"])
        for winner in (alpha, bravo, alpha, bravo, alpha, bravo):
            self.finish_played(
                self.make_match(competition, stage, alpha, bravo),
                winner,
            )

        rows = self.rows_by_entry(standings_for_stage(stage))
        self.assertEqual(rows[alpha.pk].recent_form, ("L", "W", "L", "W", "L"))
        self.assertEqual(rows[bravo.pk].recent_form, ("W", "L", "W", "L", "W"))


class PoolPersistentBracketTests(PoolPhaseThreeTestCase):
    def make_knockout(self, size, *, third_place=False):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            third_place_match_enabled=third_place,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.KNOCKOUT)
        entries = self.make_entries(
            competition,
            [f"Entrant {number}" for number in range(1, size + 1)],
        )
        return competition, stage, entries

    def generate_manual_bracket(self, entries, competition, stage, *, third_place=False, targets=None):
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=len(entries),
            manual_entry_ids=[entry.pk for entry in entries],
            third_place_enabled=third_place,
            round_race_targets=targets or {},
            actor=self.owner,
        )
        return generate_bracket_from_preview(preview, actor=self.owner)

    def test_supported_persistent_topologies_have_explicit_slots(self):
        for size in (2, 4, 8, 16, 32, 64, 128, 256):
            with self.subTest(size=size):
                competition, stage, entries = self.make_knockout(size)
                bracket = self.generate_manual_bracket(entries, competition, stage)
                self.assertEqual(bracket.size, size)
                self.assertEqual(bracket.ties.count(), size - 1)
                self.assertEqual(
                    PoolKnockoutTieSlot.objects.filter(tie__bracket=bracket).count(),
                    2 * (size - 1),
                )
                self.assertEqual(
                    bracket.ties.filter(round_number=1, is_third_place=False).count(),
                    size // 2,
                )
                self.assertEqual(
                    PoolMatch.objects.filter(stage=stage).count(),
                    size // 2,
                )

    def test_large_bracket_confirmation_query_growth_is_bounded(self):
        """A larger draw must not restore one lock/query per generated tie."""

        small_competition, small_stage, small_entries = self.make_knockout(32)
        small_preview = create_draw_preview(
            small_competition,
            stage=small_stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=32,
            manual_entry_ids=[entry.pk for entry in small_entries],
            actor=self.owner,
        )
        large_competition, large_stage, large_entries = self.make_knockout(256)
        large_preview = create_draw_preview(
            large_competition,
            stage=large_stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=256,
            manual_entry_ids=[entry.pk for entry in large_entries],
            actor=self.owner,
        )

        # Entry setup itself intentionally exercises model-level safeguards
        # and can fill Django's debug query log.  Start each measurement from
        # an empty log so the captured counts remain meaningful.
        reset_queries()
        with CaptureQueriesContext(connection) as small_queries:
            generate_bracket_from_preview(small_preview, actor=self.owner)
        reset_queries()
        with CaptureQueriesContext(connection) as large_queries:
            generate_bracket_from_preview(large_preview, actor=self.owner)

        # SQLite may split large bulk statements to respect its bind-variable
        # ceiling, so retain a little backend headroom.  This still fails if
        # confirmation regresses to repeated slot lookups/locks per tie.
        self.assertLessEqual(
            len(large_queries),
            len(small_queries) + 40,
            (len(small_queries), len(large_queries)),
        )

    def test_large_ordinary_manual_draw_uses_hidden_search_picker_fields(self):
        competition, _stage, entries = self.make_knockout(64)

        form = PoolBracketDrawForm(competition=competition)

        self.assertTrue(form.large_manual_slots)
        self.assertEqual(form.manual_half_size, 32)
        self.assertTrue(form.fields["slot_1"].widget.is_hidden)
        self.assertTrue(form.fields["slot_64"].widget.is_hidden)
        self.assertEqual(len(form.large_manual_slot_fields), 64)
        self.assertEqual(
            {option["id"] for option in form.manual_slot_options},
            {entry.pk for entry in entries},
        )
        self.assertIn("entry #", form.manual_slot_options[0]["search_label"])

    def test_manual_bracket_size_refresh_renders_64_slots_without_creating_records(self):
        competition, stage, _entries = self.make_knockout(4)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_competition_bracket", args=[competition.slug]),
            {
                "stage": stage.pk,
                "bracket_size": 64,
                "draw_method": "manual",
            },
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["draw_form"]
        self.assertFalse(form.is_bound)
        self.assertEqual(form.selected_size, 64)
        self.assertEqual(form.initial["bracket_size"], "64")
        self.assertEqual(form.initial["draw_method"], "MANUAL")
        self.assertEqual(len(form.large_manual_slot_fields), 64)
        self.assertIn("slot_64", form.fields)
        self.assertContains(response, 'searchParams.set("bracket_size", bracketSize.value)')
        self.assertContains(response, 'searchParams.set("draw_method", "MANUAL")')
        self.assertFalse(PoolBracketDrawPreview.objects.filter(stage=stage).exists())
        self.assertFalse(PoolKnockoutBracket.objects.filter(stage=stage).exists())
        self.assertFalse(PoolMatch.objects.filter(stage=stage).exists())

    def test_manual_bracket_size_refresh_renders_32_slots(self):
        competition, stage, _entries = self.make_knockout(4)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_competition_bracket", args=[competition.slug]),
            {
                "stage": stage.pk,
                "bracket_size": 32,
                "draw_method": PoolBracketDrawPreview.DrawMethod.MANUAL,
            },
        )

        form = response.context["draw_form"]
        self.assertEqual(form.selected_size, 32)
        self.assertEqual(len(form.small_manual_slot_fields), 32)
        self.assertIn("slot_32", form.fields)
        self.assertNotIn("slot_33", form.fields)

    def test_manual_preview_and_confirmation_work_after_size_refresh(self):
        competition, stage, entries = self.make_knockout(4)
        self.client.force_login(self.owner)
        setup_url = reverse("pool:manage_competition_bracket", args=[competition.slug])
        refreshed = self.client.get(
            setup_url,
            {
                "stage": stage.pk,
                "bracket_size": 4,
                "draw_method": PoolBracketDrawPreview.DrawMethod.MANUAL,
            },
        )
        self.assertEqual(refreshed.context["draw_form"].selected_size, 4)

        preview_response = self.client.post(
            reverse("pool:manage_competition_bracket_preview", args=[competition.slug]),
            {
                "stage": stage.pk,
                "draw_method": PoolBracketDrawPreview.DrawMethod.MANUAL,
                "bracket_size": 4,
                "slot_1": entries[0].pk,
                "slot_2": entries[1].pk,
                "slot_3": entries[2].pk,
                "slot_4": entries[3].pk,
                "round_race_target_1": 1,
                "round_race_target_2": 1,
            },
        )
        self.assertEqual(preview_response.status_code, 302)
        preview = PoolBracketDrawPreview.objects.get(
            competition=competition,
            stage=stage,
            created_by=self.owner,
        )
        self.assertEqual(preview.bracket_size, 4)
        self.assertFalse(PoolKnockoutBracket.objects.filter(stage=stage).exists())

        confirmation_response = self.client.post(
            reverse("pool:manage_competition_bracket_generate", args=[competition.slug]),
            {"token": preview.token, "confirm": "on"},
        )
        self.assertRedirects(
            confirmation_response,
            f"{setup_url}?stage={stage.pk}",
        )
        self.assertTrue(PoolKnockoutBracket.objects.filter(stage=stage, size=4).exists())

    def test_non_manual_size_query_does_not_change_random_draw_behavior(self):
        competition, stage, _entries = self.make_knockout(4)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_competition_bracket", args=[competition.slug]),
            {
                "stage": stage.pk,
                "bracket_size": 64,
                "draw_method": PoolBracketDrawPreview.DrawMethod.RANDOM,
            },
        )

        form = response.context["draw_form"]
        self.assertEqual(form.selected_size, 64)
        self.assertEqual(form["draw_method"].value(), "RANDOM")
        self.assertFalse(PoolKnockoutBracket.objects.filter(stage=stage).exists())
        self.assertFalse(PoolMatch.objects.filter(stage=stage).exists())

    def test_random_draw_has_no_preview_and_is_persisted_once_on_confirmation(self):
        competition, stage, entries = self.make_knockout(4)
        with self.assertRaisesRegex(
            PoolBracketValidationError,
            "confirmed once without a preview",
        ):
            create_draw_preview(
                competition,
                stage=stage,
                draw_method=PoolBracketDrawPreview.DrawMethod.RANDOM,
                bracket_size=4,
                actor=self.owner,
            )
        self.assertFalse(
            PoolBracketDrawPreview.objects.filter(
                competition=competition,
                stage=stage,
            ).exists()
        )

        bracket = generate_random_bracket(
            competition,
            stage=stage,
            bracket_size=4,
            actor=self.owner,
            confirmed=True,
        )
        preview = bracket.confirmed_preview

        self.assertEqual(
            bracket.draw_method,
            PoolBracketDrawPreview.DrawMethod.RANDOM,
        )
        self.assertIsNotNone(preview)
        self.assertEqual(
            preview.draw_method,
            PoolBracketDrawPreview.DrawMethod.RANDOM,
        )
        self.assertEqual(bracket.draw_order, preview.draw_order)
        self.assertEqual(PoolMatch.objects.filter(stage=stage).count(), 2)
        self.assertEqual(set(entry.pk for entry in entries), set(bracket.draw_order))

        # A direct repeat cannot turn the one-shot draw into a redraw, and
        # even an untouched random bracket is not resettable by the normal
        # manager-facing service.
        with self.assertRaisesRegex(
            PoolBracketValidationError,
            "already been confirmed",
        ):
            generate_random_bracket(
                competition,
                stage=stage,
                bracket_size=4,
                actor=self.owner,
                confirmed=True,
            )
        with self.assertRaisesRegex(
            PoolBracketValidationError,
            "already been confirmed",
        ):
            reset_bracket(bracket, actor=self.owner, reason="Attempt redraw")
        bracket.refresh_from_db()
        self.assertEqual(bracket.draw_order, preview.draw_order)

    def test_random_draw_http_confirmation_does_not_create_a_discardable_preview(self):
        competition, stage, entries = self.make_knockout(4)
        self.client.force_login(self.owner)
        url = reverse("pool:manage_competition_bracket_preview", args=[competition.slug])
        payload = {
            "stage": stage.pk,
            "draw_method": PoolBracketDrawPreview.DrawMethod.RANDOM,
            "bracket_size": 4,
            "round_race_target_1": 1,
            "round_race_target_2": 1,
        }

        unconfirmed = self.client.post(url, payload)
        self.assertEqual(unconfirmed.status_code, 200)
        self.assertIsNone(unconfirmed.context["preview"])
        self.assertContains(unconfirmed, "one-shot random draw is final")
        self.assertFalse(PoolBracketDrawPreview.objects.filter(stage=stage).exists())
        self.assertFalse(PoolKnockoutTie.objects.filter(bracket__stage=stage).exists())
        for entry in entries:
            self.assertNotContains(unconfirmed, entry.display_name)

        response = self.client.post(url, {**payload, "confirm_random_draw": "on"})
        self.assertRedirects(
            response,
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}",
        )
        bracket = PoolKnockoutTie.objects.get(
            bracket__stage=stage,
            round_number=1,
            position=1,
        ).bracket
        original_order = list(bracket.draw_order)
        self.assertEqual(bracket.confirmed_preview.draw_method, "RANDOM")

        # A direct repeat of the POST is rejected server-side rather than
        # relying on the hidden/reset UI controls.
        repeat = self.client.post(url, {**payload, "confirm_random_draw": "on"})
        self.assertRedirects(
            repeat,
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}",
        )
        bracket.refresh_from_db()
        self.assertEqual(bracket.draw_order, original_order)
        self.assertEqual(PoolKnockoutTie.objects.filter(bracket=bracket).count(), 3)

    def test_manual_draw_still_creates_a_reviewable_preview(self):
        competition, stage, entries = self.make_knockout(4)

        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )

        self.assertEqual(preview.draw_order, [entry.pk for entry in entries])
        self.assertFalse(PoolKnockoutTie.objects.filter(bracket__stage=stage).exists())
        self.assertEqual(
            generate_bracket_from_preview(preview, actor=self.owner).confirmed_preview_id,
            preview.pk,
        )

    def test_two_slot_third_place_is_rejected_by_draw_form_and_service(self):
        competition, stage, entries = self.make_knockout(2)
        form = PoolBracketDrawForm(
            {
                "stage": stage.pk,
                "draw_method": PoolBracketDrawPreview.DrawMethod.MANUAL,
                "bracket_size": 2,
                "third_place_enabled": "on",
                "third_place_race_target": 1,
                "slot_1": entries[0].pk,
                "slot_2": entries[1].pk,
                "round_race_target_1": 1,
            },
            competition=competition,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("third_place_enabled", form.errors)

        with self.assertRaises(PoolBracketValidationError):
            create_draw_preview(
                competition,
                stage=stage,
                draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
                bracket_size=2,
                manual_entry_ids=[entry.pk for entry in entries],
                third_place_enabled=True,
                actor=self.owner,
            )

    def test_only_the_exact_confirmed_preview_is_idempotent(self):
        competition, stage, entries = self.make_knockout(4)
        first_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_entry_ids=[entry.pk for entry in entries],
            third_place_enabled=False,
            round_race_targets={"1": 2, "2": 3},
            actor=self.owner,
        )
        different_settings_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_entry_ids=[entry.pk for entry in entries],
            third_place_enabled=True,
            round_race_targets={"1": 5, "2": 7, "third_place": 3},
            actor=self.owner,
        )

        bracket = generate_bracket_from_preview(first_preview, actor=self.owner)
        self.assertEqual(bracket.confirmed_preview_id, first_preview.pk)
        with self.assertRaises(PoolBracketValidationError):
            generate_bracket_from_preview(different_settings_preview, actor=self.owner)

        bracket.refresh_from_db()
        self.assertFalse(bracket.third_place_enabled)
        self.assertEqual(
            list(
                bracket.round_rules.order_by("round_number", "is_third_place").values_list(
                    "round_number", "is_third_place", "race_target"
                )
            ),
            [(1, False, 2), (2, False, 3)],
        )

    def test_direct_finished_bye_and_scored_walkover_fail_model_validation(self):
        competition = self.make_competition()
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ["Alpha", "Bravo"])
        pool_match = self.make_match(competition, stage, alpha, bravo)

        pool_match.status = PoolMatch.Status.FINISHED
        pool_match.winner = alpha
        pool_match.result_method = PoolMatch.ResultMethod.BYE
        with self.assertRaises(ValidationError):
            pool_match.full_clean()

        pool_match.result_method = PoolMatch.ResultMethod.WALKOVER
        pool_match.home_score = 1
        with self.assertRaises(ValidationError):
            pool_match.full_clean()

    def test_seeded_draw_validates_seeds_and_preserves_all_entrants(self):
        competition, stage, entries = self.make_knockout(4)
        for seed, entry in enumerate(entries, start=1):
            entry.seed = seed
            entry.save()
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.SEEDED,
            bracket_size=4,
            actor=self.owner,
        )
        self.assertEqual(set(preview.draw_order), {entry.pk for entry in entries})

        missing_competition, missing_stage, missing_entries = self.make_knockout(2)
        missing_entries[0].seed = 1
        missing_entries[0].save()
        with self.assertRaises(PoolBracketValidationError):
            create_draw_preview(
                missing_competition,
                stage=missing_stage,
                draw_method=PoolBracketDrawPreview.DrawMethod.SEEDED,
                bracket_size=2,
            )

        duplicate_competition, duplicate_stage, duplicate_entries = self.make_knockout(2)
        for entry in duplicate_entries:
            entry.seed = 1
            entry.save()
        with self.assertRaises(PoolBracketValidationError):
            create_draw_preview(
                duplicate_competition,
                stage=duplicate_stage,
                draw_method=PoolBracketDrawPreview.DrawMethod.SEEDED,
                bracket_size=2,
            )

    def test_manual_draw_rejects_duplicates_and_cross_competition_entries(self):
        competition, stage, entries = self.make_knockout(2)
        other_competition, _other_stage, other_entries = self.make_knockout(2)

        with self.assertRaises(PoolBracketValidationError):
            create_draw_preview(
                competition,
                stage=stage,
                draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
                bracket_size=2,
                manual_entry_ids=[entries[0].pk, entries[0].pk],
            )
        with self.assertRaises(PoolBracketValidationError):
            create_draw_preview(
                competition,
                stage=stage,
                draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
                bracket_size=2,
                manual_entry_ids=[entries[0].pk, other_entries[0].pk],
            )
        self.assertFalse(PoolBracketDrawPreview.objects.filter(competition=other_competition).exists())

    def test_bye_advances_without_a_match_or_rack_history_and_can_be_reset_untouched(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.KNOCKOUT)
        entries = self.make_entries(competition, ["Alpha", "Bravo", "Charlie"])
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_entry_ids=[entries[0].pk, None, entries[1].pk, entries[2].pk],
            actor=self.owner,
        )
        bracket = generate_bracket_from_preview(preview, actor=self.owner)

        bye_ties = bracket.ties.filter(
            resolution_method=PoolKnockoutTie.ResolutionMethod.BYE
        )
        self.assertEqual(bye_ties.count(), 1)
        self.assertIsNone(bye_ties.get().match)
        self.assertEqual(PoolMatch.objects.filter(stage=stage).count(), 1)
        self.assertEqual(PoolRack.objects.filter(match__stage=stage).count(), 0)

        # A bye is topology, not competitive activity.  An otherwise untouched
        # draw remains safe for the owner to reset.
        self.assertTrue(reset_bracket(bracket, actor=self.owner, reason="Test reset"))
        self.assertFalse(PoolKnockoutTie.objects.filter(bracket_id=bracket.pk).exists())
        self.assertFalse(PoolBracketDrawPreview.objects.filter(pk=preview.pk).exists())

    def test_winner_and_semifinal_loser_progression_with_round_targets(self):
        competition, stage, entries = self.make_knockout(4, third_place=True)
        bracket = self.generate_manual_bracket(
            entries,
            competition,
            stage,
            third_place=True,
            targets={"1": 3, "2": 5, "third_place": 2},
        )
        semis = list(
            bracket.ties.filter(round_number=1, is_third_place=False).order_by("position")
        )
        self.assertEqual([tie.match.race_target for tie in semis], [3, 3])

        self.finish_special(semis[0].match, entries[0])
        self.finish_special(semis[1].match, entries[2])
        final = bracket.ties.get(round_number=2, is_third_place=False)
        third = bracket.ties.get(is_third_place=True)
        final.refresh_from_db()
        third.refresh_from_db()
        self.assertIsNotNone(final.match)
        self.assertIsNotNone(third.match)
        self.assertEqual(final.match.race_target, 5)
        self.assertEqual(third.match.race_target, 2)
        self.assertEqual(
            {
                slot.entry_id
                for slot in final.slots.all()
            },
            {entries[0].pk, entries[2].pk},
        )
        self.assertEqual(
            {
                slot.entry_id
                for slot in third.slots.all()
            },
            {entries[1].pk, entries[3].pk},
        )

    def test_correction_clears_unstarted_dependent_but_blocks_started_dependency(self):
        competition, stage, entries = self.make_knockout(4)
        bracket = self.generate_manual_bracket(entries, competition, stage)
        semi_one, semi_two = list(bracket.ties.filter(round_number=1).order_by("position"))
        self.finish_special(semi_one.match, entries[0])
        self.finish_special(semi_two.match, entries[2])
        final = bracket.ties.get(round_number=2, is_third_place=False)
        final_match_id = final.match_id

        open_result_correction(semi_one.match, recorded_by=self.owner, reason="Safe correction")
        final.refresh_from_db()
        self.assertIsNone(final.match_id)
        self.assertFalse(PoolMatch.objects.filter(pk=final_match_id).exists())

        # Rebuild a fresh bracket so a downstream final can be started.  A
        # source result then cannot be opened because replacement is unsafe.
        competition, stage, entries = self.make_knockout(4)
        bracket = self.generate_manual_bracket(entries, competition, stage)
        semi_one, semi_two = list(bracket.ties.filter(round_number=1).order_by("position"))
        self.finish_special(semi_one.match, entries[0])
        self.finish_special(semi_two.match, entries[2])
        final = bracket.ties.get(round_number=2, is_third_place=False)
        start_match(final.match, recorded_by=self.owner)
        with self.assertRaises(PoolResultValidationError):
            open_result_correction(
                semi_one.match,
                recorded_by=self.owner,
                reason="Unsafe correction",
            )

    def test_empty_postponed_dependent_can_be_replaced_by_a_source_correction(self):
        competition, stage, entries = self.make_knockout(4)
        bracket = self.generate_manual_bracket(entries, competition, stage)
        semi_one, semi_two = list(
            bracket.ties.filter(round_number=1).order_by("position")
        )
        self.finish_special(semi_one.match, entries[0])
        self.finish_special(semi_two.match, entries[2])
        final = bracket.ties.get(round_number=2, is_third_place=False)
        final_match_id = final.match_id
        transition_match_status(
            final.match,
            status=PoolMatch.Status.POSTPONED,
            recorded_by=self.owner,
            reason="Weather delay before play",
        )

        # Postponed is unstarted and has no rack/result history.  Replacing a
        # source participant must therefore remain safe, just like an empty
        # upcoming fixture.
        open_result_correction(
            semi_one.match,
            recorded_by=self.owner,
            reason="Correct an unplayed dependent",
        )
        final.refresh_from_db()
        self.assertIsNone(final.match_id)
        self.assertFalse(PoolMatch.objects.filter(pk=final_match_id).exists())

    def test_generated_bracket_fixture_cannot_be_cancelled_or_abandoned(self):
        competition, stage, entries = self.make_knockout(2)
        bracket = self.generate_manual_bracket(entries, competition, stage)
        tie = bracket.ties.get(round_number=1, is_third_place=False)

        with self.assertRaises(PoolResultValidationError):
            transition_match_status(
                tie.match,
                status=PoolMatch.Status.CANCELLED,
                recorded_by=self.owner,
                reason="A generated bracket fixture cannot be cancelled",
            )
        tie.match.refresh_from_db()
        self.assertEqual(tie.match.status, PoolMatch.Status.UPCOMING)

        start_match(tie.match, recorded_by=self.owner)
        with self.assertRaises(PoolResultValidationError):
            transition_match_status(
                tie.match,
                status=PoolMatch.Status.ABANDONED,
                recorded_by=self.owner,
                reason="A generated bracket fixture cannot be abandoned",
            )
        tie.match.refresh_from_db()
        self.assertEqual(tie.match.status, PoolMatch.Status.LIVE)

    def test_generic_non_played_service_never_accepts_a_bye(self):
        competition = self.make_competition()
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        alpha, bravo = self.make_entries(competition, ["Alpha", "Bravo"])
        pool_match = self.make_match(competition, stage, alpha, bravo)

        with self.assertRaises(PoolResultValidationError):
            record_non_played_result(
                pool_match,
                result_method=PoolMatch.ResultMethod.BYE,
                winner=alpha,
                recorded_by=self.owner,
                reason="Byes must be structural bracket propagation",
            )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.UPCOMING)
        self.assertEqual(pool_match.racks.count(), 0)


class PoolGroupQualificationTests(PoolPhaseThreeTestCase):
    def make_group_competition(self, *, same_group_first_tie=False):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        group_stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Group Stage"
        group_stage.save()
        knockout_stage = self.stage_for(
            competition,
            PoolCompetitionStage.StageType.KNOCKOUT,
        )
        group_a = PoolGroup.objects.create(stage=group_stage, name="Group A", order=1)
        group_b = PoolGroup.objects.create(stage=group_stage, name="Group B", order=2)
        a_one, a_two, b_one, b_two = self.make_entries(
            competition,
            ["A One", "A Two", "B One", "B Two"],
        )
        for group, group_entries in (
            (group_a, (a_one, a_two)),
            (group_b, (b_one, b_two)),
        ):
            for order, entry in enumerate(group_entries, start=1):
                PoolGroupEntry.objects.create(
                    stage=group_stage,
                    group=group,
                    entry=entry,
                    sort_order=order,
                )
        matches = {
            "a": self.make_match(competition, group_stage, a_one, a_two, group=group_a),
            "b": self.make_match(competition, group_stage, b_one, b_two, group=group_b),
        }
        qualification_slots = (
            (
                (group_a, 1, 1),
                (group_a, 2, 2),
                (group_b, 1, 3),
                (group_b, 2, 4),
            )
            if same_group_first_tie
            else (
                (group_a, 1, 1),
                (group_b, 2, 2),
                (group_b, 1, 3),
                (group_a, 2, 4),
            )
        )
        for group, position, destination in qualification_slots:
            PoolGroupQualificationRule.objects.create(
                source_stage=group_stage,
                group=group,
                knockout_stage=knockout_stage,
                position=position,
                destination_slot=destination,
                # This deliberately exercises an organiser-approved same-group
                # first-round pairing; the separation preference is disabled
                # for that fixture layout only.
                keep_groups_apart=not same_group_first_tie,
            )
        return {
            "competition": competition,
            "group_stage": group_stage,
            "knockout_stage": knockout_stage,
            "group_a": group_a,
            "group_b": group_b,
            "entries": (a_one, a_two, b_one, b_two),
            "matches": matches,
        }

    def generate_group_bracket(self, scenario):
        preview = create_draw_preview(
            scenario["competition"],
            stage=scenario["knockout_stage"],
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            bracket_size=4,
            actor=self.owner,
        )
        return generate_bracket_from_preview(preview, actor=self.owner)

    def test_qualification_preview_confirmation_locks_only_preview_row(self):
        scenario = self.make_group_competition()
        a_one, _a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        preview = create_draw_preview(
            scenario["competition"],
            stage=scenario["knockout_stage"],
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            bracket_size=4,
            actor=self.owner,
        )

        preview_manager = PoolBracketDrawPreview.objects
        with patch.object(
            preview_manager,
            "select_for_update",
            wraps=preview_manager.select_for_update,
        ) as select_for_update:
            bracket = generate_bracket_from_preview(preview, actor=self.owner)

        select_for_update.assert_called_once_with(of=("self",))
        self.assertEqual(bracket.confirmed_preview_id, preview.pk)

    def test_group_qualification_uses_calculated_tables_and_explicit_destinations(self):
        scenario = self.make_group_competition()
        with self.assertRaises(PoolBracketValidationError):
            self.generate_group_bracket(scenario)

        a_one, _a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        bracket = self.generate_group_bracket(scenario)
        slots = {
            slot.source_group_id: []
            for slot in PoolKnockoutTieSlot.objects.filter(tie__bracket=bracket)
            if slot.source_group_id
        }
        for slot in PoolKnockoutTieSlot.objects.filter(tie__bracket=bracket):
            if slot.source_group_id:
                slots[slot.source_group_id].append(slot)
                self.assertEqual(
                    slot.source_type,
                    PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
                )
        self.assertEqual(len(slots[scenario["group_a"].pk]), 2)
        self.assertEqual(len(slots[scenario["group_b"].pk]), 2)
        self.assertEqual(
            PoolKnockoutTieSlot.objects.get(
                tie__bracket=bracket,
                source_group=scenario["group_a"],
                source_position=1,
            ).entry_id,
            a_one.pk,
        )

    def test_qualification_tab_applies_current_standings_then_confirms_preview(self):
        scenario = self.make_group_competition()
        a_one, a_two, b_one, b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        apply_url = reverse(
            "pool:manage_competition_qualification_apply",
            args=[scenario["competition"].slug],
        )
        self.client.force_login(self.owner)

        applied = self.client.post(
            apply_url,
            {
                "source_stage": scenario["group_stage"].pk,
                "knockout_stage": scenario["knockout_stage"].pk,
            },
        )

        self.assertEqual(applied.status_code, 302)
        preview = PoolBracketDrawPreview.objects.get(
            competition=scenario["competition"],
            stage=scenario["knockout_stage"],
            created_by=self.owner,
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
        )
        self.assertEqual(preview.draw_order, [a_one.pk, b_two.pk, b_one.pk, a_two.pk])
        self.assertFalse(PoolKnockoutBracket.objects.filter(stage=preview.stage).exists())

        preview_page = self.client.get(applied["Location"])
        self.assertContains(preview_page, "Qualification Preview")
        self.assertContains(preview_page, "Qualified Participants")
        self.assertContains(preview_page, "Confirm &amp; Generate Knockout")
        self.assertContains(preview_page, "A One")
        self.assertContains(preview_page, "B Two")
        self.assertContains(preview_page, "A One vs B Two")

        confirmed = self.client.post(
            reverse(
                "pool:manage_competition_bracket_generate",
                args=[scenario["competition"].slug],
            ),
            {"token": preview.token, "confirm": "on"},
        )

        self.assertRedirects(
            confirmed,
            f"{reverse('pool:manage_competition_bracket', args=[scenario['competition'].slug])}?stage={scenario['knockout_stage'].pk}",
        )
        bracket = PoolKnockoutBracket.objects.get(stage=scenario["knockout_stage"])
        first_round = list(
            bracket.ties.filter(round_number=1, is_third_place=False).order_by("position")
        )
        self.assertEqual(
            [
                (tie.match.home_entry_id, tie.match.away_entry_id)
                for tie in first_round
            ],
            [(a_one.pk, b_two.pk), (b_one.pk, a_two.pk)],
        )

    def test_qualification_tab_refuses_unfinished_or_incomplete_group_rules(self):
        scenario = self.make_group_competition()
        apply_url = reverse(
            "pool:manage_competition_qualification_apply",
            args=[scenario["competition"].slug],
        )
        self.client.force_login(self.owner)

        unfinished = self.client.post(
            apply_url,
            {
                "source_stage": scenario["group_stage"].pk,
                "knockout_stage": scenario["knockout_stage"].pk,
            },
            follow=True,
        )
        self.assertContains(
            unfinished,
            "Group qualification is blocked while required group matches are upcoming, live, or postponed.",
        )
        self.assertFalse(PoolBracketDrawPreview.objects.exists())
        self.assertFalse(PoolKnockoutBracket.objects.exists())

        a_one, _a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        PoolGroupQualificationRule.objects.get(
            group=scenario["group_a"], position=2
        ).delete()
        incomplete = self.client.post(
            apply_url,
            {
                "source_stage": scenario["group_stage"].pk,
                "knockout_stage": scenario["knockout_stage"].pk,
            },
            follow=True,
        )
        self.assertContains(
            incomplete,
            "Configure Group A qualification positions 1 through 2 exactly once before generating the bracket.",
        )
        self.assertFalse(PoolBracketDrawPreview.objects.exists())
        self.assertFalse(PoolKnockoutBracket.objects.exists())

    def test_qualification_tab_confirmation_rejects_stale_group_standings(self):
        scenario = self.make_group_competition()
        a_one, a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        self.client.force_login(self.owner)
        applied = self.client.post(
            reverse(
                "pool:manage_competition_qualification_apply",
                args=[scenario["competition"].slug],
            ),
            {
                "source_stage": scenario["group_stage"].pk,
                "knockout_stage": scenario["knockout_stage"].pk,
            },
        )
        self.assertEqual(applied.status_code, 302)
        preview = PoolBracketDrawPreview.objects.get(
            competition=scenario["competition"],
            stage=scenario["knockout_stage"],
            created_by=self.owner,
        )

        open_result_correction(
            scenario["matches"]["a"],
            recorded_by=self.owner,
            reason="Correct Group A standing before bracket confirmation",
        )
        self.finish_special(scenario["matches"]["a"], a_two)
        stale = self.client.post(
            reverse(
                "pool:manage_competition_bracket_generate",
                args=[scenario["competition"].slug],
            ),
            {"token": preview.token, "confirm": "on"},
            follow=True,
        )

        self.assertContains(
            stale,
            "Group standings changed after this preview. Create and confirm a new draw preview.",
        )
        self.assertTrue(PoolBracketDrawPreview.objects.filter(pk=preview.pk).exists())
        self.assertFalse(PoolKnockoutBracket.objects.filter(stage=preview.stage).exists())

    def test_qualification_tab_does_not_replace_an_existing_bracket(self):
        scenario = self.make_group_competition()
        a_one, _a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        existing = self.generate_group_bracket(scenario)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse(
                "pool:manage_competition_qualification_apply",
                args=[scenario["competition"].slug],
            ),
            {
                "source_stage": scenario["group_stage"].pk,
                "knockout_stage": scenario["knockout_stage"].pk,
            },
            follow=True,
        )

        self.assertContains(response, "This knockout stage already has a generated bracket.")
        self.assertEqual(PoolKnockoutBracket.objects.get(stage=existing.stage), existing)
        self.assertEqual(PoolBracketDrawPreview.objects.filter(stage=existing.stage).count(), 1)

    def test_qualification_apply_requires_competition_management_access(self):
        scenario = self.make_group_competition()
        self.client.force_login(self.outsider)

        response = self.client.post(
            reverse(
                "pool:manage_competition_qualification_apply",
                args=[scenario["competition"].slug],
            ),
            {
                "source_stage": scenario["group_stage"].pk,
                "knockout_stage": scenario["knockout_stage"].pk,
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PoolBracketDrawPreview.objects.exists())

    def test_existing_bracket_tab_group_qualification_preview_still_works(self):
        scenario = self.make_group_competition()
        a_one, _a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_preview",
                args=[scenario["competition"].slug],
            ),
            {
                "stage": scenario["knockout_stage"].pk,
                "draw_method": PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
                "bracket_size": 4,
                "round_race_target_1": 1,
                "round_race_target_2": 1,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            PoolBracketDrawPreview.objects.filter(
                competition=scenario["competition"],
                stage=scenario["knockout_stage"],
                draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
                created_by=self.owner,
            ).exists()
        )

    def test_group_correction_replaces_safe_qualifier_slots_but_not_started_ones(self):
        scenario = self.make_group_competition()
        a_one, a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        bracket = self.generate_group_bracket(scenario)

        # The incoming bracket fixture is unstarted, so an audited correction
        # can safely exchange Group A's first and second positions.
        source = scenario["matches"]["a"]
        open_result_correction(source, recorded_by=self.owner, reason="Swap Group A")
        self.finish_special(source, a_two, PoolMatch.ResultMethod.DISQUALIFICATION)
        first_slot = PoolKnockoutTieSlot.objects.get(
            tie__bracket=bracket,
            source_group=scenario["group_a"],
            source_position=1,
        )
        self.assertEqual(first_slot.entry_id, a_two.pk)

        # Once an affected knockout fixture is live, the same group-result
        # correction is refused in the service layer.
        affected_tie = first_slot.tie
        affected_tie.refresh_from_db()
        start_match(affected_tie.match, recorded_by=self.owner)
        with self.assertRaises(PoolResultValidationError):
            open_result_correction(
                source,
                recorded_by=self.owner,
                reason="Unsafe Group A correction",
            )

    def test_group_qualifier_swap_in_one_unstarted_tie_is_updated_atomically(self):
        scenario = self.make_group_competition(same_group_first_tie=True)
        a_one, a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        bracket = self.generate_group_bracket(scenario)

        # Both Group A qualifier slots feed the same unstarted semi-final.
        # Reversing their table positions must update them as one safe batch,
        # not temporarily collide or leave one stale entrant in the fixture.
        open_result_correction(
            scenario["matches"]["a"],
            recorded_by=self.owner,
            reason="Reverse both Group A qualification slots",
        )
        self.finish_special(
            scenario["matches"]["a"],
            a_two,
            PoolMatch.ResultMethod.DISQUALIFICATION,
        )
        group_a_slots = list(
            PoolKnockoutTieSlot.objects.filter(
                tie__bracket=bracket,
                source_group=scenario["group_a"],
            ).order_by("source_position")
        )
        self.assertEqual([slot.entry_id for slot in group_a_slots], [a_two.pk, a_one.pk])
        self.assertEqual(group_a_slots[0].tie_id, group_a_slots[1].tie_id)
        group_a_slots[0].tie.match.refresh_from_db()
        self.assertEqual(
            {
                group_a_slots[0].tie.match.home_entry_id,
                group_a_slots[0].tie.match.away_entry_id,
            },
            {a_one.pk, a_two.pk},
        )

    def test_generated_qualification_bracket_locks_points_and_tie_breakers(self):
        scenario = self.make_group_competition()
        a_one, _a_two, b_one, _b_two = scenario["entries"]
        self.finish_special(scenario["matches"]["a"], a_one)
        self.finish_special(scenario["matches"]["b"], b_one)
        self.generate_group_bracket(scenario)

        competition = scenario["competition"]
        competition.points_for_win = 3
        with self.assertRaises(ValidationError):
            competition.full_clean()

        tie_breaker = competition.tie_breakers.order_by("order").first()
        tie_breaker.order = 9
        with self.assertRaises(ValidationError):
            tie_breaker.full_clean()
        with self.assertRaises(PoolStandingsValidationError):
            replace_pool_tie_breakers(
                competition,
                list(reversed(DEFAULT_TIE_BREAKERS)),
            )

    def test_non_qualifier_group_correction_is_allowed_despite_unrelated_started_fixture(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            slug="phase-three-unchanged-qualifier",
            name="Phase Three Unchanged Qualifier",
        )
        group_stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Group Stage"
        group_stage.save()
        knockout_stage = self.stage_for(
            competition,
            PoolCompetitionStage.StageType.KNOCKOUT,
        )
        group_a = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=1,
        )
        group_b = PoolGroup.objects.create(
            stage=group_stage,
            name="Group B",
            order=2,
            qualifiers=1,
        )
        a_one, a_two, a_three, b_one, b_two = self.make_entries(
            competition,
            ["A One", "A Two", "A Three", "B One", "B Two"],
        )
        for group, group_entries in (
            (group_a, (a_one, a_two, a_three)),
            (group_b, (b_one, b_two)),
        ):
            for order, entry in enumerate(group_entries, start=1):
                PoolGroupEntry.objects.create(
                    stage=group_stage,
                    group=group,
                    entry=entry,
                    sort_order=order,
                )

        # A One remains first even while the A Two v A Three result is being
        # corrected or temporarily excluded from calculated standings.
        self.finish_special(
            self.make_match(competition, group_stage, a_one, a_two, group=group_a),
            a_one,
        )
        self.finish_special(
            self.make_match(competition, group_stage, a_one, a_three, group=group_a),
            a_one,
        )
        non_qualifier_match = self.finish_special(
            self.make_match(competition, group_stage, a_two, a_three, group=group_a),
            a_two,
        )
        self.finish_special(
            self.make_match(competition, group_stage, b_one, b_two, group=group_b),
            b_one,
        )
        PoolGroupQualificationRule.objects.create(
            source_stage=group_stage,
            group=group_a,
            knockout_stage=knockout_stage,
            position=1,
            destination_slot=1,
        )
        PoolGroupQualificationRule.objects.create(
            source_stage=group_stage,
            group=group_b,
            knockout_stage=knockout_stage,
            position=1,
            destination_slot=2,
        )
        preview = create_draw_preview(
            competition,
            stage=knockout_stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            bracket_size=2,
            actor=self.owner,
        )
        bracket = generate_bracket_from_preview(preview, actor=self.owner)
        final = bracket.ties.get(round_number=1, is_third_place=False)
        start_match(final.match, recorded_by=self.owner)

        # This correction cannot change a qualifier, so a started unrelated
        # qualification fixture must not cause a blanket dependency block.
        open_result_correction(
            non_qualifier_match,
            recorded_by=self.owner,
            reason="Correct non-qualifier result",
        )
        non_qualifier_match.refresh_from_db()
        self.assertTrue(non_qualifier_match.is_in_correction_mode)


class PoolBracketManagerSecurityTests(PoolPhaseThreeTestCase):
    def test_manager_can_discard_only_their_own_scoped_unconfirmed_preview(self):
        competition, stage, entries = self.make_knockout_for_security()
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=self.manager,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        owner_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )
        manager_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.manager,
        )
        self.client.force_login(self.manager)

        foreign_response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_preview_discard",
                args=[competition.slug, stage.pk, owner_preview.token],
            )
        )
        self.assertEqual(foreign_response.status_code, 404)
        self.assertTrue(PoolBracketDrawPreview.objects.filter(pk=owner_preview.pk).exists())

        response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_preview_discard",
                args=[competition.slug, stage.pk, manager_preview.token],
            )
        )
        self.assertRedirects(
            response,
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}",
        )
        self.assertFalse(
            PoolBracketDrawPreview.objects.filter(pk=manager_preview.pk).exists()
        )

    def test_discard_rejects_a_preview_confirmed_by_a_bracket(self):
        competition, stage, entries = self.make_knockout_for_security()
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )
        generate_bracket_from_preview(preview, actor=self.owner)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_preview_discard",
                args=[competition.slug, stage.pk, preview.token],
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PoolBracketDrawPreview.objects.filter(pk=preview.pk).exists())

    def test_owner_can_discard_another_managers_stale_unconfirmed_preview(self):
        competition, stage, entries = self.make_knockout_for_security()
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=self.manager,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        stale_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.manager,
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_preview_discard",
                args=[competition.slug, stage.pk, stale_preview.token],
            )
        )

        self.assertRedirects(
            response,
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}",
        )
        self.assertFalse(
            PoolBracketDrawPreview.objects.filter(pk=stale_preview.pk).exists()
        )

    def test_reset_clears_confirmed_and_stale_stage_previews(self):
        competition, stage, entries = self.make_knockout_for_security()
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=self.manager,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        confirmed_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )
        stale_preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.manager,
        )
        bracket = generate_bracket_from_preview(confirmed_preview, actor=self.owner)

        self.assertTrue(reset_bracket(bracket, actor=self.owner))

        self.assertFalse(
            PoolBracketDrawPreview.objects.filter(
                pk__in=[confirmed_preview.pk, stale_preview.pk]
            ).exists()
        )

    def test_bracket_urls_are_competition_scoped_and_reset_is_owner_only(self):
        competition, stage, entries = self.make_knockout_for_security()
        self.generate_security_bracket(competition, stage, entries)
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=self.manager,
            role=PoolCompetitionMembership.Role.ADMIN,
        )

        self.client.force_login(self.outsider)
        response = self.client.get(
            reverse("pool:manage_competition_bracket", args=[competition.slug])
        )
        self.assertEqual(response.status_code, 404)

        self.client.force_login(self.manager)
        reset_response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_reset",
                args=[competition.slug, stage.pk],
            ),
            {"confirm": "on"},
        )
        self.assertEqual(reset_response.status_code, 403)

    def test_generic_manager_fixture_creation_rejects_knockout_stage_before_draw(self):
        competition, stage, entries = self.make_knockout_for_security()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("pool:manage_competition_fixture_create", args=[competition.slug]),
            {
                "stage": stage.pk,
                "group": "",
                "home_entry": entries[0].pk,
                "away_entry": entries[1].pk,
                "scheduled_at": "",
                "race_target": 1,
                "round_label": "Manual final",
                "venue": "",
                "stream_url": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bracket manager")
        self.assertFalse(PoolMatch.objects.filter(stage=stage).exists())

    def make_knockout_for_security(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.KNOCKOUT)
        entries = self.make_entries(competition, ["Security A", "Security B"])
        return competition, stage, entries

    def generate_security_bracket(self, competition, stage, entries):
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )
        return generate_bracket_from_preview(preview, actor=self.owner)


class PoolBracketRoundTargetUpdateTests(PoolPhaseThreeTestCase):
    """Regression coverage for the bracket-owned race-target batch path."""

    def make_bracket(self, *, size=8, participant_mode=PoolCompetition.ParticipantMode.SINGLES):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            participant_mode=participant_mode,
            default_race_target=3,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.KNOCKOUT)
        if participant_mode == PoolCompetition.ParticipantMode.TEAMS:
            entries = [
                PoolCompetitionEntry.objects.create(
                    competition=competition,
                    team=PoolTeam.objects.create(name=f"Round target team {number}"),
                )
                for number in range(1, size + 1)
            ]
        else:
            entries = self.make_entries(
                competition,
                [f"Round target entrant {number}" for number in range(1, size + 1)],
            )
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=size,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )
        return competition, stage, entries, generate_bracket_from_preview(
            preview,
            actor=self.owner,
        )

    @staticmethod
    def targets_for(bracket, *, round_targets=None):
        round_targets = round_targets or {}
        targets = {
            rule.pk: rule.race_target
            for rule in PoolKnockoutRoundRule.objects.filter(bracket=bracket)
        }
        for rule in PoolKnockoutRoundRule.objects.filter(bracket=bracket):
            target = round_targets.get(rule.round_number)
            if target is not None:
                targets[rule.pk] = target
        return targets

    def test_round_targets_stay_independent_across_rules_ties_and_existing_matches(self):
        _competition, _stage, _entries, bracket = self.make_bracket()

        update_bracket_round_targets(
            bracket,
            self.targets_for(bracket, round_targets={1: 5, 2: 7, 3: 9}),
            actor=self.owner,
        )

        expected = {1: 5, 2: 7, 3: 9}
        self.assertEqual(
            {
                rule.round_number: rule.race_target
                for rule in PoolKnockoutRoundRule.objects.filter(bracket=bracket)
            },
            expected,
        )
        for tie in PoolKnockoutTie.objects.filter(bracket=bracket):
            self.assertEqual(tie.race_target, expected[tie.round_number])
            if tie.match_id:
                self.assertEqual(tie.match.race_target, expected[tie.round_number])

    def test_changing_only_one_round_leaves_other_round_targets_unchanged(self):
        _competition, _stage, _entries, bracket = self.make_bracket()
        update_bracket_round_targets(
            bracket,
            self.targets_for(bracket, round_targets={1: 5, 2: 7, 3: 9}),
            actor=self.owner,
        )

        update_bracket_round_targets(
            bracket,
            self.targets_for(bracket, round_targets={2: 11}),
            actor=self.owner,
        )

        self.assertEqual(
            {
                rule.round_number: rule.race_target
                for rule in PoolKnockoutRoundRule.objects.filter(bracket=bracket)
            },
            {1: 5, 2: 11, 3: 9},
        )

    def test_future_tie_materialises_with_its_updated_round_target(self):
        _competition, _stage, entries, bracket = self.make_bracket()
        update_bracket_round_targets(
            bracket,
            self.targets_for(bracket, round_targets={3: 11}),
            actor=self.owner,
        )

        quarter_finals = list(
            bracket.ties.filter(round_number=1).order_by("position")
        )
        for tie, winner in zip(quarter_finals, entries[::2]):
            self.finish_special(tie.match, winner)
        semi_finals = list(
            bracket.ties.filter(round_number=2).order_by("position")
        )
        for tie in semi_finals:
            self.finish_special(tie.match, tie.match.home_entry)

        final = bracket.ties.get(round_number=3)
        final.refresh_from_db()
        self.assertIsNotNone(final.match_id)
        self.assertEqual(final.race_target, 11)
        self.assertEqual(final.match.race_target, 11)

    def test_upcoming_materialised_match_is_updated_without_a_model_save_per_fixture(self):
        _competition, _stage, _entries, bracket = self.make_bracket(size=16)
        targets = self.targets_for(
            bracket,
            round_targets={1: 5, 2: 7, 3: 9, 4: 11},
        )

        with patch.object(
            PoolMatch,
            "save",
            side_effect=AssertionError("round-target batch must not call PoolMatch.save()"),
        ):
            update_bracket_round_targets(bracket, targets, actor=self.owner)

        first_round_matches = PoolMatch.objects.filter(
            knockout_tie__bracket=bracket,
            knockout_tie__round_number=1,
        )
        self.assertEqual(first_round_matches.count(), 8)
        self.assertFalse(first_round_matches.exclude(race_target=5).exists())

    def test_started_tie_rejects_the_entire_multi_round_submission_from_the_manager(self):
        competition, stage, _entries, bracket = self.make_bracket()
        started_tie = bracket.ties.get(round_number=1, position=1)
        start_match(started_tie.match, recorded_by=self.owner)
        original_rules = dict(
            PoolKnockoutRoundRule.objects.filter(bracket=bracket).values_list(
                "pk", "race_target"
            )
        )
        original_ties = dict(
            PoolKnockoutTie.objects.filter(bracket=bracket).values_list("pk", "race_target")
        )
        original_matches = dict(
            PoolMatch.objects.filter(knockout_tie__bracket=bracket).values_list(
                "pk", "race_target"
            )
        )
        submitted = self.targets_for(bracket, round_targets={1: 5, 2: 7, 3: 9})
        post_data = {
            f"round_rule_{rule_id}": target
            for rule_id, target in submitted.items()
        }

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "pool:manage_competition_bracket_round_targets",
                args=[competition.slug, stage.pk],
            ),
            post_data,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            dict(
                PoolKnockoutRoundRule.objects.filter(bracket=bracket).values_list(
                    "pk", "race_target"
                )
            ),
            original_rules,
        )
        self.assertEqual(
            dict(
                PoolKnockoutTie.objects.filter(bracket=bracket).values_list(
                    "pk", "race_target"
                )
            ),
            original_ties,
        )
        self.assertEqual(
            dict(
                PoolMatch.objects.filter(knockout_tie__bracket=bracket).values_list(
                    "pk", "race_target"
                )
            ),
            original_matches,
        )

    def test_team_bracket_target_ceiling_and_public_bulk_update_protection_remain_intact(self):
        _competition, _stage, _entries, bracket = self.make_bracket(
            size=2,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        targets = self.targets_for(bracket, round_targets={1: 14})
        with self.assertRaisesMessage(
            PoolBracketValidationError,
            "A team match race target cannot exceed 13",
        ):
            update_bracket_round_targets(bracket, targets, actor=self.owner)

        match = bracket.ties.get(round_number=1).match
        with self.assertRaises(ValidationError):
            PoolMatch.objects.filter(pk=match.pk).update(race_target=5)
