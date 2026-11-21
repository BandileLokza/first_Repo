"""Focused regression tests for competition-specific standings tie-breakers."""

from datetime import timedelta
from unittest.mock import patch

from allauth.socialaccount.models import SocialApp
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.forms.models import inlineformset_factory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.admin import (
    CompetitionTieBreakerInlineForm,
    CompetitionTieBreakerInlineFormSet,
)
from leagues.models import Competition, CompetitionTieBreaker, Group, Stage, Standing
from leagues.services import replace_competition_tie_breakers
from leagues.standings import DEFAULT_TIE_BREAKERS, configured_tie_breakers, rank_standings
from matches.models import Match
from teams.models import Team


Criterion = CompetitionTieBreaker.Criterion
User = get_user_model()


class StandingsRankingTestCase(TestCase):
    """Shared fixtures that keep result-derived and standing totals independent."""

    def setUp(self):
        super().setUp()
        self._sequence = 0
        self._match_sequence = 0

    def make_context(self, names=("Alpha", "Bravo"), *, group=False):
        self._sequence += 1
        competition = Competition.objects.create(
            name=f"Tie-breaker Competition {self._sequence}",
            slug=f"tie-breaker-{self._sequence}",
            season="2026",
            format_type="GROUP_KNOCKOUT" if group else "TABLE",
        )
        stage = Stage.objects.create(
            competition=competition,
            name="Group stage" if group else "League stage",
            stage_type="GROUP" if group else "LEAGUE",
            order=1,
        )
        table_group = Group.objects.create(stage=stage, name="A") if group else None
        teams = [
            Team.objects.create(competition=competition, name=name)
            for name in names
        ]
        if table_group:
            table_group.teams.add(*teams)

        # The table and group signals normally create these records.  Keeping
        # this get_or_create makes the fixtures resilient to unusual setup
        # ordering without changing the facts being ranked.
        for team in teams:
            Standing.objects.get_or_create(
                competition=competition,
                stage=stage,
                group=table_group,
                team=team,
            )

        return competition, stage, table_group, teams

    def configure(self, competition, *criteria):
        competition.tie_breakers.all().delete()
        for position, criterion in enumerate(criteria, start=1):
            CompetitionTieBreaker.objects.create(
                competition=competition,
                criterion=criterion,
                order=position,
            )

    def set_totals(
        self,
        competition,
        stage,
        table_group,
        team,
        *,
        points=0,
        goals_for=0,
        goals_against=0,
        won=0,
        baseline_points=0,
        baseline_goals_for=0,
        baseline_goals_against=0,
        baseline_won=0,
    ):
        standing = Standing.objects.get(
            competition=competition,
            stage=stage,
            group=table_group,
            team=team,
        )
        standing.points = points
        standing.goals_for = goals_for
        standing.goals_against = goals_against
        standing.won = won
        standing.baseline_points = baseline_points
        standing.baseline_goals_for = baseline_goals_for
        standing.baseline_goals_against = baseline_goals_against
        standing.baseline_won = baseline_won
        standing.save(
            update_fields=[
                "points",
                "goals_for",
                "goals_against",
                "won",
                "baseline_points",
                "baseline_goals_for",
                "baseline_goals_against",
                "baseline_won",
            ]
        )

    def finish_match(
        self,
        competition,
        stage,
        table_group,
        home_team,
        away_team,
        home_score,
        away_score,
        *,
        status="FINISHED",
        leg=None,
    ):
        self._match_sequence += 1
        return Match.objects.create(
            competition=competition,
            stage=stage,
            group=table_group,
            home_team=home_team,
            away_team=away_team,
            home_score=home_score,
            away_score=away_score,
            status=status,
            table_updated=status == "FINISHED",
            table_home_score=home_score if status == "FINISHED" else None,
            table_away_score=away_score if status == "FINISHED" else None,
            leg=self._match_sequence if leg is None else leg,
            match_date=timezone.now() + timedelta(minutes=self._match_sequence),
        )

    def ranked_names(self, competition, stage, table_group, rows=None):
        if rows is None:
            rows = Standing.objects.filter(
                competition=competition,
                stage=stage,
                group=table_group,
            ).select_related("team")
        return [standing.team.name for standing in rank_standings(competition, rows)]


