"""Focused regression coverage for the simplified margin-points manager flow.

These tests deliberately exercise the threshold input seam rather than teaching
organisers about the persisted ``minimum_margin`` / ``maximum_margin`` ranges.
The authoritative standings service is still used for allocation.
"""

from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMarginPointRule,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
)
from .services import record_rack, start_match
from .standings import (
    PoolStandingsValidationError,
    configured_pool_margin_point_rules,
    derive_pool_margin_point_rules_from_thresholds,
    point_allocation_for_pool_match,
    replace_pool_standings_points_configuration,
)


User = get_user_model()


class PoolMarginUXTestCase(TestCase):
    """Factories restricted to the simplified threshold-editor contract."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="margin-ux-owner",
            password="test-password",
        )
        self._competition_number = 0
        self._player_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        defaults = {
            "name": f"Margin UX Competition {self._competition_number}",
            "slug": f"margin-ux-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "default_race_target": 4,
        }
        defaults.update(overrides)
        return PoolCompetition.objects.create(**defaults)

    def make_entries(self, competition, *names):
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
    def league_stage(competition):
        return competition.stages.get(stage_type=PoolCompetitionStage.StageType.LEAGUE)

    def finish_played_match(self, competition, home_entry, away_entry, *, home_score, away_score):
        """Finish a race using only the existing authoritative rack workflow."""
        match = PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home_entry,
            away_entry=away_entry,
            race_target=competition.default_race_target,
        )
        start_match(match, recorded_by=self.owner)
        # Record the eventual loser's racks first so the authoritative service
        # never closes the race before its complete score is represented.
        if home_score == competition.default_race_target:
            loser, loser_score = away_entry, away_score
            winner, winner_score = home_entry, home_score
        else:
            loser, loser_score = home_entry, home_score
            winner, winner_score = away_entry, away_score
        for _number in range(loser_score):
            record_rack(match, winner=loser, recorded_by=self.owner)
        for _number in range(winner_score):
            record_rack(match, winner=winner, recorded_by=self.owner)
        match.refresh_from_db()
        self.assertEqual(match.status, PoolMatch.Status.FINISHED)
        return match

    @staticmethod
    def ranges(rules):
        return [
            (
                rule.minimum_margin,
                rule.maximum_margin,
                rule.winner_points,
                rule.loser_points,
            )
            for rule in rules
        ]

    @staticmethod
    def two_band_thresholds(maximum_margin=4):
        return (
            {"up_to_margin": 3, "winner_points": 3, "loser_points": 1},
            {
                "up_to_margin": maximum_margin,
                "winner_points": 4,
                "loser_points": 0,
            },
        )

    def configure_threshold_policy(self, competition, thresholds):
        return replace_pool_standings_points_configuration(
            competition,
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            # These retained defaults are intentionally for non-played outcomes.
            points_for_win=3,
            points_for_loss=0,
            margin_rules=derive_pool_margin_point_rules_from_thresholds(
                competition,
                thresholds,
            ),
        )

    @staticmethod
    def standings_url(competition, **query):
        url = reverse("pool:manage_competition_standings", args=[competition.slug])
        return f"{url}?{urlencode(query)}" if query else url

    @staticmethod
    def threshold_post_data(*rules, points_mode=PoolCompetition.PointsMode.MARGIN_BASED):
        """Build the server-side progressive-enhancement formset payload."""
        data = {
            "action": "points",
            "points_mode": points_mode,
            "points_for_win": 3,
            "points_for_loss": 0,
            "margin_rules-TOTAL_FORMS": len(rules),
            "margin_rules-INITIAL_FORMS": 0,
            "margin_rules-MIN_NUM_FORMS": 0,
            "margin_rules-MAX_NUM_FORMS": 1000,
        }
        for number, rule in enumerate(rules):
            for field in ("up_to_margin", "winner_points", "loser_points"):
                value = rule.get(field, "")
                data[f"margin_rules-{number}-{field}"] = "" if value is None else value
        return data


class PoolMarginThresholdDerivationTests(PoolMarginUXTestCase):
    def test_two_threshold_rules_derive_the_persisted_contiguous_ranges(self):
        competition = self.make_competition()

        derived = derive_pool_margin_point_rules_from_thresholds(
            competition,
            self.two_band_thresholds(),
        )
        self.assertEqual(self.ranges(derived), [(1, 3, 3, 1), (4, 4, 4, 0)])

        _competition, saved_rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )
        self.assertEqual(self.ranges(saved_rules), [(1, 3, 3, 1), (4, 4, 4, 0)])

    def test_three_threshold_rules_derive_each_lower_bound_without_manual_ranges(self):
        competition = self.make_competition()
        derived = derive_pool_margin_point_rules_from_thresholds(
            competition,
            (
                {"up_to_margin": 1, "winner_points": 2, "loser_points": 1},
                {"up_to_margin": 3, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            )
        )

        self.assertEqual(
            self.ranges(derived),
            [(1, 1, 2, 1), (2, 3, 3, 1), (4, 4, 4, 0)],
        )

    def test_three_threshold_rules_allocate_each_derived_band(self):
        competition = self.make_competition(default_race_target=4)
        thresholds = (
            {"up_to_margin": 1, "winner_points": 2, "loser_points": 1},
            {"up_to_margin": 3, "winner_points": 3, "loser_points": 1},
            {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
        )
        competition, _rules = self.configure_threshold_policy(competition, thresholds)
        home, away = self.make_entries(competition, "Home", "Away")

        for scores, allocation in {
            (4, 3): (2, 1),
            (4, 2): (3, 1),
            (4, 1): (3, 1),
            (4, 0): (4, 0),
        }.items():
            with self.subTest(scores=scores):
                pool_match = self.finish_played_match(
                    competition,
                    home,
                    away,
                    home_score=scores[0],
                    away_score=scores[1],
                )
                self.assertEqual(
                    point_allocation_for_pool_match(competition, pool_match),
                    allocation,
                )

    def test_race_to_seven_policy_ends_at_seven_without_an_eight_plus_band(self):
        competition = self.make_competition(default_race_target=7)

        derived = derive_pool_margin_point_rules_from_thresholds(
            competition,
            (
                {"up_to_margin": 3, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 7, "winner_points": 4, "loser_points": 0},
            ),
        )

        self.assertEqual(self.ranges(derived), [(1, 3, 3, 1), (4, 7, 4, 0)])
        self.assertFalse(any(rule.maximum_margin is None for rule in derived))

    def test_incomplete_or_beyond_target_thresholds_are_rejected(self):
        competition = self.make_competition(default_race_target=4)
        for thresholds in (
            (
                {"up_to_margin": 2, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 3, "winner_points": 4, "loser_points": 0},
            ),
            (
                {"up_to_margin": 2, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 5, "winner_points": 4, "loser_points": 0},
            ),
        ):
            with self.subTest(thresholds=thresholds):
                with self.assertRaises(PoolStandingsValidationError):
                    derive_pool_margin_point_rules_from_thresholds(
                        competition,
                        thresholds,
                    )

        with self.assertRaises(PoolStandingsValidationError):
            replace_pool_standings_points_configuration(
                competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
                points_for_win=3,
                points_for_loss=0,
                margin_rules=((1, 2, 3, 1), (4, 4, 4, 0)),
            )

    def test_existing_table_fixture_override_sets_the_policy_maximum(self):
        competition = self.make_competition(default_race_target=4)
        home, away = self.make_entries(competition, "Home", "Away")
        PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home,
            away_entry=away,
            race_target=5,
        )

        with self.assertRaises(PoolStandingsValidationError):
            derive_pool_margin_point_rules_from_thresholds(
                competition,
                self.two_band_thresholds(),
            )

        derived = derive_pool_margin_point_rules_from_thresholds(
            competition,
            self.two_band_thresholds(maximum_margin=5),
        )
        self.assertEqual(self.ranges(derived), [(1, 3, 3, 1), (4, 5, 4, 0)])

    def test_active_margin_policy_blocks_race_target_increase_until_covered(self):
        competition = self.make_competition(default_race_target=4)
        competition, _rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )
        competition.default_race_target = 5

        with self.assertRaises(ValidationError):
            competition.save()

        competition.refresh_from_db()
        self.assertEqual(competition.default_race_target, 4)

    def test_active_margin_policy_blocks_uncovered_table_fixture_override(self):
        competition = self.make_competition(default_race_target=4)
        competition, _rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )
        home, away = self.make_entries(competition, "Home", "Away")
        match = PoolMatch(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home,
            away_entry=away,
            race_target=5,
        )

        with self.assertRaises(ValidationError):
            match.full_clean()

    def test_bulk_margin_rule_deletion_cannot_break_an_active_policy(self):
        competition = self.make_competition(default_race_target=4)
        competition, rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )

        with self.assertRaises(ValidationError):
            PoolCompetitionMarginPointRule.objects.filter(
                pk__in=[rule.pk for rule in rules]
            ).delete()

        self.assertEqual(
            PoolCompetitionMarginPointRule.objects.filter(
                competition=competition
            ).count(),
            2,
        )

    def test_bulk_margin_rule_updates_are_rejected(self):
        competition = self.make_competition(default_race_target=4)
        competition, rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )

        with self.assertRaises(ValidationError):
            PoolCompetitionMarginPointRule.objects.filter(
                pk=rules[0].pk
            ).update(maximum_margin=None)

        rules[0].refresh_from_db()
        self.assertEqual(rules[0].maximum_margin, 3)

    def test_active_policy_rejects_a_finite_terminal_rule_becoming_open_ended(self):
        competition = self.make_competition(default_race_target=4)
        competition, rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )
        terminal_rule = rules[-1]
        terminal_rule.maximum_margin = None

        with self.assertRaises(ValidationError):
            terminal_rule.save()

        terminal_rule.refresh_from_db()
        self.assertEqual(terminal_rule.maximum_margin, 4)

    def test_lowering_the_only_high_target_table_fixture_is_blocked(self):
        """Validate the future table scope, not the persisted fixture value."""

        competition = self.make_competition(default_race_target=4)
        home, away = self.make_entries(competition, "Home", "Away")
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home,
            away_entry=away,
            race_target=5,
        )
        competition, _rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(maximum_margin=5),
        )

        pool_match.race_target = 4
        with self.assertRaises(ValidationError):
            pool_match.save()

        pool_match.refresh_from_db()
        self.assertEqual(pool_match.race_target, 5)

        # A raw queryset update has no prospective state for model validation,
        # so it must fail closed rather than bypassing this same guard.
        with self.assertRaises(ValidationError):
            PoolMatch.objects.filter(pk=pool_match.pk).update(race_target=4)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.race_target, 5)

    def test_removing_only_high_target_table_fixture_is_blocked_for_bulk_and_instance_deletion(self):
        competition = self.make_competition(default_race_target=4)
        home, away, charlie, delta = self.make_entries(
            competition,
            "Home",
            "Away",
            "Charlie",
            "Delta",
        )
        first = PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home,
            away_entry=away,
            race_target=5,
        )
        second = PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=charlie,
            away_entry=delta,
            race_target=5,
        )
        competition, _rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(maximum_margin=5),
        )

        # Delete both in one request: validating each row separately would
        # incorrectly see the other race-to-5 fixture and let the policy
        # become invalid after the second deletion.
        with self.assertRaises(ValidationError):
            PoolMatch.objects.filter(pk__in=[first.pk, second.pk]).delete()
        self.assertEqual(
            PoolMatch.objects.filter(pk__in=[first.pk, second.pk]).count(),
            2,
        )

        # Django admin's individual-object delete path bypasses QuerySet.delete.
        # Use a separate field with one high-target fixture: deleting either
        # member above alone is correctly safe while the other remains.
        individual_competition = self.make_competition(default_race_target=4)
        individual_home, individual_away = self.make_entries(
            individual_competition,
            "Individual Home",
            "Individual Away",
        )
        individual = PoolMatch.objects.create(
            competition=individual_competition,
            stage=self.league_stage(individual_competition),
            home_entry=individual_home,
            away_entry=individual_away,
            race_target=5,
        )
        individual_competition, _rules = self.configure_threshold_policy(
            individual_competition,
            self.two_band_thresholds(maximum_margin=5),
        )
        with self.assertRaises(ValidationError):
            individual.delete()
        self.assertTrue(PoolMatch.objects.filter(pk=individual.pk).exists())

    def test_moving_a_high_target_fixture_out_of_table_play_is_blocked(self):
        competition = self.make_competition(
            default_race_target=4,
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        home, away = self.make_entries(competition, "Home", "Away")
        league_stage = self.league_stage(competition)
        knockout_stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        )
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=league_stage,
            home_entry=home,
            away_entry=away,
            race_target=5,
        )
        competition, _rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(maximum_margin=5),
        )

        with self.assertRaises(ValidationError):
            PoolMatch.objects.filter(pk=pool_match.pk).update(
                stage_id=knockout_stage.pk
            )
        pool_match.stage = knockout_stage
        with self.assertRaises(ValidationError):
            pool_match.save()

        pool_match.refresh_from_db()
        self.assertEqual(pool_match.stage_id, league_stage.pk)

    def test_legacy_open_ended_policy_stays_readable_and_is_normalised_on_save(self):
        competition = self.make_competition(default_race_target=4)
        PoolCompetitionMarginPointRule.objects.create(
            competition=competition,
            minimum_margin=1,
            maximum_margin=3,
            winner_points=3,
            loser_points=1,
        )
        PoolCompetitionMarginPointRule.objects.create(
            competition=competition,
            minimum_margin=4,
            maximum_margin=None,
            winner_points=4,
            loser_points=0,
        )
        with self.assertRaises(ValidationError):
            PoolCompetition.objects.filter(pk=competition.pk).update(
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED
            )

        # A new activation may not retain an open terminal rule.
        competition.points_mode = PoolCompetition.PointsMode.MARGIN_BASED
        with self.assertRaises(ValidationError):
            competition.save()
        competition.refresh_from_db()
        self.assertEqual(competition.points_mode, PoolCompetition.PointsMode.FIXED)

        # Simulate a pre-bounded-range saved state without asking the current
        # application manager to reinterpret historic database rows.
        PoolCompetition._base_manager.filter(pk=competition.pk).update(
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED
        )
        competition.refresh_from_db()

        # Historical data remains readable and unrelated competition edits do
        # not force a made-up allocation.  Any policy write below canonicalises
        # the old terminal rule to the actual race target instead.
        competition.description = "Historic policy retained for review."
        competition.save()

        public_response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug])
        )
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "4–4 racks")
        self.assertNotContains(public_response, "4+")
        self.assertNotContains(public_response, "or more")

        legacy_rules = configured_pool_margin_point_rules(competition)
        competition, normalised = replace_pool_standings_points_configuration(
            competition,
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            points_for_win=competition.points_for_win,
            points_for_loss=competition.points_for_loss,
            margin_rules=legacy_rules,
            allow_legacy_open_ended=True,
        )

        self.assertEqual(self.ranges(normalised), [(1, 3, 3, 1), (4, 4, 4, 0)])

    def test_race_to_four_uses_rack_margin_not_the_race_target(self):
        competition = self.make_competition(default_race_target=4)
        competition, _rules = self.configure_threshold_policy(
            competition,
            self.two_band_thresholds(),
        )
        home, away = self.make_entries(competition, "Home", "Away")

        expected = {
            (4, 3): (3, 1),
            (4, 2): (3, 1),
            (4, 1): (3, 1),
            (4, 0): (4, 0),
        }
        for scores, allocation in expected.items():
            with self.subTest(scores=scores):
                pool_match = self.finish_played_match(
                    competition,
                    home,
                    away,
                    home_score=scores[0],
                    away_score=scores[1],
                )
                self.assertEqual(
                    point_allocation_for_pool_match(competition, pool_match),
                    allocation,
                )

    def test_race_to_four_exact_two_band_policy_covers_every_real_score(self):
        """Race to 4 ends at four; no fictional fifth-margin rule is needed."""
        competition = self.make_competition(default_race_target=4)
        thresholds = (
            {"up_to_margin": 2, "winner_points": 3, "loser_points": 1},
            {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
        )
        competition, saved_rules = self.configure_threshold_policy(
            competition,
            thresholds,
        )
        self.assertEqual(self.ranges(saved_rules), [(1, 2, 3, 1), (3, 4, 4, 0)])

        home, away = self.make_entries(competition, "Home", "Away")
        expected = {
            (4, 3): (3, 1),
            (4, 2): (3, 1),
            (4, 1): (4, 0),
            (4, 0): (4, 0),
        }
        for scores, allocation in expected.items():
            with self.subTest(scores=scores):
                pool_match = self.finish_played_match(
                    competition,
                    home,
                    away,
                    home_score=scores[0],
                    away_score=scores[1],
                )
                self.assertEqual(
                    point_allocation_for_pool_match(competition, pool_match),
                    allocation,
                )

    def test_threshold_and_raw_range_validation_remain_server_side(self):
        invalid_thresholds = {
            "first threshold below one": (
                {"up_to_margin": 0, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
            "duplicate thresholds": (
                {"up_to_margin": 2, "winner_points": 2, "loser_points": 1},
                {"up_to_margin": 2, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
            "decreasing thresholds": (
                {"up_to_margin": 3, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 2, "winner_points": 2, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
            "negative points": (
                {"up_to_margin": 3, "winner_points": -1, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
            "missing allocation": (
                {"up_to_margin": 3, "winner_points": "", "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
        }
        for description, thresholds in invalid_thresholds.items():
            with self.subTest(description=description):
                with self.assertRaises(PoolStandingsValidationError):
                    derive_pool_margin_point_rules_from_thresholds(
                        self.make_competition(),
                        thresholds,
                    )

        competition = self.make_competition()
        with self.assertRaises(PoolStandingsValidationError):
            replace_pool_standings_points_configuration(
                competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
                points_for_win=3,
                points_for_loss=0,
                # A forged storage-level overlap must still be rejected even
                # though the normal manager UI does not expose these fields.
                margin_rules=((1, 3, 3, 1), (3, 4, 4, 0)),
            )
        with self.assertRaises(PoolStandingsValidationError):
            replace_pool_standings_points_configuration(
                competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
                points_for_win=3,
                points_for_loss=0,
                margin_rules=((1, 3, 3, 1), (4, None, 4, 0)),
            )


class PoolMarginThresholdManagerUXTests(PoolMarginUXTestCase):
    def setUp(self):
        super().setUp()
        self.competition = self.make_competition()
        self.client.force_login(self.owner)

    def test_fixed_mode_does_not_render_margin_cards(self):
        response = self.client.get(
            self.standings_url(
                self.competition,
                points_mode=PoolCompetition.PointsMode.FIXED,
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="pool-margin-points-editor"')
        self.assertNotContains(response, '<article class="pool-margin-rule-card ')

    def test_first_margin_based_draft_shows_two_readable_rule_cards(self):
        response = self.client.get(
            self.standings_url(
                self.competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pool-margin-points-editor"')
        self.assertContains(response, '<article class="pool-margin-rule-card ', count=2)
        self.assertNotContains(response, "or more")
        self.assertContains(response, "maximum table race target of 4")

    def test_add_another_rule_get_shows_three_cards_without_javascript(self):
        competition = self.make_competition(default_race_target=7)
        response = self.client.get(
            self.standings_url(
                competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
                add_margin_rule=1,
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<article class="pool-margin-rule-card ', count=3)
        self.assertContains(response, "+ Add another rule")

    def test_repeated_add_rule_get_keeps_inserting_before_the_final_card(self):
        competition = self.make_competition(default_race_target=7)
        response = self.client.get(
            self.standings_url(
                competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
                margin_rule_count=4,
                add_margin_rule=1,
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<article class="pool-margin-rule-card ', count=4)
        self.assertContains(response, "margin_rule_count=5")

    def test_fixed_mode_save_preserves_a_valid_margin_policy_for_later_reuse(self):
        self.configure_threshold_policy(self.competition, self.two_band_thresholds())
        before = self.ranges(
            PoolCompetitionMarginPointRule.objects.filter(
                competition=self.competition
            ).order_by("minimum_margin", "id")
        )

        response = self.client.post(
            self.standings_url(self.competition),
            {
                "action": "points",
                "points_mode": PoolCompetition.PointsMode.FIXED,
                "points_for_win": 2,
                "points_for_loss": 0,
            },
        )

        self.assertRedirects(response, self.standings_url(self.competition))
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.points_mode, PoolCompetition.PointsMode.FIXED)
        self.assertEqual(
            self.ranges(
                PoolCompetitionMarginPointRule.objects.filter(
                    competition=self.competition
                ).order_by("minimum_margin", "id")
            ),
            before,
        )

    def test_existing_policy_with_more_than_eight_bands_stays_editable(self):
        competition = self.make_competition(default_race_target=9)
        thresholds = tuple(
            {
                "up_to_margin": margin,
                "winner_points": 3,
                "loser_points": 1,
            }
            for margin in range(1, 9)
        ) + (
            {"up_to_margin": 9, "winner_points": 4, "loser_points": 0},
        )
        self.configure_threshold_policy(competition, thresholds)

        response = self.client.get(self.standings_url(competition))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<article class="pool-margin-rule-card ', count=9)

        response = self.client.post(
            self.standings_url(competition),
            self.threshold_post_data(*thresholds),
        )
        self.assertRedirects(response, self.standings_url(competition))
        self.assertEqual(
            PoolCompetitionMarginPointRule.objects.filter(
                competition=competition
            ).count(),
            9,
        )

    def test_threshold_post_saves_derived_ranges_through_the_manager_boundary(self):
        response = self.client.post(
            self.standings_url(self.competition),
            self.threshold_post_data(
                {"up_to_margin": 2, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
        )

        self.assertRedirects(response, self.standings_url(self.competition))
        self.competition.refresh_from_db()
        self.assertEqual(
            self.competition.points_mode,
            PoolCompetition.PointsMode.MARGIN_BASED,
        )
        self.assertEqual(
            self.ranges(
                PoolCompetitionMarginPointRule.objects.filter(
                    competition=self.competition
                ).order_by("minimum_margin", "id")
            ),
            [(1, 2, 3, 1), (3, 4, 4, 0)],
        )

    def test_malformed_threshold_post_is_rejected_without_changing_the_policy(self):
        response = self.client.post(
            self.standings_url(self.competition),
            self.threshold_post_data(
                {"up_to_margin": 0, "winner_points": 3, "loser_points": 1},
                {"up_to_margin": 4, "winner_points": 4, "loser_points": 0},
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.points_mode, PoolCompetition.PointsMode.FIXED)
        self.assertFalse(
            PoolCompetitionMarginPointRule.objects.filter(
                competition=self.competition
            ).exists()
        )

    def test_forged_terminal_threshold_is_rejected(self):
        payload = self.threshold_post_data(
            {"up_to_margin": 2, "winner_points": 3, "loser_points": 1},
            {"up_to_margin": 3, "winner_points": 4, "loser_points": 0},
        )

        response = self.client.post(self.standings_url(self.competition), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The final rule must end")
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.points_mode, PoolCompetition.PointsMode.FIXED)

    def test_target_increase_has_a_safe_fixed_then_reconfigure_workflow(self):
        self.competition, _rules = self.configure_threshold_policy(
            self.competition,
            self.two_band_thresholds(),
        )
        information_url = reverse(
            "pool:manage_competition_information",
            args=[self.competition.slug],
        )
        information_payload = {
            "name": self.competition.name,
            "slug": self.competition.slug,
            "participant_mode": self.competition.participant_mode,
            "visibility": self.competition.visibility,
            "operational_state": self.competition.operational_state,
            "default_race_target": 5,
        }

        blocked = self.client.post(information_url, information_payload)
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "must cover every winning margin from 1 through 5")
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.default_race_target, 4)

        fixed = self.client.post(
            self.standings_url(self.competition),
            {
                "action": "points",
                "points_mode": PoolCompetition.PointsMode.FIXED,
                "points_for_win": 2,
                "points_for_loss": 0,
            },
        )
        self.assertRedirects(fixed, self.standings_url(self.competition))
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.points_mode, PoolCompetition.PointsMode.FIXED)

        changed = self.client.post(information_url, information_payload)
        self.assertRedirects(changed, information_url)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.default_race_target, 5)

        repair = self.client.get(
            self.standings_url(
                self.competition,
                points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            )
        )
        self.assertEqual(repair.status_code, 200)
        self.assertContains(repair, '<article class="pool-margin-rule-card ', count=3)
        self.assertContains(repair, 'value="5"')

    def test_forged_raw_range_fields_are_rejected_by_the_threshold_editor(self):
        payload = self.threshold_post_data(*self.two_band_thresholds())
        # These were the old raw form fields.  They must not be silently
        # honoured alongside the simplified threshold card payload.
        payload["margin_rules-0-minimum_margin"] = 1
        payload["margin_rules-0-maximum_margin"] = 99

        response = self.client.post(self.standings_url(self.competition), payload)

        self.assertEqual(response.status_code, 200)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.points_mode, PoolCompetition.PointsMode.FIXED)
        self.assertFalse(
            PoolCompetitionMarginPointRule.objects.filter(
                competition=self.competition
            ).exists()
        )

    def test_foreign_margin_rule_identifier_cannot_be_posted_into_this_competition(self):
        foreign_competition = self.make_competition(slug="foreign-margin-ux")
        _foreign_competition, foreign_rules = self.configure_threshold_policy(
            foreign_competition,
            self.two_band_thresholds(),
        )
        foreign_rule = foreign_rules[0]
        original = (
            foreign_rule.minimum_margin,
            foreign_rule.maximum_margin,
            foreign_rule.winner_points,
            foreign_rule.loser_points,
        )
        payload = self.threshold_post_data(
            {"up_to_margin": 3, "winner_points": 99, "loser_points": 98},
            {"up_to_margin": 4, "winner_points": 97, "loser_points": 96},
        )
        payload["margin_rules-INITIAL_FORMS"] = 1
        payload["margin_rules-0-id"] = foreign_rule.pk

        response = self.client.post(self.standings_url(self.competition), payload)

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
            PoolCompetitionMarginPointRule.objects.filter(
                competition=self.competition
            ).exists()
        )
