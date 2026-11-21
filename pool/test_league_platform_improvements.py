"""Regression coverage for imported pool tables and league scheduling."""

from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from pool.baselines import PoolBaselineValidationError, save_pool_standing_baselines
from pool.league_fixtures import (
    LeagueFixtureValidationError,
    confirm_fixture_preview,
    get_league_fixture_scope,
    league_fixture_status,
    plan_first_round_preview,
    plan_second_round_preview,
    serialize_fixture_preview,
)
from pool.models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupEntry,
    PoolMatch,
    PoolPlayer,
    PoolStandingBaseline,
    PoolTeam,
)
from pool.services import record_non_played_result, record_rack, start_match
from pool.standings import standings_for_stage


User = get_user_model()


class PoolLeaguePlatformTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("league-owner", password="test-password")
        self.superuser = User.objects.create_superuser(
            "league-superuser", "superuser@example.test", "test-password"
        )
        self.counter = 0

    def competition(self, *, mode=PoolCompetition.ParticipantMode.SINGLES, **overrides):
        self.counter += 1
        values = {
            "name": f"League {self.counter}",
            "slug": f"league-{self.counter}",
            "owner": self.owner,
            "participant_mode": mode,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "default_race_target": 1,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    def stage(self, competition):
        return competition.stages.get(stage_type=PoolCompetitionStage.StageType.LEAGUE)

    def singles_entries(self, competition, count):
        entries = []
        for number in range(count):
            player = PoolPlayer.objects.create(full_name=f"Player {self.counter}-{number}")
            entries.append(PoolCompetitionEntry.objects.create(competition=competition, player=player))
        return entries

    def match(self, competition, stage, home, away):
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            race_target=1,
        )