class CompetitionTieBreakerModelTests(StandingsRankingTestCase):
    def _create_admin_user(self):
        administrator = User.objects.create_superuser(
            username="tiebreaker-admin",
            email="admin@example.com",
            password="pass",
        )
        self.client.force_login(administrator)

    def _submit_admin_tie_breaker_change(self, competition, rows, *, include_series=True):
        """Submit the Competition change view with complete inline rows.

        Each row is ``(existing_primary_key, position, criterion, delete)``.
        Existing forms must come before new forms, as Django's inline formset
        management fields require.
        """

        data = {
            "name": competition.name,
            "slug": competition.slug,
            "season": competition.season,
            "format_type": competition.format_type,
            "display_order": str(competition.display_order),
            "league_type": competition.series.league_type,
            "stages-TOTAL_FORMS": "0",
            "stages-INITIAL_FORMS": "0",
            "stages-MIN_NUM_FORMS": "0",
            "stages-MAX_NUM_FORMS": "1000",
            "tie_breakers-TOTAL_FORMS": str(len(rows)),
            "tie_breakers-INITIAL_FORMS": str(
                sum(row[0] is not None for row in rows)
            ),
            "tie_breakers-MIN_NUM_FORMS": "1",
            "tie_breakers-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        if include_series:
            data["series"] = str(competition.series_id)
        if competition.predictions_enabled:
            data["predictions_enabled"] = "on"

        for index, (row_id, position, criterion, delete) in enumerate(rows):
            prefix = f"tie_breakers-{index}"
            if row_id is not None:
                data[f"{prefix}-id"] = str(row_id)
            data[f"{prefix}-order"] = str(position)
            data[f"{prefix}-criterion"] = criterion
            if delete:
                data[f"{prefix}-DELETE"] = "on"

        return self.client.post(
            reverse("admin:leagues_competition_change", args=[competition.pk]),
            data,
        )

    def test_admin_change_reports_a_missing_series_as_a_form_error(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        rows = [
            (criterion.pk, criterion.order, criterion.criterion, False)
            for criterion in competition.tie_breakers.order_by("order", "id")
        ]

        response = self._submit_admin_tie_breaker_change(
            competition,
            rows,
            include_series=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("series", response.context["adminform"].form.errors)
        competition.refresh_from_db()
        self.assertIsNotNone(competition.series_id)

    def assert_tie_breaker_configuration(self, competition, expected):
        self.assertEqual(
            list(competition.tie_breakers.values_list("order", "criterion")),
            expected,
        )
        self.assertFalse(competition.tie_breakers.filter(order__gte=32000).exists())

    def test_admin_add_saves_its_inline_configuration_without_duplicates(self):
        self._create_admin_user()

        response = self.client.post(
            reverse("admin:leagues_competition_add"),
            {
                "name": "Admin Tie-breaker Competition",
                "slug": "admin-tie-breaker-competition",
                "season": "2026",
                "format_type": "TABLE",
                "display_order": "100",
                "league_type": "ZONAL",
                "stages-TOTAL_FORMS": "0",
                "stages-INITIAL_FORMS": "0",
                "stages-MIN_NUM_FORMS": "0",
                "stages-MAX_NUM_FORMS": "1000",
                "tie_breakers-TOTAL_FORMS": "3",
                "tie_breakers-INITIAL_FORMS": "0",
                "tie_breakers-MIN_NUM_FORMS": "1",
                "tie_breakers-MAX_NUM_FORMS": "1000",
                "tie_breakers-0-order": "1",
                "tie_breakers-0-criterion": Criterion.POINTS,
                "tie_breakers-1-order": "2",
                "tie_breakers-1-criterion": Criterion.HEAD_TO_HEAD_POINTS,
                "tie_breakers-2-order": "3",
                "tie_breakers-2-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        competition = Competition.objects.get(slug="admin-tie-breaker-competition")
        self.assertEqual(
            list(competition.tie_breakers.values_list("order", "criterion")),
            [
                (1, Criterion.POINTS),
                (2, Criterion.HEAD_TO_HEAD_POINTS),
                (3, Criterion.OVERALL_GOAL_DIFFERENCE),
            ],
        )

    def test_admin_change_replaces_default_with_head_to_head_configuration(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        points, overall_goal_difference, overall_goals_scored = list(
            competition.tie_breakers.order_by("order", "id")
        )

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (points.pk, 1, Criterion.POINTS, False),
                (
                    overall_goal_difference.pk,
                    3,
                    Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
                    False,
                ),
                (
                    overall_goals_scored.pk,
                    4,
                    Criterion.HEAD_TO_HEAD_GOALS_SCORED,
                    False,
                ),
                (None, 2, Criterion.HEAD_TO_HEAD_POINTS, False),
                (None, 5, Criterion.HEAD_TO_HEAD_AWAY_GOALS, False),
            ],
        )

        self.assertEqual(response.status_code, 302)
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.HEAD_TO_HEAD_POINTS),
                (3, Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE),
                (4, Criterion.HEAD_TO_HEAD_GOALS_SCORED),
                (5, Criterion.HEAD_TO_HEAD_AWAY_GOALS),
            ],
        )

    def test_admin_change_rebuilds_configuration_with_leaked_positions(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        competition.tie_breakers.all().delete()
        points = CompetitionTieBreaker.objects.create(
            competition=competition,
            order=1,
            criterion=Criterion.POINTS,
        )
        head_to_head_goals_scored = CompetitionTieBreaker.objects.create(
            competition=competition,
            order=4,
            criterion=Criterion.HEAD_TO_HEAD_GOALS_SCORED,
        )
        overall_goal_difference = CompetitionTieBreaker.objects.create(
            competition=competition,
            order=32764,
            criterion=Criterion.OVERALL_GOAL_DIFFERENCE,
        )
        head_to_head_goal_difference = CompetitionTieBreaker.objects.create(
            competition=competition,
            order=32766,
            criterion=Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
        )
        head_to_head_points = CompetitionTieBreaker.objects.create(
            competition=competition,
            order=32767,
            criterion=Criterion.HEAD_TO_HEAD_POINTS,
        )

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (points.pk, 1, Criterion.POINTS, False),
                (
                    head_to_head_goals_scored.pk,
                    4,
                    Criterion.HEAD_TO_HEAD_GOALS_SCORED,
                    False,
                ),
                (
                    overall_goal_difference.pk,
                    3,
                    Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
                    False,
                ),
                (
                    head_to_head_goal_difference.pk,
                    2,
                    Criterion.HEAD_TO_HEAD_POINTS,
                    False,
                ),
                (
                    head_to_head_points.pk,
                    5,
                    Criterion.HEAD_TO_HEAD_AWAY_GOALS,
                    False,
                ),
            ],
        )

        self.assertEqual(response.status_code, 302)
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.HEAD_TO_HEAD_POINTS),
                (3, Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE),
                (4, Criterion.HEAD_TO_HEAD_GOALS_SCORED),
                (5, Criterion.HEAD_TO_HEAD_AWAY_GOALS),
            ],
        )

    def test_admin_change_can_swap_existing_criteria(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        points, overall_goal_difference, overall_goals_scored = list(
            competition.tie_breakers.order_by("order", "id")
        )

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (points.pk, 1, Criterion.POINTS, False),
                (
                    overall_goal_difference.pk,
                    2,
                    Criterion.OVERALL_GOALS_SCORED,
                    False,
                ),
                (
                    overall_goals_scored.pk,
                    3,
                    Criterion.OVERALL_GOAL_DIFFERENCE,
                    False,
                ),
            ],
        )

        self.assertEqual(response.status_code, 302)
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.OVERALL_GOALS_SCORED),
                (3, Criterion.OVERALL_GOAL_DIFFERENCE),
            ],
        )

    def test_admin_change_can_replace_multiple_existing_criteria(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        self.configure(
            competition,
            Criterion.POINTS,
            Criterion.OVERALL_GOAL_DIFFERENCE,
            Criterion.OVERALL_GOALS_SCORED,
            Criterion.WINS,
            Criterion.FAIR_PLAY,
        )
        rows = list(competition.tie_breakers.order_by("order", "id"))

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (rows[0].pk, 1, Criterion.POINTS, False),
                (rows[1].pk, 2, Criterion.HEAD_TO_HEAD_POINTS, False),
                (
                    rows[2].pk,
                    3,
                    Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
                    False,
                ),
                (
                    rows[3].pk,
                    4,
                    Criterion.HEAD_TO_HEAD_GOALS_SCORED,
                    False,
                ),
                (
                    rows[4].pk,
                    5,
                    Criterion.HEAD_TO_HEAD_AWAY_GOALS,
                    False,
                ),
            ],
        )

        self.assertEqual(response.status_code, 302)
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.HEAD_TO_HEAD_POINTS),
                (3, Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE),
                (4, Criterion.HEAD_TO_HEAD_GOALS_SCORED),
                (5, Criterion.HEAD_TO_HEAD_AWAY_GOALS),
            ],
        )

    def test_admin_change_can_delete_and_insert_criteria_together(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        points, overall_goal_difference, overall_goals_scored = list(
            competition.tie_breakers.order_by("order", "id")
        )

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (points.pk, 1, Criterion.POINTS, False),
                (
                    overall_goal_difference.pk,
                    2,
                    Criterion.OVERALL_GOAL_DIFFERENCE,
                    False,
                ),
                (
                    overall_goals_scored.pk,
                    3,
                    Criterion.OVERALL_GOALS_SCORED,
                    True,
                ),
                (None, 3, Criterion.HEAD_TO_HEAD_POINTS, False),
            ],
        )

        self.assertEqual(response.status_code, 302)
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.OVERALL_GOAL_DIFFERENCE),
                (3, Criterion.HEAD_TO_HEAD_POINTS),
            ],
        )

    def test_admin_change_rejects_duplicate_criterion_without_saving(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        rows = list(competition.tie_breakers.order_by("order", "id"))

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (rows[0].pk, 1, Criterion.POINTS, False),
                (rows[1].pk, 2, Criterion.OVERALL_GOAL_DIFFERENCE, False),
                (rows[2].pk, 3, Criterion.OVERALL_GOAL_DIFFERENCE, False),
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Please correct the duplicate data for criterion.",
        )
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.OVERALL_GOAL_DIFFERENCE),
                (3, Criterion.OVERALL_GOALS_SCORED),
            ],
        )

    def test_admin_change_rejects_duplicate_position_without_saving(self):
        self._create_admin_user()
        competition, _, _, _ = self.make_context()
        rows = list(competition.tie_breakers.order_by("order", "id"))

        response = self._submit_admin_tie_breaker_change(
            competition,
            [
                (rows[0].pk, 1, Criterion.POINTS, False),
                (rows[1].pk, 2, Criterion.OVERALL_GOAL_DIFFERENCE, False),
                (rows[2].pk, 2, Criterion.OVERALL_GOALS_SCORED, False),
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Please correct the duplicate data for order.",
        )
        self.assert_tie_breaker_configuration(
            competition,
            [
                (1, Criterion.POINTS),
                (2, Criterion.OVERALL_GOAL_DIFFERENCE),
                (3, Criterion.OVERALL_GOALS_SCORED),
            ],
        )

    def test_failed_atomic_rebuild_restores_the_existing_configuration(self):
        competition, _, _, _ = self.make_context()
        original_configuration = list(
            competition.tie_breakers.values_list("order", "criterion")
        )

        with patch.object(
            CompetitionTieBreaker.objects,
            "bulk_create",
            side_effect=IntegrityError("forced bulk-create failure"),
        ):
            with self.assertRaises(IntegrityError):
                replace_competition_tie_breakers(
                    competition,
                    [
                        (1, Criterion.POINTS),
                        (2, Criterion.HEAD_TO_HEAD_POINTS),
                        (3, Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE),
                    ],
                )

        self.assert_tie_breaker_configuration(
            competition,
            original_configuration,
        )

    def test_points_is_first_and_criteria_and_positions_are_unique(self):
        competition, _, _, _ = self.make_context()
        self.assertEqual(
            list(
                competition.tie_breakers.values_list("order", "criterion")
            ),
            [
                (1, Criterion.POINTS),
                (2, Criterion.OVERALL_GOAL_DIFFERENCE),
                (3, Criterion.OVERALL_GOALS_SCORED),
            ],
        )
        competition.tie_breakers.all().delete()

        with self.assertRaises(ValidationError):
            CompetitionTieBreaker(
                competition=competition,
                criterion=Criterion.POINTS,
                order=2,
            ).full_clean()

        with self.assertRaises(ValidationError):
            CompetitionTieBreaker(
                competition=competition,
                criterion=Criterion.OVERALL_GOAL_DIFFERENCE,
                order=1,
            ).full_clean()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompetitionTieBreaker.objects.create(
                    competition=competition,
                    criterion=Criterion.WINS,
                    order=1,
                )

        CompetitionTieBreaker.objects.create(
            competition=competition,
            criterion=Criterion.POINTS,
            order=1,
        )
        CompetitionTieBreaker.objects.create(
            competition=competition,
            criterion=Criterion.OVERALL_GOAL_DIFFERENCE,
            order=2,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompetitionTieBreaker.objects.create(
                    competition=competition,
                    criterion=Criterion.OVERALL_GOAL_DIFFERENCE,
                    order=3,
                )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompetitionTieBreaker.objects.create(
                    competition=competition,
                    criterion=Criterion.OVERALL_GOALS_SCORED,
                    order=2,
                )

    def test_admin_inline_rejects_a_configuration_without_points(self):
        competition, _, _, _ = self.make_context()
        competition.tie_breakers.all().delete()
        formset_class = inlineformset_factory(
            Competition,
            CompetitionTieBreaker,
            form=CompetitionTieBreakerInlineForm,
            formset=CompetitionTieBreakerInlineFormSet,
            fields=("order", "criterion"),
            extra=0,
        )
        prefix = formset_class.get_default_prefix()
        formset = formset_class(
            data={
                f"{prefix}-TOTAL_FORMS": "2",
                f"{prefix}-INITIAL_FORMS": "0",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
                f"{prefix}-0-order": "2",
                f"{prefix}-0-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
                f"{prefix}-1-order": "3",
                f"{prefix}-1-criterion": Criterion.OVERALL_GOALS_SCORED,
            },
            instance=competition,
            prefix=prefix,
        )

        self.assertFalse(formset.is_valid())
        self.assertIn("Points", str(formset.non_form_errors()))

    def test_admin_inline_rejects_duplicate_criteria_and_positions(self):
        competition, _, _, _ = self.make_context()
        competition.tie_breakers.all().delete()
        formset_class = inlineformset_factory(
            Competition,
            CompetitionTieBreaker,
            form=CompetitionTieBreakerInlineForm,
            formset=CompetitionTieBreakerInlineFormSet,
            fields=("order", "criterion"),
            extra=0,
        )
        prefix = formset_class.get_default_prefix()

        duplicate_criterion = formset_class(
            data={
                f"{prefix}-TOTAL_FORMS": "3",
                f"{prefix}-INITIAL_FORMS": "0",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
                f"{prefix}-0-order": "1",
                f"{prefix}-0-criterion": Criterion.POINTS,
                f"{prefix}-1-order": "2",
                f"{prefix}-1-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
                f"{prefix}-2-order": "3",
                f"{prefix}-2-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
            },
            instance=competition,
            prefix=prefix,
        )
        duplicate_position = formset_class(
            data={
                f"{prefix}-TOTAL_FORMS": "3",
                f"{prefix}-INITIAL_FORMS": "0",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
                f"{prefix}-0-order": "1",
                f"{prefix}-0-criterion": Criterion.POINTS,
                f"{prefix}-1-order": "2",
                f"{prefix}-1-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
                f"{prefix}-2-order": "2",
                f"{prefix}-2-criterion": Criterion.OVERALL_GOALS_SCORED,
            },
            instance=competition,
            prefix=prefix,
        )

        self.assertFalse(duplicate_criterion.is_valid())
        self.assertFalse(duplicate_position.is_valid())

    def test_admin_inline_can_swap_existing_non_points_positions(self):
        competition, _, _, _ = self.make_context()
        rows = list(competition.tie_breakers.order_by("order", "id"))
        formset_class = inlineformset_factory(
            Competition,
            CompetitionTieBreaker,
            form=CompetitionTieBreakerInlineForm,
            formset=CompetitionTieBreakerInlineFormSet,
            fields=("order", "criterion"),
            extra=0,
        )
        prefix = formset_class.get_default_prefix()
        formset = formset_class(
            data={
                f"{prefix}-TOTAL_FORMS": "3",
                f"{prefix}-INITIAL_FORMS": "3",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
                f"{prefix}-0-id": str(rows[0].pk),
                f"{prefix}-0-order": "1",
                f"{prefix}-0-criterion": Criterion.POINTS,
                f"{prefix}-1-id": str(rows[1].pk),
                f"{prefix}-1-order": "3",
                f"{prefix}-1-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
                f"{prefix}-2-id": str(rows[2].pk),
                f"{prefix}-2-order": "2",
                f"{prefix}-2-criterion": Criterion.OVERALL_GOALS_SCORED,
            },
            instance=competition,
            prefix=prefix,
        )

        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()

        self.assertEqual(
            list(competition.tie_breakers.values_list("order", "criterion")),
            [
                (1, Criterion.POINTS),
                (2, Criterion.OVERALL_GOALS_SCORED),
                (3, Criterion.OVERALL_GOAL_DIFFERENCE),
            ],
        )

    def test_admin_inline_can_insert_a_criterion_between_existing_positions(self):
        competition, _, _, _ = self.make_context()
        rows = list(competition.tie_breakers.order_by("order", "id"))
        formset_class = inlineformset_factory(
            Competition,
            CompetitionTieBreaker,
            form=CompetitionTieBreakerInlineForm,
            formset=CompetitionTieBreakerInlineFormSet,
            fields=("order", "criterion"),
            extra=0,
        )
        prefix = formset_class.get_default_prefix()
        formset = formset_class(
            data={
                f"{prefix}-TOTAL_FORMS": "4",
                f"{prefix}-INITIAL_FORMS": "3",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
                f"{prefix}-0-id": str(rows[0].pk),
                f"{prefix}-0-order": "1",
                f"{prefix}-0-criterion": Criterion.POINTS,
                f"{prefix}-1-id": str(rows[1].pk),
                f"{prefix}-1-order": "3",
                f"{prefix}-1-criterion": Criterion.OVERALL_GOAL_DIFFERENCE,
                f"{prefix}-2-id": str(rows[2].pk),
                f"{prefix}-2-order": "4",
                f"{prefix}-2-criterion": Criterion.OVERALL_GOALS_SCORED,
                f"{prefix}-3-order": "2",
                f"{prefix}-3-criterion": Criterion.HEAD_TO_HEAD_POINTS,
            },
            instance=competition,
            prefix=prefix,
        )

        self.assertTrue(formset.is_valid(), formset.errors)
        formset.save()

        self.assertEqual(
            list(competition.tie_breakers.values_list("order", "criterion")),
            [
                (1, Criterion.POINTS),
                (2, Criterion.HEAD_TO_HEAD_POINTS),
                (3, Criterion.OVERALL_GOAL_DIFFERENCE),
                (4, Criterion.OVERALL_GOALS_SCORED),
            ],
        )


class StandingsRankingServiceTests(StandingsRankingTestCase):
    def test_default_configuration_preserves_default_order_and_uses_baselines(self):
        competition, stage, table_group, (alpha, bravo, charlie) = self.make_context(
            ("Alpha", "Bravo", "Charlie")
        )
        self.set_totals(
            competition,
            stage,
            table_group,
            alpha,
            points=5,
            goals_for=2,
            goals_against=1,
            baseline_points=5,
            baseline_goals_for=8,
            baseline_goals_against=1,
        )
        self.set_totals(
            competition,
            stage,
            table_group,
            bravo,
            points=10,
            goals_for=4,
            goals_against=3,
        )
        self.set_totals(
            competition,
            stage,
            table_group,
            charlie,
            points=9,
            goals_for=12,
            goals_against=1,
        )

        self.assertEqual(configured_tie_breakers(competition), DEFAULT_TIE_BREAKERS)
        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo", "Charlie"],
        )

        # The runtime fallback retains the same behavior for a legacy/imported
        # competition whose configuration was not persisted yet.
        competition.tie_breakers.all().delete()
        self.assertEqual(configured_tie_breakers(competition), DEFAULT_TIE_BREAKERS)
        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo", "Charlie"],
        )

    def test_competitions_can_use_different_configured_orders(self):
        first, first_stage, first_group, (alpha, bravo) = self.make_context()
        self.configure(
            first,
            Criterion.POINTS,
            Criterion.OVERALL_GOAL_DIFFERENCE,
        )
        self.set_totals(
            first,
            first_stage,
            first_group,
            alpha,
            points=12,
            goals_for=8,
            goals_against=2,
            won=3,
        )
        self.set_totals(
            first,
            first_stage,
            first_group,
            bravo,
            points=12,
            goals_for=6,
            goals_against=3,
            won=5,
        )

        second, second_stage, second_group, (charlie, delta) = self.make_context(
            ("Charlie", "Delta")
        )
        self.configure(second, Criterion.POINTS, Criterion.WINS)
        self.set_totals(
            second,
            second_stage,
            second_group,
            charlie,
            points=12,
            goals_for=8,
            goals_against=2,
            won=3,
        )
        self.set_totals(
            second,
            second_stage,
            second_group,
            delta,
            points=12,
            goals_for=6,
            goals_against=3,
            won=5,
        )

        self.assertEqual(
            self.ranked_names(first, first_stage, first_group),
            ["Alpha", "Bravo"],
        )
        self.assertEqual(
            self.ranked_names(second, second_stage, second_group),
            ["Delta", "Charlie"],
        )

    def test_two_team_points_overall_and_wins_criteria(self):
        cases = (
            (
                Criterion.OVERALL_GOAL_DIFFERENCE,
                {"points": 11, "goals_for": 8, "goals_against": 2},
                {"points": 11, "goals_for": 5, "goals_against": 2},
            ),
            (
                Criterion.OVERALL_GOALS_SCORED,
                {"points": 11, "goals_for": 8, "goals_against": 4},
                {"points": 11, "goals_for": 6, "goals_against": 2},
            ),
            (
                Criterion.WINS,
                {"points": 11, "goals_for": 8, "goals_against": 4, "won": 3},
                {"points": 11, "goals_for": 8, "goals_against": 4, "won": 4},
            ),
        )

        for criterion, alpha_totals, bravo_totals in cases:
            with self.subTest(criterion=criterion):
                competition, stage, table_group, (alpha, bravo) = self.make_context()
                self.configure(competition, Criterion.POINTS, criterion)
                self.set_totals(competition, stage, table_group, alpha, **alpha_totals)
                self.set_totals(competition, stage, table_group, bravo, **bravo_totals)
                expected = "Bravo" if criterion == Criterion.WINS else "Alpha"
                self.assertEqual(
                    self.ranked_names(competition, stage, table_group),
                    [expected, "Alpha" if expected == "Bravo" else "Bravo"],
                )

    def test_two_team_head_to_head_points_goal_difference_and_goals_scored(self):
        cases = (
            (
                Criterion.HEAD_TO_HEAD_POINTS,
                (("alpha", "bravo", 1, 0),),
            ),
            (
                Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
                (("alpha", "bravo", 3, 0), ("bravo", "alpha", 2, 0)),
            ),
            (
                Criterion.HEAD_TO_HEAD_GOALS_SCORED,
                (("alpha", "bravo", 4, 1),),
            ),
        )

        for criterion, results in cases:
            with self.subTest(criterion=criterion):
                competition, stage, table_group, (alpha, bravo) = self.make_context()
                self.configure(competition, Criterion.POINTS, criterion)
                self.set_totals(competition, stage, table_group, alpha, points=10)
                self.set_totals(competition, stage, table_group, bravo, points=10)
                teams = {"alpha": alpha, "bravo": bravo}
                for home, away, home_score, away_score in results:
                    self.finish_match(
                        competition,
                        stage,
                        table_group,
                        teams[home],
                        teams[away],
                        home_score,
                        away_score,
                    )
                self.assertEqual(
                    self.ranked_names(competition, stage, table_group),
                    ["Alpha", "Bravo"],
                )

    def test_two_team_head_to_head_away_goals(self):
        competition, stage, table_group, (alpha, bravo) = self.make_context()
        self.configure(competition, Criterion.POINTS, Criterion.HEAD_TO_HEAD_AWAY_GOALS)
        self.set_totals(competition, stage, table_group, alpha, points=10)
        self.set_totals(competition, stage, table_group, bravo, points=10)
        self.finish_match(competition, stage, table_group, alpha, bravo, 1, 0)
        self.finish_match(competition, stage, table_group, bravo, alpha, 2, 1)

        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo"],
        )

    def test_incomplete_head_to_head_safely_falls_through_to_the_next_rule(self):
        competition, stage, table_group, (alpha, bravo) = self.make_context()
        self.configure(
            competition,
            Criterion.POINTS,
            Criterion.HEAD_TO_HEAD_POINTS,
            Criterion.OVERALL_GOAL_DIFFERENCE,
        )
        self.set_totals(
            competition,
            stage,
            table_group,
            alpha,
            points=10,
            goals_for=4,
            goals_against=1,
        )
        self.set_totals(
            competition,
            stage,
            table_group,
            bravo,
            points=10,
            goals_for=2,
            goals_against=1,
        )

        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo"],
        )

    def test_three_team_mini_table_uses_all_completed_pairwise_results(self):
        competition, stage, table_group, (alpha, bravo, charlie) = self.make_context(
            ("Alpha", "Bravo", "Charlie")
        )
        self.configure(competition, Criterion.POINTS, Criterion.HEAD_TO_HEAD_POINTS)
        for team in (alpha, bravo, charlie):
            self.set_totals(competition, stage, table_group, team, points=10)

        self.finish_match(competition, stage, table_group, alpha, bravo, 1, 0)
        self.finish_match(competition, stage, table_group, alpha, charlie, 1, 0)
        self.finish_match(competition, stage, table_group, bravo, charlie, 1, 0)

        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo", "Charlie"],
        )

    def test_four_team_mini_table_applies_head_to_head_goal_difference(self):
        competition, stage, table_group, (alpha, bravo, charlie, delta) = self.make_context(
            ("Alpha", "Bravo", "Charlie", "Delta")
        )
        self.configure(
            competition,
            Criterion.POINTS,
            Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
        )
        for team in (alpha, bravo, charlie, delta):
            self.set_totals(competition, stage, table_group, team, points=10)

        self.finish_match(competition, stage, table_group, alpha, bravo, 1, 0)
        self.finish_match(competition, stage, table_group, alpha, charlie, 1, 0)
        self.finish_match(competition, stage, table_group, delta, alpha, 1, 0)
        self.finish_match(competition, stage, table_group, bravo, charlie, 2, 0)
        self.finish_match(competition, stage, table_group, bravo, delta, 1, 0)
        self.finish_match(competition, stage, table_group, charlie, delta, 3, 0)

        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Bravo", "Alpha", "Charlie", "Delta"],
        )

    def test_multi_team_head_to_head_reapplies_to_a_remaining_subgroup(self):
        competition, stage, table_group, (charlie, bravo, alpha) = self.make_context(
            ("Charlie", "Bravo", "Alpha")
        )
        self.configure(
            competition,
            Criterion.POINTS,
            Criterion.HEAD_TO_HEAD_POINTS,
            Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
        )
        for team in (alpha, bravo, charlie):
            self.set_totals(competition, stage, table_group, team, points=10)

        # A is clear on the three-team mini-table.  B and C both have three
        # H2H points there, then B wins their reduced two-team mini-table 4-1.
        self.finish_match(competition, stage, table_group, alpha, bravo, 7, 0)
        self.finish_match(competition, stage, table_group, alpha, charlie, 1, 0)
        self.finish_match(competition, stage, table_group, bravo, charlie, 4, 0)
        self.finish_match(competition, stage, table_group, charlie, bravo, 1, 0)

        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo", "Charlie"],
        )

    def test_head_to_head_uses_only_finished_matches_in_the_exact_scope(self):
        competition, stage, table_group, (alpha, bravo) = self.make_context()
        self.configure(competition, Criterion.POINTS, Criterion.HEAD_TO_HEAD_POINTS)
        self.set_totals(competition, stage, table_group, alpha, points=10)
        self.set_totals(competition, stage, table_group, bravo, points=10)
        self.finish_match(competition, stage, table_group, alpha, bravo, 1, 0)

        other_stage = Stage.objects.create(
            competition=competition,
            name="Other league stage",
            stage_type="LEAGUE",
            order=2,
        )
        self.finish_match(competition, other_stage, None, bravo, alpha, 9, 0)

        other_group = Group.objects.create(stage=stage, name="Elsewhere")
        self.finish_match(competition, stage, other_group, bravo, alpha, 9, 0)

        self.finish_match(
            competition,
            stage,
            table_group,
            bravo,
            alpha,
            9,
            0,
            status="UPCOMING",
        )

        other_competition = Competition.objects.create(
            name="Other Competition",
            slug="other-tie-breaker-competition",
            season="2025",
            format_type="TABLE",
        )
        foreign_stage = Stage.objects.create(
            competition=other_competition,
            name="Foreign stage",
            stage_type="LEAGUE",
            order=1,
        )
        # Match.save intentionally does not call full_clean(), allowing this
        # historic malformed record to prove ranking never crosses competition
        # boundaries even when team IDs happen to match.
        self.finish_match(other_competition, foreign_stage, None, bravo, alpha, 9, 0)

        self.assertEqual(
            self.ranked_names(competition, stage, table_group),
            ["Alpha", "Bravo"],
        )

    def test_fair_play_and_drawing_of_lots_are_recognized_but_never_random(self):
        competition, stage, table_group, (zulu, alpha) = self.make_context(
            ("Zulu", "Alpha")
        )
        self.configure(
            competition,
            Criterion.POINTS,
            Criterion.FAIR_PLAY,
            Criterion.DRAWING_OF_LOTS,
        )
        self.set_totals(competition, stage, table_group, zulu, points=10)
        self.set_totals(competition, stage, table_group, alpha, points=10)
        rows = list(
            Standing.objects.filter(
                competition=competition,
                stage=stage,
                group=table_group,
            ).select_related("team")
        )

        self.assertEqual(
            self.ranked_names(competition, stage, table_group, list(reversed(rows))),
            ["Alpha", "Zulu"],
        )
        self.assertEqual(
            self.ranked_names(competition, stage, table_group, list(reversed(rows))),
            ["Alpha", "Zulu"],
        )


class PublicStandingsTieBreakerIntegrationTests(StandingsRankingTestCase):
    def setUp(self):
        super().setUp()
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-client",
            secret="test-secret",
        )
        social_app.sites.add(Site.objects.get_current())

    def test_public_standings_view_uses_the_competition_configuration(self):
        competition, stage, table_group, (alpha, bravo) = self.make_context()
        self.configure(competition, Criterion.POINTS, Criterion.WINS)
        self.set_totals(competition, stage, table_group, alpha, points=12, won=3)
        self.set_totals(competition, stage, table_group, bravo, points=12, won=4)

        response = self.client.get(reverse("league_standings", args=[competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [standing.team.name for standing in response.context["standings"]],
            ["Bravo", "Alpha"],
        )
