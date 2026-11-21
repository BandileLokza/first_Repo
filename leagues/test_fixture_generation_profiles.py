from datetime import timedelta
from io import StringIO
from random import Random
from tempfile import TemporaryDirectory
from time import perf_counter

from django.core.exceptions import ValidationError
from django.core import signing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.forms import FixtureForm
from league_manager.models import LeagueMembership
from leagues.fixtures import (
    _circle_large_first_round_orientations,
    _entries_balance,
    build_generation_preview,
    confirm_generation_preview,
    generate_first_round,
    generate_remaining_first_round,
    generate_second_round,
    round_generation_state,
)
from leagues.football import recent_form_by_team
from leagues.models import Competition, CompetitionSeries, Region, Stage, Standing
from leagues.views import _public_competitions, _public_league_cards
from matches.models import Match, Player
from teams.models import Team


User = get_user_model()


class TableLeagueFixtureGenerationTests(TestCase):
    def make_league(
        self, team_count=4, *, archived=False, format_type="TABLE", slug_suffix=""
    ):
        competition = Competition.objects.create(
            name="Fixture League",
            slug=(
                f"fixture-league-{team_count}-{format_type.lower()}-{archived}{slug_suffix}"
            ),
            season="2026/27",
            format_type=format_type,
            is_archived=archived,
        )
        stage = Stage.objects.create(
            competition=competition, name="League", stage_type="LEAGUE", order=1
        )
        teams = [
            Team.objects.create(name=f"Team {number}", competition=competition)
            for number in range(team_count)
        ]
        return competition, stage, teams

    def test_manual_fixture_allows_null_or_explicit_gameweek(self):
        competition, stage, teams = self.make_league()
        fixture = Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[0],
            away_team=teams[1],
            match_date=None,
        )
        self.assertIsNone(fixture.gameweek)
        form = FixtureForm(
            data={
                "stage": stage.id,
                "home_team": teams[2].id,
                "away_team": teams[3].id,
                "gameweek": 4,
                "match_date": "",
                "location": "",
                "leg": 1,
            },
            league=competition,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["gameweek"], 4)

    def test_manual_fixture_uses_home_ground_only_as_an_overridable_default(self):
        competition, stage, teams = self.make_league()
        teams[0].home_ground = "Home Ground"
        teams[0].save(update_fields=["home_ground"])
        base_data = {
            "stage": stage.id,
            "home_team": teams[0].id,
            "away_team": teams[1].id,
            "gameweek": 1,
            "match_date": "",
            "leg": 1,
        }
        default_form = FixtureForm(data={**base_data, "location": ""}, league=competition)
        self.assertTrue(default_form.is_valid(), default_form.errors)
        self.assertEqual(default_form.cleaned_data["location"], "Home Ground")
        neutral_form = FixtureForm(data={**base_data, "location": "Neutral Venue"}, league=competition)
        self.assertTrue(neutral_form.is_valid(), neutral_form.errors)
        self.assertEqual(neutral_form.cleaned_data["location"], "Neutral Venue")

    def test_even_first_round_is_complete_and_gameweek_safe(self):
        competition, _stage, teams = self.make_league(4)
        self.assertEqual(generate_first_round(competition), 6)
        fixtures = list(Match.objects.filter(competition=competition).order_by("gameweek", "id"))
        self.assertEqual({fixture.gameweek for fixture in fixtures}, {1, 2, 3})
        self.assertTrue(all(fixture.match_date is None for fixture in fixtures))
        self.assertEqual(
            {frozenset((fixture.home_team_id, fixture.away_team_id)) for fixture in fixtures},
            {frozenset((a.id, b.id)) for index, a in enumerate(teams) for b in teams[index + 1:]},
        )
        for gameweek in range(1, 4):
            ids = [
                team_id
                for fixture in fixtures if fixture.gameweek == gameweek
                for team_id in (fixture.home_team_id, fixture.away_team_id)
            ]
            self.assertEqual(len(ids), len(set(ids)))

    def test_random_draw_and_balanced_venues_preserve_round_robin_invariants(self):
        competition, _stage, teams = self.make_league(6)
        observed_input = []

        def reverse_draw(items):
            observed_input.extend(team.id for team in items)
            items.reverse()

        generate_first_round(competition, shuffle=reverse_draw)
        fixtures = list(Match.objects.filter(competition=competition).order_by("gameweek", "id"))
        self.assertEqual(observed_input, sorted(observed_input, key=lambda team_id: next(team.name for team in teams if team.id == team_id)))
        self.assertEqual(len(fixtures), 15)
        self.assertEqual({fixture.gameweek for fixture in fixtures}, {1, 2, 3, 4, 5})
        homes = {team.id: 0 for team in teams}
        venues = {team.id: [] for team in teams}
        for fixture in fixtures:
            homes[fixture.home_team_id] += 1
            venues[fixture.home_team_id].append((fixture.gameweek, "H"))
            venues[fixture.away_team_id].append((fixture.gameweek, "A"))
        self.assertTrue(all(count in {2, 3} for count in homes.values()))
        for history in venues.values():
            values = [venue for _gameweek, venue in sorted(history)]
            self.assertFalse(any(values[index:index + 3] == ["H"] * 3 or values[index:index + 3] == ["A"] * 3 for index in range(len(values) - 2)))

    def test_odd_first_round_gives_one_bye_per_gameweek(self):
        competition, _stage, teams = self.make_league(5)
        generate_first_round(competition)
        fixtures = list(Match.objects.filter(competition=competition))
        self.assertEqual(len(fixtures), 10)
        for gameweek in range(1, 6):
            round_fixtures = [fixture for fixture in fixtures if fixture.gameweek == gameweek]
            self.assertEqual(len(round_fixtures), 2)
            participants = {
                team_id
                for fixture in round_fixtures
                for team_id in (fixture.home_team_id, fixture.away_team_id)
            }
            self.assertEqual(len({team.id for team in teams} - participants), 1)

    def test_second_round_is_exact_reverse_and_continues_gameweeks(self):
        competition, _stage, _teams = self.make_league(4)
        generate_first_round(competition)
        first = list(Match.objects.filter(competition=competition))
        self.assertEqual(generate_second_round(competition), 6)
        second = list(Match.objects.filter(competition=competition, leg=2))
        self.assertEqual(len(second), 6)
        self.assertEqual({fixture.gameweek for fixture in second}, {4, 5, 6})
        self.assertEqual(
            {(fixture.home_team_id, fixture.away_team_id) for fixture in second},
            {(fixture.away_team_id, fixture.home_team_id) for fixture in first},
        )

    def test_generated_fixtures_copy_the_current_home_team_ground(self):
        competition, _stage, teams = self.make_league(4)
        for number, team in enumerate(teams):
            team.home_ground = f"Ground {number}"
            team.save(update_fields=["home_ground"])

        generate_first_round(competition)
        first_round = list(Match.objects.filter(competition=competition).select_related("home_team"))
        self.assertTrue(all(match.location == match.home_team.home_ground for match in first_round))

        generate_second_round(competition)
        second_round = Match.objects.filter(competition=competition, leg=2).select_related("home_team")
        self.assertTrue(all(match.location == match.home_team.home_ground for match in second_round))

    def test_missing_or_changed_home_ground_does_not_rewrite_existing_fixture(self):
        competition, _stage, teams = self.make_league(2)
        teams[0].home_ground = "Original Ground"
        teams[0].save(update_fields=["home_ground"])
        generate_first_round(competition)
        fixture = Match.objects.get(competition=competition)
        original_location = fixture.location
        fixture.home_team.home_ground = "Changed Ground"
        fixture.home_team.save(update_fields=["home_ground"])
        fixture.refresh_from_db()
        self.assertEqual(fixture.location, original_location)

    def test_generation_rejects_unsafe_states_without_partial_fixtures(self):
        competition, stage, teams = self.make_league(4)
        Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            match_date=timezone.now(), status="FINISHED",
        )
        with self.assertRaises(ValidationError):
            generate_first_round(competition)
        self.assertEqual(Match.objects.filter(competition=competition).count(), 1)

        archived, _stage, _teams = self.make_league(2, archived=True)
        with self.assertRaises(ValidationError):
            generate_first_round(archived)

        non_table, _stage, _teams = self.make_league(2, format_type="KNOCKOUT")
        with self.assertRaises(ValidationError):
            generate_first_round(non_table)

    def test_second_round_rejects_team_set_change_and_duplicate_generation(self):
        competition, _stage, teams = self.make_league(4)
        generate_first_round(competition)
        Team.objects.create(name="Late team", competition=competition)
        with self.assertRaises(ValidationError):
            generate_second_round(competition)

    def test_manager_generation_post_requires_manager_permission(self):
        competition, _stage, _teams = self.make_league(4)
        url = reverse("league_manager:generate_first_round", args=[competition.slug])
        preview_url = reverse(
            "league_manager:fixture_generation_preview", args=[competition.slug, "first"]
        )
        self.assertEqual(self.client.post(url).status_code, 302)
        manager = User.objects.create_user(username="fixture-manager", password="pass")
        LeagueMembership.objects.create(
            user=manager,
            competition=competition,
            role=LeagueMembership.Role.ADMIN,
            is_active=True,
        )
        self.client.force_login(manager)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Match.objects.filter(competition=competition).count(), 0)
        preview = self.client.post(preview_url)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Review Fixture Plan")
        self.assertEqual(Match.objects.filter(competition=competition).count(), 0)
        confirmation = self.client.post(url, {"preview_token": preview.context["token"]})
        self.assertEqual(confirmation.status_code, 302)
        self.assertEqual(Match.objects.filter(competition=competition).count(), 6)

        competition, _stage, _teams = self.make_league(6)
        generate_first_round(competition)
        generate_second_round(competition)
        with self.assertRaises(ValidationError):
            generate_second_round(competition)

    def test_manager_round_actions_follow_the_verified_generation_state(self):
        competition, stage, teams = self.make_league(4)
        manager = User.objects.create_user(username="fixture-ui-manager", password="pass")
        LeagueMembership.objects.create(
            user=manager,
            competition=competition,
            role=LeagueMembership.Role.ADMIN,
            is_active=True,
        )
        self.client.force_login(manager)
        fixture_list_url = reverse("league_manager:fixture_list", args=[competition.slug])

        response = self.client.get(fixture_list_url)
        self.assertTrue(response.context["can_generate_first_round"])
        self.assertFalse(response.context["can_generate_second_round"])
        self.assertContains(response, "Preview First Round")
        self.assertNotContains(response, "Preview Second Round")

        generate_first_round(competition)
        response = self.client.get(fixture_list_url)
        self.assertFalse(response.context["can_generate_first_round"])
        self.assertTrue(response.context["can_generate_second_round"])
        self.assertNotContains(response, "Preview First Round")
        self.assertContains(response, "Preview Second Round")

        generate_second_round(competition)
        response = self.client.get(fixture_list_url)
        self.assertFalse(response.context["can_generate_first_round"])
        self.assertFalse(response.context["can_generate_second_round"])
        self.assertNotContains(response, "Preview First Round")
        self.assertNotContains(response, "Preview Second Round")

        # A valid manual partial fixture continues safely instead of being
        # mistaken for a complete generated round.
        manual, manual_stage, manual_teams = self.make_league(5)
        Match.objects.create(
            competition=manual,
            stage=manual_stage,
            home_team=manual_teams[0],
            away_team=manual_teams[1],
            gameweek=1,
        )
        self.assertEqual(
            round_generation_state(manual),
            {
                "can_generate_first_round": False,
                "can_generate_remaining_first_round": True,
                "can_generate_second_round": False,
            },
        )

    def test_remaining_round_preserves_history_and_completes_each_pair_once(self):
        competition, stage, teams = self.make_league(6)
        historical = [
            Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=teams[0],
                away_team=teams[1],
                gameweek=1,
                status="FINISHED",
                home_score=2,
                away_score=1,
                location="Historical Ground",
            ),
            Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=teams[2],
                away_team=teams[3],
                gameweek=1,
                status="FINISHED",
            ),
            Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=teams[4],
                away_team=teams[5],
                gameweek=1,
                status="FINISHED",
            ),
            Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=teams[0],
                away_team=teams[2],
                gameweek=2,
                status="FINISHED",
            ),
        ]
        historical_values = {
            match.id: (match.home_team_id, match.away_team_id, match.home_score, match.location)
            for match in historical
        }

        self.assertEqual(generate_remaining_first_round(competition), 11)
        fixtures = list(Match.objects.filter(competition=competition).order_by("gameweek", "id"))
        generated_gameweeks = sorted(
            {match.gameweek for match in fixtures if match.id not in historical_values}
        )
        # The constrained scheduler fills the two open slots in historical
        # Gameweek 2 rather than abandoning that partial gameweek and starting
        # an unnecessarily long first round at Gameweek 3.
        self.assertEqual(generated_gameweeks[0], 2)
        self.assertEqual(
            generated_gameweeks,
            list(range(2, 2 + len(generated_gameweeks))),
        )
        self.assertEqual(
            {frozenset((match.home_team_id, match.away_team_id)) for match in fixtures},
            {frozenset((first.id, second.id)) for index, first in enumerate(teams) for second in teams[index + 1:]},
        )
        for gameweek in generated_gameweeks:
            participants = [
                team_id for match in fixtures if match.gameweek == gameweek and match.id not in historical_values
                for team_id in (match.home_team_id, match.away_team_id)
            ]
            self.assertEqual(len(participants), len(set(participants)))
        self.assertEqual(
            [
                Match.objects.filter(competition=competition, gameweek=gameweek).count()
                for gameweek in range(1, 6)
            ],
            [3, 3, 3, 3, 3],
        )
        for match in Match.objects.filter(id__in=historical_values):
            self.assertEqual(
                (match.home_team_id, match.away_team_id, match.home_score, match.location),
                historical_values[match.id],
            )
        self.assertTrue(round_generation_state(competition)["can_generate_second_round"])
        self.assertEqual(generate_second_round(competition), 15)
        first_round = Match.objects.filter(competition=competition, leg=1)
        second_round = Match.objects.filter(competition=competition, leg=2)
        self.assertEqual(
            {(match.home_team_id, match.away_team_id) for match in second_round},
            {(match.away_team_id, match.home_team_id) for match in first_round},
        )

    def test_remaining_round_rejects_duplicate_historical_pairings(self):
        competition, stage, teams = self.make_league(4)
        Match.objects.create(competition=competition, stage=stage, home_team=teams[0], away_team=teams[1], gameweek=1)
        Match.objects.create(competition=competition, stage=stage, home_team=teams[1], away_team=teams[0], gameweek=2)
        with self.assertRaisesMessage(ValidationError, "duplicate first-round matchup"):
            generate_remaining_first_round(competition)

    def test_eight_team_manual_schedule_with_different_pairings_is_completed_in_seven_gameweeks(self):
        competition, stage, teams = self.make_league(8)
        manual_pairs = {
            1: [(0, 1), (2, 3), (4, 5), (6, 7)],
            2: [(0, 2), (1, 3), (4, 6), (5, 7)],
        }
        manual = []
        for gameweek, pairs in manual_pairs.items():
            for home, away in pairs:
                manual.append(
                    Match.objects.create(
                        competition=competition,
                        stage=stage,
                        home_team=teams[home],
                        away_team=teams[away],
                        gameweek=gameweek,
                        status="UPCOMING",
                    )
                )
        original = {
            match.id: (match.gameweek, match.home_team_id, match.away_team_id, match.status)
            for match in manual
        }

        self.assertEqual(generate_remaining_first_round(competition), 20)
        first_round = Match.objects.filter(competition=competition, leg=1)
        self.assertEqual(first_round.count(), 28)
        self.assertEqual(
            [first_round.filter(gameweek=gameweek).count() for gameweek in range(1, 8)],
            [4] * 7,
        )
        for match in Match.objects.filter(id__in=original):
            self.assertEqual(
                (match.gameweek, match.home_team_id, match.away_team_id, match.status),
                original[match.id],
            )
        self.assertEqual(generate_second_round(competition), 28)
        self.assertEqual(Match.objects.filter(competition=competition).count(), 56)

    def test_partial_gameweek_is_completed_without_changing_fixed_matches(self):
        competition, stage, teams = self.make_league(8)
        fixed = []
        for home, away in ((0, 7), (1, 6), (2, 5), (3, 4)):
            fixed.append(Match.objects.create(
                competition=competition, stage=stage,
                home_team=teams[home], away_team=teams[away], gameweek=1,
            ))
        for home, away in ((0, 6), (7, 5)):
            fixed.append(Match.objects.create(
                competition=competition, stage=stage,
                home_team=teams[home], away_team=teams[away], gameweek=2,
            ))
        original = {match.id: (match.home_team_id, match.away_team_id, match.gameweek) for match in fixed}

        self.assertEqual(generate_remaining_first_round(competition), 22)
        self.assertEqual(Match.objects.filter(competition=competition, gameweek=2).count(), 4)
        self.assertEqual(
            [Match.objects.filter(competition=competition, gameweek=gameweek).count() for gameweek in range(1, 8)],
            [4] * 7,
        )
        for match in Match.objects.filter(id__in=original):
            self.assertEqual(
                (match.home_team_id, match.away_team_id, match.gameweek), original[match.id]
            )

    def test_mixed_finished_and_upcoming_history_is_status_invariant(self):
        competition, stage, teams = self.make_league(8)
        pairs = ((0, 7), (1, 6), (2, 5), (3, 4), (0, 6), (7, 5), (1, 4), (2, 3))
        for index, (home, away) in enumerate(pairs):
            Match.objects.create(
                competition=competition, stage=stage,
                home_team=teams[home], away_team=teams[away],
                gameweek=1 if index < 4 else 2,
                status="FINISHED" if index % 2 else "UPCOMING",
            )
        self.assertEqual(generate_remaining_first_round(competition), 20)
        self.assertEqual(Match.objects.filter(competition=competition, leg=1).count(), 28)
        self.assertEqual(
            [Match.objects.filter(competition=competition, gameweek=gameweek).count() for gameweek in range(1, 8)],
            [4] * 7,
        )

    def test_seven_team_double_round_has_one_bye_per_team_per_round(self):
        competition, _stage, teams = self.make_league(7)
        self.assertEqual(generate_first_round(competition), 21)
        self.assertEqual(generate_second_round(competition), 21)
        fixtures = Match.objects.filter(competition=competition)
        self.assertEqual(fixtures.count(), 42)
        self.assertEqual(
            [fixtures.filter(gameweek=gameweek).count() for gameweek in range(1, 15)],
            [3] * 14,
        )
        for gameweek in range(1, 15):
            participants = {
                team_id
                for fixture in fixtures.filter(gameweek=gameweek)
                for team_id in (fixture.home_team_id, fixture.away_team_id)
            }
            self.assertEqual(len(participants), 6)
        for team in teams:
            self.assertEqual(
                fixtures.filter(home_team=team).count(), 6,
            )
            self.assertEqual(
                fixtures.filter(away_team=team).count(), 6,
            )

    def test_preview_reports_balanced_home_away_sequences_for_new_eight_team_league(self):
        competition, _stage, teams = self.make_league(8)
        preview = build_generation_preview(competition, "first")
        for team in teams:
            values = preview["balance"]["teams"][team.id]
            self.assertIn(values["home"], {3, 4})
            self.assertIn(values["away"], {3, 4})
            self.assertLessEqual(values["longest_streak"], 2, values)

    def test_new_eight_team_home_away_balance_is_stable_across_seeded_draws(self):
        """A random circle draw must not determine whether balance is fair."""

        for seed in range(24):
            competition, _stage, teams = self.make_league(8, slug_suffix=f"-seed-{seed}")
            generate_first_round(competition, shuffle=Random(seed).shuffle)
            entries = [
                (match.gameweek, match.home_team_id, match.away_team_id)
                for match in Match.objects.filter(competition=competition, leg=1)
            ]
            balance = _entries_balance(tuple(team.id for team in teams), entries)
            for team in teams:
                values = balance["teams"][team.id]
                self.assertIn(values["home"], {3, 4}, (seed, values))
                self.assertIn(values["away"], {3, 4}, (seed, values))
                self.assertLessEqual(values["longest_streak"], 2, (seed, values))

    def test_second_round_preview_preserves_reverse_fixtures_and_balances_whole_season(self):
        competition, _stage, teams = self.make_league(8)
        generate_first_round(competition, shuffle=lambda items: None)
        preview = build_generation_preview(competition, "second")
        self.assertEqual(len(preview["proposed"]), 28)
        first = Match.objects.filter(competition=competition, leg=1)
        proposed = {(fixture.home_team_id, fixture.away_team_id) for fixture in preview["proposed"]}
        self.assertEqual(
            proposed,
            {(match.away_team_id, match.home_team_id) for match in first},
        )
        for team in teams:
            values = preview["balance"]["teams"][team.id]
            self.assertEqual(values["home"], 7)
            self.assertEqual(values["away"], 7)
            self.assertLessEqual(values["longest_streak"], 2, values)

    def test_large_new_league_generation_remains_bounded(self):
        timings = []
        for team_count in (12, 14, 16, 18, 20):
            competition, _stage, _teams = self.make_league(team_count)
            started = perf_counter()
            generate_first_round(competition)
            generate_second_round(competition)
            timings.append(perf_counter() - started)
            expected_total = team_count * (team_count - 1)
            self.assertEqual(Match.objects.filter(competition=competition).count(), expected_total)
        # This is a regression guard for the bounded implementation, not a
        # production SLA. It leaves generous headroom for slower CI hosts.
        self.assertTrue(all(value < 5 for value in timings), timings)

    def test_large_fresh_first_rounds_have_no_three_match_venue_streaks(self):
        for team_count in (12, 14, 16, 18, 20):
            competition, _stage, teams = self.make_league(
                team_count, slug_suffix=f"-large-balance-{team_count}"
            )
            self.assertEqual(
                generate_first_round(competition),
                team_count * (team_count - 1) // 2,
            )
            fixtures = Match.objects.filter(competition=competition, leg=1)
            self.assertEqual(
                [fixtures.filter(gameweek=gameweek).count() for gameweek in range(1, team_count)],
                [team_count // 2] * (team_count - 1),
            )
            balance = _entries_balance(
                tuple(team.id for team in teams),
                [
                    (fixture.gameweek, fixture.home_team_id, fixture.away_team_id)
                    for fixture in fixtures
                ],
            )
            for team in teams:
                values = balance["teams"][team.id]
                self.assertIn(values["home"], {team_count // 2 - 1, team_count // 2})
                self.assertLessEqual(values["longest_streak"], 2, values)

    def test_sixteen_team_balance_is_stable_across_seeded_circle_draws(self):
        for seed in range(12):
            competition, _stage, teams = self.make_league(
                16, slug_suffix=f"-sixteen-seed-{seed}"
            )
            self.assertEqual(
                generate_first_round(competition, shuffle=Random(seed).shuffle), 120
            )
            balance = _entries_balance(
                tuple(team.id for team in teams),
                [
                    (fixture.gameweek, fixture.home_team_id, fixture.away_team_id)
                    for fixture in Match.objects.filter(competition=competition, leg=1)
                ],
            )
            for team in teams:
                values = balance["teams"][team.id]
                self.assertIn(values["home"], {7, 8}, (seed, values))
                self.assertLessEqual(values["longest_streak"], 2, (seed, values))

    def test_sixteen_team_double_round_has_no_three_match_venue_streaks(self):
        competition, _stage, teams = self.make_league(16, slug_suffix="-season-balance")
        self.assertEqual(generate_first_round(competition), 120)
        self.assertEqual(generate_second_round(competition), 120)
        fixtures = Match.objects.filter(competition=competition)
        self.assertEqual(
            [fixtures.filter(gameweek=gameweek).count() for gameweek in range(1, 31)],
            [8] * 30,
        )
        balance = _entries_balance(
            tuple(team.id for team in teams),
            [
                (fixture.gameweek, fixture.home_team_id, fixture.away_team_id)
                for fixture in fixtures
            ],
        )
        for team in teams:
            values = balance["teams"][team.id]
            self.assertEqual(values["home"], 15, values)
            self.assertEqual(values["away"], 15, values)
            self.assertLessEqual(values["longest_streak"], 2, values)

    def test_sixteen_team_continuation_preserves_two_gameweeks_and_balances(self):
        competition, stage, teams = self.make_league(16, slug_suffix="-manual-continuation")
        draw = list(teams)
        Random(0).shuffle(draw)
        initial_plan = _circle_large_first_round_orientations(
            draw,
            tuple(team.id for team in teams),
            tuple(range(1, 16)),
        )
        manual = []
        for fixture in initial_plan:
            if fixture.gameweek > 2:
                continue
            manual.append(
                Match.objects.create(
                    competition=competition,
                    stage=stage,
                    home_team_id=fixture.home_team_id,
                    away_team_id=fixture.away_team_id,
                    gameweek=fixture.gameweek,
                    leg=1,
                )
            )
        original = {
            match.id: (match.gameweek, match.home_team_id, match.away_team_id)
            for match in manual
        }
        self.assertEqual(generate_remaining_first_round(competition), 104)
        fixtures = Match.objects.filter(competition=competition, leg=1)
        self.assertEqual(
            [fixtures.filter(gameweek=gameweek).count() for gameweek in range(1, 16)],
            [8] * 15,
        )
        for match in Match.objects.filter(id__in=original):
            self.assertEqual(
                (match.gameweek, match.home_team_id, match.away_team_id), original[match.id]
            )
        balance = _entries_balance(
            tuple(team.id for team in teams),
            [
                (fixture.gameweek, fixture.home_team_id, fixture.away_team_id)
                for fixture in fixtures
            ],
        )
        for team in teams:
            values = balance["teams"][team.id]
            self.assertIn(values["home"], {7, 8}, values)
            self.assertLessEqual(values["longest_streak"], 2, values)

    def test_fixed_three_match_home_run_blocks_generation_without_writes(self):
        competition, stage, teams = self.make_league(6, slug_suffix="-fixed-streak")
        fixed_rounds = {
            1: ((0, 1), (2, 3), (4, 5)),
            2: ((0, 2), (1, 4), (3, 5)),
            3: ((0, 3), (1, 5), (2, 4)),
        }
        for gameweek, pairings in fixed_rounds.items():
            for home, away in pairings:
                Match.objects.create(
                    competition=competition,
                    stage=stage,
                    home_team=teams[home],
                    away_team=teams[away],
                    gameweek=gameweek,
                    leg=1,
                )
        existing_count = Match.objects.filter(competition=competition).count()
        with self.assertRaisesMessage(ValidationError, "Team 0"):
            generate_remaining_first_round(competition)
        self.assertEqual(Match.objects.filter(competition=competition).count(), existing_count)

    def test_confirmation_rejects_a_signed_plan_with_a_three_match_run(self):
        competition, _stage, teams = self.make_league(4, slug_suffix="-confirm-streak")
        preview = build_generation_preview(competition, "first")
        payload = signing.loads(preview["token"])
        payload["fixtures"] = [
            [1, teams[0].id, teams[1].id, 1],
            [1, teams[2].id, teams[3].id, 1],
            [2, teams[0].id, teams[2].id, 1],
            [2, teams[1].id, teams[3].id, 1],
            [3, teams[0].id, teams[3].id, 1],
            [3, teams[1].id, teams[2].id, 1],
        ]
        with self.assertRaisesMessage(ValidationError, "Team 0"):
            confirm_generation_preview(
                competition, signing.dumps(payload), action="first"
            )
        self.assertEqual(Match.objects.filter(competition=competition).count(), 0)

    def test_preview_is_read_only_then_stale_previews_are_rejected(self):
        competition, stage, teams = self.make_league(4)
        preview = build_generation_preview(competition, "first")
        self.assertEqual(Match.objects.filter(competition=competition).count(), 0)
        self.assertEqual(len(preview["proposed"]), 6)
        Match.objects.create(
            competition=competition, stage=stage,
            home_team=teams[0], away_team=teams[1], gameweek=1,
        )
        with self.assertRaisesMessage(ValidationError, "preview is stale"):
            confirm_generation_preview(competition, preview["token"])

        with self.assertRaisesMessage(ValidationError, "does not match"):
            confirm_generation_preview(competition, preview["token"], action="second")

        remaining_preview = build_generation_preview(competition, "remaining_first")
        self.assertEqual(Match.objects.filter(competition=competition).count(), 1)
        self.assertEqual(confirm_generation_preview(competition, remaining_preview["token"]), 5)
        self.assertEqual(Match.objects.filter(competition=competition, leg=1).count(), 6)

    def test_cleanup_command_is_dry_by_default_and_requires_protection_checks(self):
        from leagues.management.commands.cleanup_generated_fixtures import protection_reasons

        competition, stage, teams = self.make_league(4)
        preserved = Match.objects.create(
            competition=competition, stage=stage,
            home_team=teams[0], away_team=teams[1], gameweek=1,
        )
        # Old automatic generation used bulk_create, which intentionally does
        # not create an interactive prediction record. Mirror that eligible
        # legacy shape rather than treating a manually created fixture as safe.
        removable = Match(
            competition=competition, stage=stage,
            home_team=teams[2], away_team=teams[3], gameweek=3,
        )
        Match.objects.bulk_create([removable])
        removable.refresh_from_db()
        self.assertEqual(protection_reasons(removable), [])
        output = StringIO()
        call_command("cleanup_generated_fixtures", competition.id, stdout=output)
        self.assertTrue(Match.objects.filter(pk=preserved.pk).exists())
        self.assertTrue(Match.objects.filter(pk=removable.pk).exists())
        self.assertIn("DRY RUN", output.getvalue())

        with TemporaryDirectory() as temporary_directory:
            backup = f"{temporary_directory}/fixtures.json"
            call_command(
                "cleanup_generated_fixtures",
                competition.id,
                "--execute",
                "--confirm-competition-id",
                str(competition.id),
                "--match-ids",
                str(removable.id),
                "--backup-file",
                backup,
                stdout=StringIO(),
            )
            self.assertTrue(Match.objects.filter(pk=preserved.pk).exists())
            self.assertFalse(Match.objects.filter(pk=removable.pk).exists())
            with open(backup, encoding="utf-8") as backup_file:
                self.assertIn(str(removable.id), backup_file.read())

    def test_cleanup_command_rejects_any_protected_candidate_without_partial_delete(self):
        competition, stage, teams = self.make_league(4)
        eligible = Match(
            competition=competition, stage=stage,
            home_team=teams[0], away_team=teams[1], gameweek=3,
        )
        Match.objects.bulk_create([eligible])
        eligible.refresh_from_db()
        # Normal model creation receives the automatic PredictionMatch signal,
        # which is deliberately treated as protected linked data.
        protected = Match.objects.create(
            competition=competition, stage=stage,
            home_team=teams[2], away_team=teams[3], gameweek=4,
        )
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_generated_fixtures",
                    competition.id,
                    "--execute",
                    "--confirm-competition-id",
                    str(competition.id),
                    "--match-ids",
                    str(eligible.id),
                    str(protected.id),
                    "--backup-file",
                    f"{temporary_directory}/fixtures.json",
                    stdout=StringIO(),
                )
        self.assertTrue(Match.objects.filter(pk=eligible.pk).exists())
        self.assertTrue(Match.objects.filter(pk=protected.pk).exists())

    def test_partially_entered_second_round_is_rejected_without_writes(self):
        competition, stage, teams = self.make_league(4)
        generate_first_round(competition)
        first = Match.objects.filter(competition=competition, leg=1).first()
        Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=first.away_team,
            away_team=first.home_team,
            gameweek=4,
            leg=2,
        )
        existing_count = Match.objects.filter(competition=competition).count()
        with self.assertRaisesMessage(ValidationError, "partially entered second round"):
            generate_second_round(competition)
        self.assertEqual(Match.objects.filter(competition=competition).count(), existing_count)

    def test_public_fixture_locations_use_the_stored_match_value(self):
        competition, stage, teams = self.make_league(2)
        teams[0].home_ground = "Changed team ground"
        teams[0].save(update_fields=["home_ground"])
        dated_fixture = Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[0],
            away_team=teams[1],
            gameweek=2,
            location="Stored Mavuso Sports Centre",
            match_date=timezone.now() + timedelta(days=2),
        )
        Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[1],
            away_team=teams[0],
            gameweek=2,
            match_date=None,
            location="A deliberately long undated venue name",
            leg=2,
        )

        detail = self.client.get(reverse("league_detail", args=[competition.slug]))
        self.assertContains(detail, "Stored Mavuso Sports Centre", count=2)
        self.assertNotContains(detail, "Changed team ground")
        self.assertNotContains(detail, "fixture-location--compact")

        preview = self.client.get(reverse("league_list") + "?tab=zonal")
        self.assertNotContains(preview, "Date: TBA")
        self.assertContains(
            preview,
            timezone.localtime(dated_fixture.match_date).strftime("%b %d"),
        )
        self.assertNotContains(preview, "Stored Mavuso Sports Centre")
        self.assertNotContains(preview, "A deliberately long undated venue name")
        self.assertNotContains(preview, 'class="fixture-location"')

    def test_compact_result_preview_uses_a_dedicated_score_column(self):
        competition, stage, teams = self.make_league(2)
        teams[0].name = "A very long home club name"
        teams[0].save(update_fields=["name"])
        Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[0],
            away_team=teams[1],
            status="FINISHED",
            home_score=4,
            away_score=0,
            location="Venue hidden from compact preview",
            match_date=timezone.now() - timedelta(days=1),
        )

        preview = self.client.get(reverse("league_list") + "?tab=zonal")
        self.assertContains(preview, "A very long home club name")
        self.assertContains(preview, 'class="compact-score-column"')
        self.assertContains(preview, '<span class="compact-score">4</span>')
        self.assertContains(preview, '<span class="compact-score">0</span>')
        self.assertNotContains(preview, "Venue hidden from compact preview")

    def test_public_preview_excludes_tba_from_a_scheduled_future_context(self):
        competition, stage, teams = self.make_league(4)
        Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=1, status="FINISHED", match_date=timezone.now() - timedelta(days=7),
        )
        dated = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[2],
            gameweek=3, match_date=timezone.now() + timedelta(days=2),
        )
        undated = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[1], away_team=teams[3],
            gameweek=3, match_date=None,
        )
        next_gameweek = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[3],
            gameweek=4, match_date=timezone.now() + timedelta(days=9),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Upcoming Fixtures · GW3")
        self.assertEqual(item["display_matches"], [dated])
        self.assertNotIn(undated, item["display_matches"])
        self.assertNotIn(next_gameweek, item["display_matches"])

    def test_public_preview_today_never_mixes_tomorrow_from_the_same_gameweek(self):
        competition, stage, teams = self.make_league(4)
        midday = timezone.localtime(timezone.now()).replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        live = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=6, status="LIVE", match_date=midday,
        )
        same_gameweek_upcoming = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=6, match_date=midday + timedelta(days=1),
        )
        other_gameweek = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[2],
            gameweek=7, match_date=midday + timedelta(days=7),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Live Matches · GW6")
        self.assertEqual(item["display_matches"], [live])
        self.assertNotIn(other_gameweek, item["display_matches"])

        Match.objects.filter(pk__in=[live.pk, same_gameweek_upcoming.pk, other_gameweek.pk]).update(
            status="FINISHED"
        )
        latest = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[1], away_team=teams[3],
            gameweek=8, status="FINISHED", match_date=midday - timedelta(days=1),
        )
        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Today's Results · GW6")
        self.assertEqual(item["display_matches"], [live])

    def test_public_preview_uses_unassigned_fixtures_only_without_numeric_gameweeks(self):
        competition, stage, teams = self.make_league(2)
        unassigned = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=None, match_date=None,
        )
        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Fixtures To Be Scheduled")
        self.assertEqual(item["display_matches"], [unassigned])

    def test_public_preview_excludes_past_dated_upcoming_fixtures(self):
        competition, stage, teams = self.make_league(4)
        stale = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=1, match_date=timezone.now() - timedelta(days=6),
        )
        current = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=2, match_date=timezone.now() + timedelta(days=1),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Upcoming Fixtures · GW2")
        self.assertEqual(item["display_matches"], [current])
        self.assertNotIn(stale, item["display_matches"])

    def test_public_preview_orders_today_live_upcoming_and_results(self):
        competition, stage, teams = self.make_league(4)
        midday = timezone.localtime(timezone.now()).replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        live = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=5, status="LIVE", match_date=midday,
        )
        upcoming = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=5, match_date=midday + timedelta(hours=2),
        )
        result = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[2],
            gameweek=5, status="FINISHED", match_date=midday - timedelta(hours=2),
        )
        Match.objects.create(
            competition=competition, stage=stage, home_team=teams[1], away_team=teams[3],
            gameweek=6, match_date=midday + timedelta(days=4),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Today's Fixtures & Results · GW5")
        self.assertEqual(item["display_matches"], [live, upcoming, result])

    def test_public_card_selection_is_identical_for_each_football_series(self):
        midday = timezone.localtime(timezone.now()).replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        region = Region.objects.create(name="Card Test Region", slug="card-test-region")
        series_types = (
            CompetitionSeries.LeagueType.REGIONAL,
            CompetitionSeries.LeagueType.PLE,
            CompetitionSeries.LeagueType.EWFA,
            CompetitionSeries.LeagueType.ZONAL,
        )

        for number, league_type in enumerate(series_types, start=1):
            series = CompetitionSeries.objects.create(
                name=f"{league_type} Card Series",
                slug=f"{league_type.lower()}-card-series",
                league_type=league_type,
                region=region if league_type == CompetitionSeries.LeagueType.REGIONAL else None,
            )
            competition = Competition.objects.create(
                name=f"{league_type} Card League",
                slug=f"{league_type.lower()}-card-league",
                season="2026/27",
                series=series,
                format_type="TABLE",
            )
            stage = Stage.objects.create(
                competition=competition,
                name="League",
                stage_type="LEAGUE",
                order=1,
            )
            home = Team.objects.create(name=f"{league_type} Home", competition=competition)
            away = Team.objects.create(name=f"{league_type} Away", competition=competition)
            live = Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=home,
                away_team=away,
                gameweek=number,
                status="LIVE",
                match_date=midday,
            )

            with self.subTest(league_type=league_type):
                item = _public_league_cards(
                    _public_competitions(Competition.objects.filter(pk=competition.pk))
                )[0]
                self.assertEqual(item["display_matches"], [live])
                self.assertEqual(item["display_label"], f"Live Matches · GW{number}")

    def test_public_preview_shows_every_current_day_match_across_gameweeks(self):
        competition, stage, teams = self.make_league(6)
        midday = timezone.localtime(timezone.now()).replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        pairs = [
            (home, away)
            for home in teams
            for away in teams
            if home != away
        ]
        statuses = ["LIVE"] * 3 + ["UPCOMING"] * 3 + ["FINISHED"] * 3
        today_matches = []
        for number, ((home, away), status) in enumerate(zip(pairs, statuses)):
            today_matches.append(Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=home,
                away_team=away,
                gameweek=4 + (number % 2),
                status=status,
                match_date=midday + timedelta(minutes=number),
            ))
        tomorrow = Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[5],
            away_team=teams[0],
            gameweek=4,
            status="UPCOMING",
            match_date=midday + timedelta(days=1),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(len(item["display_matches"]), 9)
        self.assertNotIn(tomorrow, item["display_matches"])
        self.assertEqual(
            [match.status for match in item["display_matches"]],
            statuses,
        )
        self.assertTrue(item["display_label"].startswith("Today's Fixtures & Results"))
        self.assertIn("GW4 & GW5", item["display_label"])

    def test_public_preview_keeps_today_result_when_future_context_is_long(self):
        competition, stage, teams = self.make_league(6)
        midday = timezone.localtime(timezone.now()).replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        result = Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[0],
            away_team=teams[1],
            gameweek=2,
            status="FINISHED",
            match_date=midday,
        )
        pairs = [
            (home, away)
            for home in teams
            for away in teams
            if home != away and {home, away} != {teams[0], teams[1]}
        ]
        for number, (home, away) in enumerate(pairs[:10]):
            Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=home,
                away_team=away,
                gameweek=2,
                status="UPCOMING",
                match_date=midday + timedelta(days=1, minutes=number),
                leg=number + 2,
            )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Today's Results · GW2")
        self.assertEqual(item["display_matches"], [result])

    def test_public_preview_caps_future_and_latest_result_fallbacks_at_eight(self):
        competition, stage, teams = self.make_league(6)
        midday = timezone.localtime(timezone.now()).replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        pairs = [
            (home, away)
            for home in teams
            for away in teams
            if home != away
        ]
        future = []
        for number, (home, away) in enumerate(pairs[:9]):
            future.append(Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=home,
                away_team=away,
                gameweek=7,
                status="UPCOMING",
                match_date=midday + timedelta(days=1, minutes=number),
            ))
        overdue = Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[5],
            away_team=teams[0],
            gameweek=1,
            status="UPCOMING",
            match_date=midday - timedelta(days=1),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Upcoming Fixtures · GW7")
        self.assertEqual(item["display_matches"], future[:8])
        self.assertNotIn(overdue, item["display_matches"])

        Match.objects.filter(pk__in=[match.pk for match in future]).update(
            status="FINISHED",
            match_date=midday - timedelta(days=2),
        )
        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Latest Results · GW7")
        self.assertEqual(len(item["display_matches"]), 8)
        self.assertNotIn(overdue, item["display_matches"])

    def test_public_preview_keeps_next_day_fixtures_in_the_selected_gameweek(self):
        competition, stage, teams = self.make_league(4)
        tomorrow = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=5, match_date=timezone.now() + timedelta(days=1),
        )
        same_weekend_context = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=5, match_date=timezone.now() + timedelta(days=2),
        )
        distant = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[2],
            gameweek=6, match_date=timezone.now() + timedelta(days=8),
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Upcoming Fixtures · GW5")
        self.assertEqual(item["display_matches"], [tomorrow, same_weekend_context])
        self.assertNotIn(distant, item["display_matches"])

    def test_public_preview_uses_nearest_dated_gameweek_before_tba(self):
        competition, stage, teams = self.make_league(4)
        dated = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=6, match_date=timezone.now() + timedelta(days=7),
        )
        Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=7, match_date=None,
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Upcoming Fixtures · GW6")
        self.assertEqual(item["display_matches"], [dated])

    def test_public_preview_uses_latest_results_before_tba_fixtures(self):
        competition, stage, teams = self.make_league(4)
        latest_result = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=5, status="FINISHED", match_date=timezone.now() - timedelta(days=1),
        )
        Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=6, match_date=None,
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Latest Results · GW5")
        self.assertEqual(item["display_matches"], [latest_result])

    def test_public_preview_uses_tba_only_as_a_final_fallback(self):
        competition, stage, teams = self.make_league(4)
        earliest = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=4, match_date=None,
        )
        later = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=5, match_date=None,
        )

        item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(item["display_label"], "Fixtures To Be Scheduled · GW4")
        self.assertEqual(item["display_matches"], [earliest])
        self.assertNotIn(later, item["display_matches"])

    def test_rescheduled_fixture_becomes_eligible_for_the_compact_preview(self):
        competition, stage, teams = self.make_league(4)
        postponed = Match.objects.create(
            competition=competition, stage=stage, home_team=teams[0], away_team=teams[1],
            gameweek=1, match_date=timezone.now() - timedelta(days=4),
        )
        Match.objects.create(
            competition=competition, stage=stage, home_team=teams[2], away_team=teams[3],
            gameweek=2, match_date=None,
        )

        first_item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertNotIn(postponed, first_item["display_matches"])

        postponed.match_date = timezone.now() + timedelta(days=1)
        postponed.save(update_fields=["match_date"])
        second_item = _public_league_cards(
            _public_competitions(Competition.objects.filter(pk=competition.pk))
        )[0]
        self.assertEqual(second_item["display_label"], "Upcoming Fixtures · GW1")
        self.assertEqual(second_item["display_matches"], [postponed])

    def test_public_preview_uses_the_prefetched_matches_without_n_plus_one_queries(self):
        for number, team_count in enumerate((2, 3, 4)):
            competition, stage, teams = self.make_league(team_count)
            Match.objects.create(
                competition=competition,
                stage=stage,
                home_team=teams[0],
                away_team=teams[1],
                gameweek=number + 1,
                match_date=timezone.now() + timedelta(days=number + 1),
            )

        with CaptureQueriesContext(connection) as queries:
            cards = _public_league_cards(
                _public_competitions(Competition.objects.all())
            )

        self.assertEqual(len(cards), 3)
        self.assertLessEqual(len(queries), 2)

    def test_public_fixture_without_location_has_no_empty_location_element(self):
        competition, stage, teams = self.make_league(2)
        Match.objects.create(
            competition=competition,
            stage=stage,
            home_team=teams[0],
            away_team=teams[1],
            match_date=timezone.now() + timedelta(days=2),
            location="",
        )
        response = self.client.get(reverse("league_detail", args=[competition.slug]))
        self.assertNotContains(response, 'class="fixture-location"')


class TableTeamProfileTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(
            name="Profile League", slug="profile-league", season="2026/27", format_type="TABLE"
        )
        self.stage = Stage.objects.create(
            competition=self.competition, name="League", stage_type="LEAGUE", order=1
        )
        self.home = Team.objects.create(name="Home", competition=self.competition)
        self.away = Team.objects.create(name="Away", competition=self.competition)
        self.other = Team.objects.create(name="Other", competition=self.competition)
        self.standing = Standing.objects.create(
            competition=self.competition, stage=self.stage, team=self.home,
            played=14, won=8, drawn=3, lost=3, goals_for=22, goals_against=12, points=27,
        )

    def finished(self, home, away, home_score, away_score, days):
        return Match.objects.create(
            competition=self.competition, stage=self.stage, home_team=home, away_team=away,
            home_score=home_score, away_score=away_score, status="FINISHED",
            match_date=timezone.now() + timedelta(days=days),
            leg=days + 1,
        )

    def test_profile_is_competition_scoped_and_shows_derived_data(self):
        matches = [
            self.finished(self.home, self.away, 1, 0, index)
            for index in range(6)
        ]
        self.finished(self.home, self.other, 0, 0, 10)
        Match.objects.create(
            competition=self.competition, stage=self.stage, home_team=self.other, away_team=self.home,
            status="LIVE", match_date=timezone.now() + timedelta(days=20),
        )
        Standing.objects.filter(
            competition=self.competition,
            stage=self.stage,
            team=self.home,
        ).delete()
        Standing.objects.create(
            competition=self.competition,
            stage=self.stage,
            team=self.home,
            played=14,
            won=8,
            drawn=3,
            lost=3,
            goals_for=22,
            goals_against=12,
            points=27,
        )
        Player.objects.create(name="Season Player", team=self.home, jersey_number=9)
        response = self.client.get(reverse("team_profile", args=[self.competition.slug, self.home.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "57.1%")
        self.assertContains(response, "Season Player")
        self.assertContains(response, "W")
        self.assertEqual(
            recent_form_by_team(matches, [self.home.id])[self.home.id], ["W"] * 5
        )

        other_competition = Competition.objects.create(
            name="Other League", slug="other-profile-league", season="2026/27", format_type="TABLE"
        )
        self.assertEqual(
            self.client.get(reverse("team_profile", args=[other_competition.slug, self.home.id])).status_code,
            404,
        )


class FootballUsabilityContinuationTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(
            name="Match Link League", slug="match-link-league", season="2026/27", format_type="TABLE"
        )
        self.stage = Stage.objects.create(
            competition=self.competition, name="League", stage_type="LEAGUE", order=1
        )
        self.home = Team.objects.create(name="Home Club", competition=self.competition)
        self.away = Team.objects.create(name="Away Club", competition=self.competition)
        self.match = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            match_date=timezone.now(),
        )

    def test_match_detail_uses_competition_scoped_links_and_logo_fallback(self):
        self.home.logo = "teams/home-club.png"
        self.home.save(update_fields=["logo"])
        response = self.client.get(reverse("match_detail", args=[self.match.id]))
        self.assertContains(
            response,
            reverse("team_profile", args=[self.competition.slug, self.home.id]),
        )
        self.assertContains(
            response,
            reverse("team_profile", args=[self.competition.slug, self.away.id]),
        )
        self.assertContains(response, "home-club.png")
        self.assertContains(response, ">A</span>")

    def test_archived_match_detail_remains_season_scoped(self):
        self.competition.is_archived = True
        self.competition.save(update_fields=["is_archived"])
        response = self.client.get(reverse("match_detail", args=[self.match.id]))
        self.assertContains(
            response,
            reverse("team_profile", args=[self.competition.slug, self.home.id]),
        )

    def test_league_detail_tabs_are_local_and_seasons_still_navigate(self):
        self.match.gameweek = 1
        self.match.save(update_fields=["gameweek"])
        archived = Competition.objects.create(
            name=self.competition.name,
            slug="match-link-league-archived",
            season="2025/26",
            series=self.competition.series,
            format_type="TABLE",
            is_archived=True,
        )

        response = self.client.get(reverse("league_detail", args=[self.competition.slug]))

        for tab in ("overview", "fixtures", "results", "standings", "stats"):
            self.assertContains(response, f'href="#tab-{tab}"')
            self.assertContains(response, f'id="tab-{tab}"')
        self.assertContains(response, "Gameweek 1")
        self.assertContains(
            response,
            reverse("team_profile", args=[self.competition.slug, self.home.id]),
        )
        self.assertContains(response, "Form")
        self.assertContains(
            response,
            f'value="{reverse("league_detail", args=[archived.slug])}"',
        )
        self.assertContains(response, "event.preventDefault()")
        self.assertNotContains(response, "window.location.assign(targetLink.href)")
        self.assertNotContains(
            response,
            reverse("league_fixtures", args=[self.competition.slug]),
        )

        archived_response = self.client.get(reverse("league_detail", args=[archived.slug]))
        self.assertEqual(archived_response.status_code, 200)
        for tab in ("overview", "fixtures", "results", "standings", "stats"):
            self.assertContains(archived_response, f'id="tab-{tab}"')

        for route_name in (
            "league_fixtures",
            "league_results",
            "league_standings",
            "league_stats",
        ):
            self.assertEqual(
                self.client.get(reverse(route_name, args=[self.competition.slug])).status_code,
                200,
            )

    def test_manager_fixture_matchup_is_an_edit_link_without_removing_button(self):
        user = User.objects.create_user(username="fixture-link-manager", password="pass")
        LeagueMembership.objects.create(
            user=user,
            competition=self.competition,
            role=LeagueMembership.Role.ADMIN,
            is_active=True,
        )
        fixture_url = reverse(
            "league_manager:fixture_update", args=[self.competition.slug, self.match.id]
        )
        self.assertEqual(
            self.client.get(reverse("league_manager:fixture_list", args=[self.competition.slug])).status_code,
            302,
        )
        self.client.force_login(user)
        response = self.client.get(reverse("league_manager:fixture_list", args=[self.competition.slug]))
        self.assertContains(response, fixture_url, count=2)
        self.assertContains(response, "Edit")

    def test_current_leagues_are_grouped_by_active_region_order(self):
        first_region = Region.objects.create(name="First", slug="first", display_order=20)
        second_region = Region.objects.create(name="Second", slug="second", display_order=10)
        first = Competition.objects.create(
            name="Zulu League", slug="display-zulu", season="2026", display_order=30
        )
        first.series.region = first_region
        first.series.league_type = CompetitionSeries.LeagueType.REGIONAL
        first.series.save(update_fields=["region", "league_type"])
        second = Competition.objects.create(
            name="Bravo League", slug="display-bravo", season="2026", display_order=10
        )
        second.series.region = second_region
        second.series.league_type = CompetitionSeries.LeagueType.REGIONAL
        second.series.save(update_fields=["region", "league_type"])
        third = Competition.objects.create(
            name="Alpha League", slug="display-alpha", season="2026", display_order=20
        )
        equal_a = Competition.objects.create(
            name="Equal Alpha", slug="display-equal-a", season="2026", display_order=50
        )
        equal_b = Competition.objects.create(
            name="Equal Bravo", slug="display-equal-b", season="2026", display_order=50
        )
        Competition.objects.create(
            name="Archived First", slug="display-archived", season="2025", display_order=1,
            is_archived=True,
        )
        response = self.client.get(reverse("league_list"))
        regions = list(response.context["regions"])
        self.assertEqual([region.id for region in regions], [second_region.id, first_region.id])
