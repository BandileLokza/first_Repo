"""Coverage for manager-entered offline Pool final results."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolMatchScorerAssignment,
    PoolMatchResultRevision,
    PoolManualResultRackSummary,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamRoster,
)
from .queries import competition_statistics, competition_team_rack_statistics
from .services import PoolResultValidationError, record_manual_final_result, record_rack, start_match
from .standings import standings_for_stage


User = get_user_model()


class ManualFinalResultTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("manual-result-owner", password="test-password")
        self.other_user = User.objects.create_user("manual-result-other", password="test-password")
        self.competition = PoolCompetition.objects.create(
            name="Manual results league",
            slug="manual-results-league",
            owner=self.owner,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=5,
        )
        self.stage = self.competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        self.home = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            player=PoolPlayer.objects.create(full_name="Offline Home"),
        )
        self.away = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            player=PoolPlayer.objects.create(full_name="Offline Away"),
        )

    def make_match(self):
        return PoolMatch.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_entry=self.home,
            away_entry=self.away,
            race_target=5,
        )


class ManualFinalResultServiceTests(ManualFinalResultTestCase):
    def test_records_aggregate_played_result_without_racks_and_updates_standings(self):
        pool_match = self.make_match()

        result = record_manual_final_result(
            pool_match,
            home_score=5,
            away_score=3,
            recorded_by=self.owner,
            reason="Verified paper scoresheet",
        )

        self.assertEqual(result.status, PoolMatch.Status.FINISHED)
        self.assertEqual(result.winner_id, self.home.pk)
        self.assertEqual(result.result_method, PoolMatch.ResultMethod.PLAYED)
        self.assertEqual(
            result.result_entry_method,
            PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT,
        )
        self.assertFalse(PoolRack.objects.filter(match=result).exists())
        rows = {row.entry_id: row for row in standings_for_stage(self.stage)}
        self.assertEqual((rows[self.home.pk].played, rows[self.home.pk].won), (1, 1))
        self.assertEqual((rows[self.home.pk].racks_for, rows[self.home.pk].racks_against), (5, 3))
        self.assertEqual(rows[self.home.pk].points, self.competition.points_for_win)
        self.assertTrue(PoolMatchResultRevision.objects.filter(
            match=result,
            action=PoolMatchResultRevision.Action.MANUAL_RESULT_ENTERED,
            revised_by=self.owner,
        ).exists())

    def test_manual_result_contributes_aggregate_statistics_but_no_special_events(self):
        pool_match = self.make_match()
        record_manual_final_result(pool_match, home_score=5, away_score=2)

        rows = {row["entry"].pk: row for row in competition_statistics(self.competition)}
        self.assertEqual((rows[self.home.pk]["racks_won"], rows[self.home.pk]["racks_lost"]), (5, 2))
        self.assertEqual((rows[self.away.pk]["racks_won"], rows[self.away.pk]["racks_lost"]), (2, 5))
        self.assertEqual(rows[self.home.pk]["green_apples"], 0)
        self.assertEqual(rows[self.home.pk]["one_visits"], 0)

    def test_manual_singles_outcomes_are_classified_without_fake_racks(self):
        pool_match = self.make_match()
        record_manual_final_result(
            pool_match,
            home_score=5,
            away_score=2,
            home_outcomes={"green_apples": 1, "red_apples": 1, "one_visits": 0},
            away_outcomes={"green_apples": 0, "red_apples": 0, "one_visits": 1},
        )
        rows = {row["entry"].pk: row for row in competition_statistics(self.competition)}
        self.assertEqual(
            (rows[self.home.pk]["normal_wins"], rows[self.home.pk]["green_apples"], rows[self.home.pk]["red_apples"]),
            (3, 1, 1),
        )
        self.assertEqual((rows[self.away.pk]["normal_wins"], rows[self.away.pk]["one_visits"]), (1, 1))
        self.assertEqual(PoolManualResultRackSummary.objects.filter(match=pool_match).count(), 2)
        self.assertFalse(PoolRack.objects.filter(match=pool_match).exists())

    def test_special_outcomes_cannot_exceed_manual_final_score(self):
        with self.assertRaises(PoolResultValidationError):
            record_manual_final_result(
                self.make_match(),
                home_score=5,
                away_score=1,
                home_outcomes={"green_apples": 3, "red_apples": 3},
            )

    def test_rejects_invalid_final_score_and_detailed_history(self):
        pool_match = self.make_match()
        with self.assertRaises(PoolResultValidationError):
            record_manual_final_result(pool_match, home_score=4, away_score=3)
        with self.assertRaises(PoolResultValidationError):
            record_manual_final_result(pool_match, home_score=5, away_score=5)

        start_match(pool_match)
        record_rack(pool_match, winner=self.home)
        with self.assertRaises(PoolResultValidationError):
            record_manual_final_result(pool_match, home_score=5, away_score=1)

    def test_existing_manual_result_can_be_corrected_with_a_new_revision(self):
        pool_match = self.make_match()
        record_manual_final_result(pool_match, home_score=5, away_score=4)
        result = record_manual_final_result(pool_match, home_score=2, away_score=5)

        self.assertEqual(result.winner_id, self.away.pk)
        self.assertEqual((result.home_score, result.away_score), (2, 5))
        self.assertEqual(PoolRack.objects.filter(match=result).count(), 0)
        self.assertTrue(PoolMatchResultRevision.objects.filter(
            match=result,
            action=PoolMatchResultRevision.Action.MANUAL_RESULT_CORRECTED,
        ).exists())

    def test_team_manual_result_keeps_player_rack_statistics_empty(self):
        competition = PoolCompetition.objects.create(
            name="Offline team league",
            slug="offline-team-league",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=5,
        )
        stage = competition.stages.get(stage_type=PoolCompetitionStage.StageType.LEAGUE)
        home = PoolCompetitionEntry.objects.create(
            competition=competition, team=PoolTeam.objects.create(name="Offline Team A")
        )
        away = PoolCompetitionEntry.objects.create(
            competition=competition, team=PoolTeam.objects.create(name="Offline Team B")
        )
        match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            race_target=5,
        )

        record_manual_final_result(
            match,
            home_score=5,
            away_score=4,
            home_outcomes={"green_apples": 2, "red_apples": 1, "one_visits": 1},
            away_outcomes={"green_apples": 1, "one_visits": 2},
        )

        team_rows = {row["entry"].pk: row for row in competition_team_rack_statistics(competition)}
        self.assertEqual(team_rows[home.pk]["racks_won"], 5)
        self.assertEqual(team_rows[away.pk]["racks_won"], 4)
        self.assertEqual((team_rows[home.pk]["team_racks"], team_rows[home.pk]["racks_lost"]), (9, 4))
        self.assertEqual((team_rows[home.pk]["normal_wins"], team_rows[home.pk]["green_apples"]), (1, 2))
        self.assertEqual(team_rows[away.pk]["one_visits"], 2)
        self.assertEqual(team_rows[home.pk]["player_rows"], [])

        self.client.force_login(self.owner)
        detail = self.client.get(reverse("pool:match_detail", args=[match.pk]))
        self.assertContains(detail, "Aggregate rack outcomes")
        self.assertContains(detail, "Offline Team A")
        self.assertContains(detail, "Green Apples")
        self.assertNotContains(detail, "Open live scorer")

        manage = self.client.get(
            reverse("pool:manage_match", args=[competition.slug, match.pk])
        )
        self.assertNotContains(manage, "Open scorer view")

    def test_manual_summary_update_replaces_current_outcome_totals(self):
        match = self.make_match()
        record_manual_final_result(
            match, home_score=5, away_score=2,
            home_outcomes={"green_apples": 2},
        )
        record_manual_final_result(
            match, home_score=5, away_score=3,
            home_outcomes={"red_apples": 2, "one_visits": 1},
        )
        summary = PoolManualResultRackSummary.objects.get(match=match, entry=self.home)
        self.assertEqual((summary.green_apples, summary.red_apples, summary.one_visits), (0, 2, 1))
        row = {item["entry"].pk: item for item in competition_statistics(self.competition)}[self.home.pk]
        self.assertEqual((row["normal_wins"], row["green_apples"], row["red_apples"], row["one_visits"]), (2, 0, 2, 1))


class ManualFinalResultManagerTests(ManualFinalResultTestCase):
    def test_manager_form_is_owner_only_and_public_match_labels_the_manual_result(self):
        pool_match = self.make_match()
        url = reverse(
            "pool:manage_match_manual_final_result",
            args=[self.competition.slug, pool_match.pk],
        )
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(self.owner)
        response = self.client.post(
            url,
            {
                "home_score": 5,
                "away_score": 1,
                "reason": "Club report",
                "confirm": "on",
            },
        )
        self.assertRedirects(
            response,
            reverse("pool:manage_competition_fixtures", args=[self.competition.slug]),
        )
        pool_match.refresh_from_db()
        self.assertContains(self.client.get(reverse("pool:match_detail", args=[pool_match.pk])), "Final result entered manually")

    def test_fixture_list_offers_manual_entry_and_update_actions(self):
        pool_match = self.make_match()
        self.client.force_login(self.owner)
        fixtures_url = reverse("pool:manage_competition_fixtures", args=[self.competition.slug])
        self.assertContains(self.client.get(fixtures_url), "Enter final result")
        record_manual_final_result(pool_match, home_score=5, away_score=0)
        self.assertContains(self.client.get(fixtures_url), "Update result")


class LiveScorerAvailabilityTests(ManualFinalResultTestCase):
    def _manager_url(self, pool_match):
        return reverse(
            "pool:manage_match",
            args=[pool_match.competition.slug, pool_match.pk],
        )

    def _finish_live_singles_match(self):
        pool_match = self.make_match()
        start_match(pool_match, recorded_by=self.owner)
        for _ in range(pool_match.race_target):
            record_rack(pool_match, winner=self.home, recorded_by=self.owner)
        pool_match.refresh_from_db()
        return pool_match

    def test_authorised_users_see_inline_scorer_for_upcoming_and_live_singles(self):
        pool_match = self.make_match()
        self.client.force_login(self.owner)

        self.assertContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Live scoring",
        )
        self.assertContains(self.client.get(self._manager_url(pool_match)), "Open scorer view")
        start_match(pool_match, recorded_by=self.owner)
        self.assertContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Record next rack",
        )
        self.assertContains(self.client.get(self._manager_url(pool_match)), "Open scorer view")

    def test_exact_scorer_uses_inline_controls_and_returns_to_match_detail(self):
        pool_match = self.make_match()
        pool_match.race_target = 1
        pool_match.save(update_fields=["race_target"])
        scorer = User.objects.create_user("inline-match-scorer", password="test-password")
        PoolMatchScorerAssignment.objects.create(
            match=pool_match,
            user=scorer,
            assigned_by=self.owner,
        )

        anonymous = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))
        self.assertNotContains(anonymous, "Live scoring")

        self.client.force_login(self.other_user)
        self.assertNotContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Live scoring",
        )

        self.client.force_login(scorer)
        detail_url = reverse("pool:match_detail", args=[pool_match.pk])
        detail = self.client.get(detail_url)
        self.assertContains(detail, "Live scoring")
        self.assertContains(detail, "Start match")
        self.assertContains(
            detail,
            'name="scoring_surface" value="match_detail"',
            html=False,
        )
        self.assertNotContains(detail, "Match scorers")
        self.assertNotContains(detail, "Non-played result")
        self.assertRedirects(
            self.client.get(reverse("pool:match_score", args=[pool_match.pk])),
            detail_url,
        )

        started = self.client.post(
            reverse("pool:manage_match_start", args=[self.competition.slug, pool_match.pk]),
            {"scoring_surface": "match_detail"},
            follow=False,
        )
        self.assertRedirects(started, detail_url)
        self.assertContains(self.client.get(detail_url), "Record next rack")

        recorded = self.client.post(
            reverse(
                "pool:manage_match_record_rack",
                args=[self.competition.slug, pool_match.pk],
            ),
            {
                "winner": self.home.pk,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "breaker": "",
                "reason": "",
                "scoring_surface": "match_detail",
            },
            follow=False,
        )
        self.assertRedirects(recorded, detail_url)

        self.assertNotContains(self.client.get(detail_url), "Live scoring")

        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(reverse("pool:match_score", args=[pool_match.pk])).status_code,
            403,
        )

    def test_exact_scorer_can_open_only_their_private_fixture(self):
        self.competition.visibility = PoolCompetition.Visibility.DRAFT
        self.competition.save(update_fields=["visibility"])
        assigned_match = self.make_match()
        other_match = self.make_match()
        scorer = User.objects.create_user("private-inline-scorer", password="test-password")
        PoolMatchScorerAssignment.objects.create(
            match=assigned_match,
            user=scorer,
            assigned_by=self.owner,
        )

        self.client.force_login(scorer)
        self.assertEqual(
            self.client.get(reverse("pool:match_detail", args=[assigned_match.pk])).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("pool:match_detail", args=[other_match.pk])).status_code,
            404,
        )

    def test_manager_scoring_from_match_detail_stays_on_match_detail(self):
        pool_match = self.make_match()
        detail_url = reverse("pool:match_detail", args=[pool_match.pk])
        self.client.force_login(self.owner)

        started = self.client.post(
            reverse("pool:manage_match_start", args=[self.competition.slug, pool_match.pk]),
            {"scoring_surface": "match_detail"},
            follow=False,
        )
        self.assertRedirects(started, detail_url)

        recorded = self.client.post(
            reverse("pool:manage_match_record_rack", args=[self.competition.slug, pool_match.pk]),
            {
                "winner": self.home.pk,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "breaker": "",
                "reason": "",
                "scoring_surface": "match_detail",
            },
            follow=False,
        )
        self.assertRedirects(recorded, detail_url)

        manager_match = self.make_match()
        manager_started = self.client.post(
            reverse(
                "pool:manage_match_start",
                args=[self.competition.slug, manager_match.pk],
            ),
            follow=False,
        )
        self.assertRedirects(manager_started, self._manager_url(manager_match))

    def test_manager_controls_and_scorer_assignment_are_available_inline(self):
        pool_match = self.make_match()
        detail_url = reverse("pool:match_detail", args=[pool_match.pk])
        scorer = User.objects.create_user("inline-manager-assignment", password="test-password")
        self.client.force_login(self.owner)

        detail = self.client.get(detail_url)
        self.assertContains(detail, "Match scorers")
        self.assertContains(detail, "Non-played result")
        self.assertContains(detail, "Fixture status")
        self.assertContains(detail, "Start match")

        assigned = self.client.post(
            reverse("pool:manage_match_scorer_add", args=[self.competition.slug, pool_match.pk]),
            {"identifier": scorer.username, "scoring_surface": "match_detail"},
            follow=False,
        )
        self.assertRedirects(assigned, detail_url)
        assignment = PoolMatchScorerAssignment.objects.get(match=pool_match, user=scorer)

        toggled = self.client.post(
            reverse(
                "pool:manage_match_scorer_toggle",
                args=[self.competition.slug, pool_match.pk, assignment.pk],
            ),
            {"scoring_surface": "match_detail"},
            follow=False,
        )
        self.assertRedirects(toggled, detail_url)

    def test_finished_live_scored_singles_hides_scorer_and_keeps_correction(self):
        pool_match = self._finish_live_singles_match()
        self.client.force_login(self.owner)

        self.assertNotContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Open live scorer",
        )
        manager = self.client.get(self._manager_url(pool_match))
        self.assertNotContains(manager, "Open scorer view")
        self.assertContains(manager, "Open correction mode")

    def test_finished_manual_singles_hides_scorer_and_rejects_direct_scoring(self):
        pool_match = self.make_match()
        record_manual_final_result(pool_match, home_score=5, away_score=1)
        self.client.force_login(self.owner)

        self.assertNotContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Open live scorer",
        )
        self.assertNotContains(self.client.get(self._manager_url(pool_match)), "Open scorer view")
        legacy = self.client.get(reverse("pool:match_score", args=[pool_match.pk]))
        self.assertRedirects(legacy, reverse("pool:match_detail", args=[pool_match.pk]))
        self.assertEqual(
            self.client.post(
                reverse(
                    "pool:manage_match_start",
                    args=[self.competition.slug, pool_match.pk],
                )
            ).status_code,
            404,
        )

    def test_finished_live_scored_team_hides_scorer(self):
        competition = PoolCompetition.objects.create(
            name="Completed live team league",
            slug="completed-live-team-league",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=2,
        )
        stage = competition.stages.get(stage_type=PoolCompetitionStage.StageType.LEAGUE)
        home = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Completed Live Team A"),
        )
        away = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Completed Live Team B"),
        )
        home_player = PoolPlayer.objects.create(full_name="Completed Live Player")
        PoolTeamRoster.objects.create(competition_entry=home, player=home_player)
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            race_target=2,
            status=PoolMatch.Status.LIVE,
        )
        record_rack(pool_match, winning_player=home_player, recorded_by=self.owner)
        record_rack(pool_match, winning_player=home_player, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)

        self.client.force_login(self.owner)
        self.assertNotContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Open live scorer",
        )
        self.assertNotContains(self.client.get(self._manager_url(pool_match)), "Open scorer view")