class PoolBaselineStandingsTests(PoolLeaguePlatformTestCase):
    def test_missing_baselines_leave_existing_table_at_zero(self):
        competition = self.competition()
        stage = self.stage(competition)
        entries = self.singles_entries(competition, 2)

        rows = {row.entry_id: row for row in standings_for_stage(stage)}

        self.assertEqual(rows[entries[0].pk].played, 0)
        self.assertEqual(rows[entries[1].pk].points, 0)

    def test_baselines_are_added_to_recorded_results_without_h2h_history(self):
        competition = self.competition()
        stage = self.stage(competition)
        home, away = self.singles_entries(competition, 2)
        save_pool_standing_baselines(
            stage,
            [
                {"entry": home, "played": 8, "won": 7, "lost": 1, "racks_for": 101, "racks_against": 64, "points": 14},
                {"entry": away, "played": 8, "won": 3, "lost": 5, "racks_for": 72, "racks_against": 89, "points": 6},
            ],
            actor=self.superuser,
        )
        fixture = self.match(competition, stage, home, away)
        start_match(fixture, recorded_by=self.owner)
        record_rack(fixture, winner=home, recorded_by=self.owner)

        rows = {row.entry_id: row for row in standings_for_stage(stage)}
        self.assertEqual((rows[home.pk].played, rows[home.pk].won), (9, 8))
        self.assertEqual((rows[home.pk].racks_for, rows[home.pk].racks_against), (102, 64))
        self.assertEqual(rows[home.pk].points, 14 + competition.points_for_win)
        self.assertEqual(rows[away.pk].racks_against, 90)

    def test_baseline_scope_and_validation_are_strict(self):
        first = self.competition()
        second = self.competition()
        first_stage, second_stage = self.stage(first), self.stage(second)
        first_entry = self.singles_entries(first, 1)[0]
        self.singles_entries(second, 1)

        with self.assertRaises(PoolBaselineValidationError):
            save_pool_standing_baselines(
                first_stage,
                [{"entry": first_entry, "played": 1, "won": 1, "lost": 1}],
            )
        self.assertFalse(PoolStandingBaseline.objects.exists())
        save_pool_standing_baselines(
            first_stage,
            [{"entry": first_entry, "played": 2, "won": 1, "lost": 1}],
        )
        self.assertEqual(standings_for_stage(second_stage)[0].played, 0)

    def test_baseline_manager_is_superuser_only_and_saves_bulk_rows(self):
        competition = self.competition()
        stage = self.stage(competition)
        entries = self.singles_entries(competition, 2)
        url = reverse("pool:manage_competition_base_standings", args=[competition.slug])

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.superuser)
        response = self.client.post(
            url,
            {
                "stage": stage.pk,
                "baseline-TOTAL_FORMS": 2,
                "baseline-INITIAL_FORMS": 2,
                "baseline-MIN_NUM_FORMS": 0,
                "baseline-MAX_NUM_FORMS": 512,
                "baseline-0-entry": entries[0].pk,
                "baseline-0-played": 3,
                "baseline-0-won": 2,
                "baseline-0-lost": 1,
                "baseline-0-racks_for": 20,
                "baseline-0-racks_against": 10,
                "baseline-0-points": 4,
                "baseline-1-entry": entries[1].pk,
                "baseline-1-played": 0,
                "baseline-1-won": 0,
                "baseline-1-lost": 0,
                "baseline-1-racks_for": 0,
                "baseline-1-racks_against": 0,
                "baseline-1-points": 0,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PoolStandingBaseline.objects.filter(stage=stage).count(), 1)


class PoolLeagueFixtureGeneratorTests(PoolLeaguePlatformTestCase):
    @staticmethod
    def stable_order(values):
        return values

    def group_with_entries(self, competition, stage, name, order, entrant_count):
        group = PoolGroup.objects.create(stage=stage, name=name, order=order)
        entries = self.singles_entries(competition, entrant_count)
        for sort_order, entry in enumerate(entries):
            PoolGroupEntry.objects.create(
                stage=stage,
                group=group,
                entry=entry,
                sort_order=sort_order,
            )
        return group, entries

    def generate_group_round_robin(self, competition, stage, name, order, entrant_count):
        """Create and confirm one independent group-table round robin."""

        group, _entries = self.group_with_entries(
            competition, stage, name, order, entrant_count
        )
        scope = get_league_fixture_scope(competition, stage, group)
        preview = plan_first_round_preview(scope, randomizer=self.stable_order)
        return group, confirm_fixture_preview(
            serialize_fixture_preview(preview), actor=self.owner, scope=scope
        )

    def test_four_entries_preview_is_complete_and_creates_nothing(self):
        competition = self.competition()
        scope = get_league_fixture_scope(competition, self.stage(competition))
        entries = self.singles_entries(competition, 4)

        preview = plan_first_round_preview(scope, randomizer=self.stable_order)
        self.assertEqual((preview.fixture_count, preview.gameweek_count), (6, 3))
        self.assertFalse(PoolMatch.objects.filter(competition=competition).exists())
        self.assertEqual(
            {tuple(sorted((item.home_entry_id, item.away_entry_id))) for item in preview.fixtures},
            {tuple(sorted((left.pk, right.pk))) for index, left in enumerate(entries) for right in entries[index + 1:]},
        )
        for week in preview.gameweeks:
            entrant_ids = [entry_id for fixture in week.fixtures for entry_id in (fixture.home_entry_id, fixture.away_entry_id)]
            self.assertEqual(len(entrant_ids), len(set(entrant_ids)))

    def test_odd_entry_byes_and_separate_reverse_round_are_safe(self):
        competition = self.competition()
        stage = self.stage(competition)
        self.singles_entries(competition, 5)
        scope = get_league_fixture_scope(competition, stage)
        first = plan_first_round_preview(scope, randomizer=self.stable_order)
        self.assertEqual((first.fixture_count, first.gameweek_count), (10, 5))
        confirm_fixture_preview(serialize_fixture_preview(first), actor=self.owner, scope=scope)
        self.assertTrue(league_fixture_status(scope)["first_round_generated"])
        self.assertFalse(league_fixture_status(scope)["second_round_generated"])

        second = plan_second_round_preview(scope)
        confirm_fixture_preview(serialize_fixture_preview(second), actor=self.owner, scope=scope)
        first_rows = PoolMatch.objects.filter(competition=competition, league_leg=1).order_by("gameweek", "pk")
        second_rows = PoolMatch.objects.filter(competition=competition, league_leg=2).order_by("gameweek", "pk")
        self.assertEqual(second_rows.count(), first_rows.count())
        self.assertEqual({row.gameweek for row in second_rows}, {6, 7, 8, 9, 10})
        self.assertEqual(
            {(row.gameweek - 5, row.home_entry_id, row.away_entry_id) for row in second_rows},
            {(row.gameweek, row.away_entry_id, row.home_entry_id) for row in first_rows},
        )
        with self.assertRaises(LeagueFixtureValidationError):
            confirm_fixture_preview(serialize_fixture_preview(second), actor=self.owner, scope=scope)

    def test_manual_or_imported_table_blocks_auto_generation(self):
        competition = self.competition()
        stage = self.stage(competition)
        home, away = self.singles_entries(competition, 2)
        scope = get_league_fixture_scope(competition, stage)
        self.match(competition, stage, home, away)
        self.assertFalse(league_fixture_status(scope)["can_generate_first_round"])
        with self.assertRaises(LeagueFixtureValidationError):
            plan_first_round_preview(scope)

    def test_team_league_uses_the_same_generator(self):
        competition = self.competition(mode=PoolCompetition.ParticipantMode.TEAMS)
        scope = get_league_fixture_scope(competition, self.stage(competition))
        for number in range(4):
            team = PoolTeam.objects.create(name=f"Team {number}")
            PoolCompetitionEntry.objects.create(competition=competition, team=team)
        preview = plan_first_round_preview(scope, randomizer=self.stable_order)
        self.assertEqual(preview.fixture_count, 6)

    def test_confirmation_reuses_its_locked_margin_policy_competition_for_every_fixture(self):
        competition = self.competition()
        scope = get_league_fixture_scope(competition, self.stage(competition))
        self.singles_entries(competition, 8)
        preview = plan_first_round_preview(scope, randomizer=self.stable_order)

        with patch.object(
            PoolMatch,
            "_lock_margin_policy_competitions",
            wraps=PoolMatch._lock_margin_policy_competitions,
        ) as lock_competitions:
            created = confirm_fixture_preview(
                serialize_fixture_preview(preview), actor=self.owner, scope=scope
            )

        self.assertEqual(len(created), preview.fixture_count)
        self.assertEqual(lock_competitions.call_count, 0)

    def test_confirmation_skips_only_redundant_database_constraint_validation(self):
        competition = self.competition()
        scope = get_league_fixture_scope(competition, self.stage(competition))
        self.singles_entries(competition, 6)
        preview = plan_first_round_preview(scope, randomizer=self.stable_order)
        original_full_clean = PoolMatch.full_clean
        validation_calls = []

        def record_full_clean(instance, *args, **kwargs):
            validation_calls.append(kwargs)
            return original_full_clean(instance, *args, **kwargs)

        with patch.object(PoolMatch, "full_clean", new=record_full_clean):
            created = confirm_fixture_preview(
                serialize_fixture_preview(preview), actor=self.owner, scope=scope
            )

        self.assertEqual(len(validation_calls), len(created))
        self.assertTrue(
            all(call["validate_constraints"] is False for call in validation_calls)
        )

    def test_ordinary_match_save_keeps_database_constraint_validation_enabled(self):
        competition = self.competition()
        stage = self.stage(competition)
        home, away = self.singles_entries(competition, 2)
        original_full_clean = PoolMatch.full_clean
        validation_calls = []

        def record_full_clean(instance, *args, **kwargs):
            validation_calls.append(kwargs)
            return original_full_clean(instance, *args, **kwargs)

        with patch.object(PoolMatch, "full_clean", new=record_full_clean):
            self.match(competition, stage, home, away)

        self.assertEqual(validation_calls[-1]["validate_constraints"], True)

    def test_each_group_starts_its_generated_round_robin_at_gameweek_one(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage = self.stage(competition)
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()
        group_a = PoolGroup.objects.create(stage=stage, name="Group A", order=1)
        group_b = PoolGroup.objects.create(stage=stage, name="Group B", order=2)
        entries = self.singles_entries(competition, 8)
        for order, entry in enumerate(entries[:4]):
            PoolGroupEntry.objects.create(
                stage=stage, group=group_a, entry=entry, sort_order=order
            )
        for order, entry in enumerate(entries[4:]):
            PoolGroupEntry.objects.create(
                stage=stage, group=group_b, entry=entry, sort_order=order
            )

        first_a = plan_first_round_preview(
            get_league_fixture_scope(competition, stage, group_a),
            randomizer=self.stable_order,
        )
        first_b = plan_first_round_preview(
            get_league_fixture_scope(competition, stage, group_b),
            randomizer=self.stable_order,
        )

        self.assertEqual(min(item.gameweek for item in first_a.fixtures), 1)
        self.assertEqual(min(item.gameweek for item in first_b.fixtures), 1)

    def test_single_group_round_robin_keeps_two_fixtures_in_each_game(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage = self.stage(competition)
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()

        group, created = self.generate_group_round_robin(
            competition, stage, "Group A", 1, 4
        )

        self.assertEqual(len(created), 6)
        self.assertEqual(
            list(
                PoolMatch.objects.filter(group=group)
                .order_by("gameweek", "pk")
                .values_list("gameweek", flat=True)
            ),
            [1, 1, 2, 2, 3, 3],
        )
        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )
        sections = response.context["fixture_gameweek_sections"]
        self.assertEqual(
            [(section["round_term"], section["gameweek"]) for section in sections],
            [("Game", 1), ("Game", 2), ("Game", 3)],
        )
        self.assertEqual(
            [len(section["groups"][0]["matches"]) for section in sections],
            [2, 2, 2],
        )

    def test_public_group_stage_groups_shared_games_before_groups_and_results(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage = self.stage(competition)
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()
        group_a, fixtures_a = self.generate_group_round_robin(
            competition, stage, "Group A", 1, 4
        )
        group_b, fixtures_b = self.generate_group_round_robin(
            competition, stage, "Group B", 2, 4
        )

        self.assertEqual(len(fixtures_a), 6)
        self.assertEqual(len(fixtures_b), 6)
        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )
        sections = response.context["fixture_gameweek_sections"]
        self.assertEqual(
            [(section["round_term"], section["gameweek"]) for section in sections],
            [("Game", 1), ("Game", 2), ("Game", 3)],
        )
        self.assertEqual(
            [
                [(group_section["group"].name, len(group_section["matches"]))
                for group_section in section["groups"]]
                for section in sections
            ],
            [
                [("Group A", 2), ("Group B", 2)],
                [("Group A", 2), ("Group B", 2)],
                [("Group A", 2), ("Group B", 2)],
            ],
        )

        for fixture in PoolMatch.objects.filter(
            competition=competition, gameweek=2
        ):
            record_non_played_result(
                fixture,
                result_method=PoolMatch.ResultMethod.WALKOVER,
                winner=fixture.home_entry,
                recorded_by=self.owner,
            )
        results = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=results"
        )
        result_sections = results.context["result_gameweek_sections"]
        self.assertEqual(
            [(section["round_term"], section["gameweek"]) for section in result_sections],
            [("Game", 2)],
        )
        self.assertEqual(
            [(group_section["group"].name, len(group_section["matches"]))
            for group_section in result_sections[0]["groups"]],
            [("Group A", 2), ("Group B", 2)],
        )

    def test_unequal_group_sizes_leave_later_games_with_only_the_remaining_group(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage = self.stage(competition)
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()
        self.generate_group_round_robin(competition, stage, "Group A", 1, 4)
        self.generate_group_round_robin(competition, stage, "Group B", 2, 6)

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )
        sections = response.context["fixture_gameweek_sections"]
        self.assertEqual([section["gameweek"] for section in sections], [1, 2, 3, 4, 5])
        self.assertEqual(
            [
                (group_section["group"].name, len(group_section["matches"]))
                for group_section in sections[3]["groups"]
            ],
            [("Group B", 3)],
        )
        self.assertEqual(
            [
                (group_section["group"].name, len(group_section["matches"]))
                for group_section in sections[4]["groups"]
            ],
            [("Group B", 3)],
        )

    def test_normal_league_public_context_keeps_gameweek_terminology(self):
        competition = self.competition()
        stage = self.stage(competition)
        self.singles_entries(competition, 4)
        scope = get_league_fixture_scope(competition, stage)
        preview = plan_first_round_preview(scope, randomizer=self.stable_order)
        confirm_fixture_preview(serialize_fixture_preview(preview), actor=self.owner, scope=scope)

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )
        sections = response.context["fixture_gameweek_sections"]
        self.assertEqual(
            [(section["round_term"], section["gameweek"]) for section in sections],
            [("Gameweek", 1), ("Gameweek", 2), ("Gameweek", 3)],
        )
        self.assertTrue(all(not section["groups"] for section in sections))

    def test_unassigned_public_rows_remain_in_an_explicit_fallback_section(self):
        competition = self.competition()
        stage = self.stage(competition)
        home, away = self.singles_entries(competition, 2)
        fixture = self.match(competition, stage, home, away)

        fixtures = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )
        fixture_sections = fixtures.context["fixture_gameweek_sections"]
        self.assertEqual(len(fixture_sections), 1)
        self.assertIsNone(fixture_sections[0]["gameweek"])
        self.assertEqual(fixture_sections[0]["matches"], [fixture])

        record_non_played_result(
            fixture,
            result_method=PoolMatch.ResultMethod.WALKOVER,
            winner=home,
            recorded_by=self.owner,
        )
        results = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=results"
        )
        result_sections = results.context["result_gameweek_sections"]
        self.assertEqual(len(result_sections), 1)
        self.assertIsNone(result_sections[0]["gameweek"])
        self.assertEqual(result_sections[0]["matches"], [fixture])

    def test_fixture_generator_applies_group_scope_before_preview_and_confirmation(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage = self.stage(competition)
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()
        group_a, entries_a = self.group_with_entries(
            competition, stage, "Group A", 1, 4
        )
        group_b, entries_b = self.group_with_entries(
            competition, stage, "Group B", 2, 4
        )
        group_c, _entries_c = self.group_with_entries(
            competition, stage, "Group C", 3, 4
        )
        scope_a = get_league_fixture_scope(competition, stage, group_a)
        preview_a = plan_first_round_preview(scope_a, randomizer=self.stable_order)
        created_a = confirm_fixture_preview(
            serialize_fixture_preview(preview_a), actor=self.owner, scope=scope_a
        )
        generator_url = reverse(
            "pool:manage_competition_fixture_generator", args=[competition.slug]
        )
        self.client.force_login(self.owner)

        group_a_page = self.client.get(
            generator_url, {"stage": stage.pk, "group": group_a.pk}
        )
        self.assertEqual(group_a_page.context["selected_group"], group_a)
        self.assertEqual(group_a_page.context["fixture_scope"].group, group_a)
        self.assertTrue(group_a_page.context["fixture_status"]["first_round_generated"])

        group_b_page = self.client.get(
            generator_url, {"stage": stage.pk, "group": group_b.pk}
        )
        self.assertEqual(group_b_page.context["selected_group"], group_b)
        self.assertEqual(group_b_page.context["fixture_scope"].group, group_b)
        self.assertFalse(group_b_page.context["fixture_status"]["first_round_generated"])
        self.assertContains(group_b_page, f'<option value="{group_b.pk}" selected>')
        self.assertContains(group_b_page, 'id="pool-fixture-status-title">Group B')
        self.assertContains(group_b_page, "Generate First Round Preview")
        self.assertNotContains(group_b_page, "Generate Second Round Preview")
        self.assertContains(group_b_page, "data-pool-fixture-scope-form")
        self.assertContains(group_b_page, "fixture_generator_scope.js")
        self.assertContains(
            group_b_page,
            f'<input type="hidden" name="stage" value="{stage.pk}">',
        )
        self.assertContains(
            group_b_page,
            f'<input type="hidden" name="group" value="{group_b.pk}">',
        )

        group_c_page = self.client.get(
            generator_url, {"stage": stage.pk, "group": group_c.pk}
        )
        self.assertEqual(group_c_page.context["fixture_scope"].group, group_c)
        self.assertFalse(group_c_page.context["fixture_status"]["first_round_generated"])

        preview_response = self.client.post(
            generator_url,
            {
                "action": "preview_first_round",
                "stage": stage.pk,
                "group": group_b.pk,
            },
        )
        preview_b = preview_response.context["fixture_preview"]
        self.assertEqual(preview_b.scope.group_id, group_b.pk)
        self.assertEqual(set(preview_b.entry_ids), {entry.pk for entry in entries_b})
        self.assertTrue(
            all(
                fixture.home_entry_id in {entry.pk for entry in entries_b}
                and fixture.away_entry_id in {entry.pk for entry in entries_b}
                for fixture in preview_b.fixtures
            )
        )
        self.assertFalse(
            set(preview_b.entry_ids).intersection(entry.pk for entry in entries_a)
        )

        token = preview_response.context["confirm_form"].initial["token"]
        confirmation = self.client.post(
            generator_url,
            {
                "action": "confirm",
                "stage": stage.pk,
                "group": group_b.pk,
                "token": token,
                "confirm": "on",
            },
        )
        self.assertRedirects(
            confirmation,
            f"{generator_url}?stage={stage.pk}&group={group_b.pk}",
        )
        self.assertEqual(
            PoolMatch.objects.filter(group=group_a).count(), len(created_a)
        )
        self.assertEqual(PoolMatch.objects.filter(group=group_b).count(), 6)
        self.assertFalse(PoolMatch.objects.filter(group=group_c).exists())

    def test_fixture_generator_rejects_a_group_from_another_stage(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        first_stage = self.stage(competition)
        first_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        first_stage.name = "First group stage"
        first_stage.save()
        second_stage = PoolCompetitionStage.objects.create(
            competition=competition,
            name="Second group stage",
            stage_type=PoolCompetitionStage.StageType.GROUP,
            order=3,
        )
        first_group, _first_entries = self.group_with_entries(
            competition, first_stage, "Group A", 1, 4
        )
        second_group, _second_entries = self.group_with_entries(
            competition, second_stage, "Group B", 1, 4
        )
        generator_url = reverse(
            "pool:manage_competition_fixture_generator", args=[competition.slug]
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            generator_url,
            {"stage": first_stage.pk, "group": second_group.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["fixture_scope"])
        self.assertContains(response, "The selected group does not belong to this stage.")
        self.assertFalse(PoolMatch.objects.filter(group__in=[first_group, second_group]).exists())

    def test_four_six_entry_groups_confirm_their_sixty_fixtures(self):
        competition = self.competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage = self.stage(competition)
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()
        entries = self.singles_entries(competition, 24)
        created = []
        for group_number in range(1, 5):
            group = PoolGroup.objects.create(
                stage=stage, name=f"Group {group_number}", order=group_number
            )
            start = (group_number - 1) * 6
            for sort_order, entry in enumerate(entries[start : start + 6]):
                PoolGroupEntry.objects.create(
                    stage=stage, group=group, entry=entry, sort_order=sort_order
                )
            scope = get_league_fixture_scope(competition, stage, group)
            preview = plan_first_round_preview(scope, randomizer=self.stable_order)
            self.assertEqual(preview.fixture_count, 15)
            created.extend(
                confirm_fixture_preview(
                    serialize_fixture_preview(preview), actor=self.owner, scope=scope
                )
            )

        self.assertEqual(len(created), 60)
        self.assertEqual(PoolMatch.objects.filter(competition=competition).count(), 60)


class PoolScoringAndPublicTabTests(PoolLeaguePlatformTestCase):
    def test_ajax_rack_submission_is_idempotent_and_returns_scores(self):
        competition = self.competition()
        stage = self.stage(competition)
        home, away = self.singles_entries(competition, 2)
        fixture = self.match(competition, stage, home, away)
        start_match(fixture, recorded_by=self.owner)
        url = reverse("pool:manage_match_record_rack", args=[competition.slug, fixture.pk])
        token = str(uuid4())
        self.client.force_login(self.owner)
        payload = {"winner": home.pk, "outcome_type": "NORMAL", "request_token": token}
        first = self.client.post(url, payload, HTTP_ACCEPT="application/json")
        second = self.client.post(url, payload, HTTP_ACCEPT="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PoolMatch.objects.get(pk=fixture.pk).racks.count(), 1)
        self.assertEqual(first.json()["home_score"], 1)

    def test_public_tabs_are_fragments_and_private_competitions_remain_hidden(self):
        competition = self.competition()
        stage = self.stage(competition)
        self.singles_entries(competition, 2)
        detail = self.client.get(reverse("pool:competition_detail", args=[competition.slug]))
        self.assertContains(detail, "data-pool-competition-tab-shell")
        self.assertContains(detail, "data-pool-competition-tab-panel")
        fragment = self.client.get(reverse("pool:competition_tab_content", args=[competition.slug, "standings"]))
        self.assertEqual(fragment.status_code, 200)
        self.assertNotContains(fragment, "data-pool-competition-tab-shell")
        self.assertEqual(self.client.get(reverse("pool:competition_tab_content", args=[competition.slug, "unknown"])).status_code, 404)
        competition.visibility = PoolCompetition.Visibility.DRAFT
        competition.save()
        self.assertEqual(self.client.get(reverse("pool:competition_tab_content", args=[competition.slug, "standings"])).status_code, 404)
