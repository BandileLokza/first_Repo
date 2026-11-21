"""HTTP and read-model coverage for player-attributed team rack scoring."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import PoolRackCorrectionForm
from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamRoster,
)
from .queries import (
    player_rack_win_statistics,
    team_entry_rack_statistics,
)
from .services import correct_rack, record_rack, start_match


User = get_user_model()


class TeamRackUiStatisticsTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="team-rack-ui-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, *, published=True, name=None):
        self._competition_number += 1
        number = self._competition_number
        competition = PoolCompetition.objects.create(
            name=name or f"Team Rack Competition {number}",
            slug=f"team-rack-competition-{number}",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=(
                PoolCompetition.Visibility.PUBLISHED
                if published
                else PoolCompetition.Visibility.DRAFT
            ),
            default_race_target=2,
        )
        return competition, competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )

    def make_team_entry(self, competition, name):
        team = PoolTeam.objects.create(name=name)
        return team, PoolCompetitionEntry.objects.create(competition=competition, team=team)

    def make_match(self, competition, stage, home_entry, away_entry):
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )

    def add_roster_player(self, entry, name, *, active=True):
        player = PoolPlayer.objects.create(full_name=name)
        PoolTeamRoster.objects.create(
            competition_entry=entry,
            player=player,
            is_active=active,
        )
        return player

    def start_legacy_team_match(self, pool_match):
        """Model an already-live Phase Two team fixture with no score sheet.

        New fixtures use the lineup-first team operations service.  These
        historical attribution/UI assertions deliberately cover the supported
        generic fallback for pre-existing live team matches, whose actual
        racks must remain recordable without fabricating 25 schedule rows.
        """

        pool_match.status = PoolMatch.Status.LIVE
        pool_match.save()
        pool_match.refresh_from_db()

    def make_scoring_match(self, *, published=True):
        competition, stage = self.make_competition(published=published)
        home_team, home_entry = self.make_team_entry(competition, "Give Thanks")
        away_team, away_entry = self.make_team_entry(competition, "Reps")
        home_player_one = self.add_roster_player(home_entry, "Tycoon")
        home_player_two = self.add_roster_player(home_entry, "Piyane")
        away_player = self.add_roster_player(away_entry, "Siya")
        pool_match = self.make_match(competition, stage, home_entry, away_entry)
        return {
            "competition": competition,
            "stage": stage,
            "home_team": home_team,
            "home_entry": home_entry,
            "away_team": away_team,
            "away_entry": away_entry,
            "home_player_one": home_player_one,
            "home_player_two": home_player_two,
            "away_player": away_player,
            "match": pool_match,
        }

    def test_team_manager_uses_player_first_choices_and_infers_the_team_entry(self):
        data = self.make_scoring_match(published=False)
        self.start_legacy_team_match(data["match"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse(
                "pool:manage_match",
                args=[data["competition"].slug, data["match"].pk],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, data["home_team"].name)
        self.assertContains(response, data["away_team"].name)
        self.assertContains(response, data["home_player_one"].full_name)
        self.assertContains(response, data["away_player"].full_name)
        self.assertContains(response, 'name="winning_player"')
        self.assertNotIn("winner", response.context["rack_form"].fields)

        response = self.client.post(
            reverse(
                "pool:manage_match_record_rack",
                args=[data["competition"].slug, data["match"].pk],
            ),
            {
                "winning_player": data["home_player_one"].pk,
                "outcome_type": PoolRack.OutcomeType.GREEN_APPLE,
                "breaker": "",
                "reason": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        rack = PoolRack.objects.get(match=data["match"], rack_number=1)
        data["match"].refresh_from_db()
        self.assertEqual(rack.winner_id, data["home_entry"].pk)
        self.assertEqual(rack.winning_player_id, data["home_player_one"].pk)
        self.assertEqual(rack.outcome_type, PoolRack.OutcomeType.GREEN_APPLE)
        self.assertEqual((data["match"].home_score, data["match"].away_score), (1, 0))

    def test_team_manager_rejects_an_outside_or_missing_player_and_warns_for_empty_rosters(self):
        data = self.make_scoring_match(published=False)
        third_team, third_entry = self.make_team_entry(data["competition"], "Outsiders")
        third_player = self.add_roster_player(third_entry, "Not In This Match")
        self.start_legacy_team_match(data["match"])
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse(
                "pool:manage_match_record_rack",
                args=[data["competition"].slug, data["match"].pk],
            ),
            {
                "winning_player": third_player.pk,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "breaker": "",
                "reason": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PoolRack.objects.filter(match=data["match"]).exists())

        empty_competition, stage = self.make_competition(published=False)
        _, empty_home = self.make_team_entry(empty_competition, "No Roster Home")
        _, empty_away = self.make_team_entry(empty_competition, "No Roster Away")
        empty_match = self.make_match(empty_competition, stage, empty_home, empty_away)
        self.start_legacy_team_match(empty_match)
        response = self.client.get(
            reverse(
                "pool:manage_match",
                args=[empty_competition.slug, empty_match.pk],
            )
        )
        self.assertContains(response, "Team roster required")
        self.assertContains(response, "Manage No Roster Home roster")
        self.assertNotContains(response, "Record rack")

    def test_correction_moves_player_outcome_statistics_without_changing_team_score(self):
        data = self.make_scoring_match()
        self.start_legacy_team_match(data["match"])
        rack = record_rack(
            data["match"],
            winning_player=data["home_player_one"],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        data["match"].refresh_from_db()

        correct_rack(
            data["match"],
            rack,
            winning_player=data["home_player_two"],
            outcome_type=PoolRack.OutcomeType.NORMAL,
            recorded_by=self.owner,
            reason="Correct player attribution",
        )
        data["match"].refresh_from_db()
        rack.refresh_from_db()

        statistics = team_entry_rack_statistics(data["home_entry"])
        by_player_id = {
            row["player"].pk: row for row in statistics["player_rows"]
        }
        self.assertEqual((data["match"].home_score, data["match"].away_score), (1, 0))
        self.assertEqual(rack.winner_id, data["home_entry"].pk)
        self.assertEqual(rack.winning_player_id, data["home_player_two"].pk)
        self.assertEqual(by_player_id[data["home_player_one"].pk]["racks_won"], 0)
        self.assertEqual(by_player_id[data["home_player_one"].pk]["green_apples"], 0)
        self.assertEqual(by_player_id[data["home_player_two"].pk]["racks_won"], 1)
        self.assertEqual(by_player_id[data["home_player_two"].pk]["normal_wins"], 1)

    def test_correction_can_preserve_an_old_player_after_their_roster_is_deactivated(self):
        data = self.make_scoring_match()
        self.start_legacy_team_match(data["match"])
        rack = record_rack(
            data["match"],
            winning_player=data["home_player_one"],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        PoolTeamRoster.objects.filter(
            competition_entry=data["home_entry"],
            player=data["home_player_one"],
        ).update(is_active=False)

        form = PoolRackCorrectionForm(
            {
                "winning_player": data["home_player_one"].pk,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "breaker": "",
                "reason": "Correct outcome after roster change.",
            },
            match=data["match"],
            preserved_winning_player=data["home_player_one"],
            preserved_winner_entry=data["home_entry"],
            require_complete_team_rosters=False,
            initial={"winning_player": data["home_player_one"].pk},
        )
        self.assertTrue(form.is_valid())
        self.assertTrue(
            any(
                group.get("label") == "Previously recorded attribution"
                for group in form.roster_groups
            )
        )

        corrected = correct_rack(
            data["match"],
            rack,
            winning_player=form.cleaned_data["winning_player"],
            outcome_type=form.cleaned_data["outcome_type"],
            breaker=form.cleaned_data["breaker"],
            recorded_by=self.owner,
            reason=form.cleaned_data["reason"],
        )
        self.assertEqual(corrected.winner_id, data["home_entry"].pk)
        self.assertEqual(corrected.winning_player_id, data["home_player_one"].pk)
        self.assertEqual(corrected.outcome_type, PoolRack.OutcomeType.NORMAL)

    def test_public_team_player_statistics_are_scoped_and_legacy_nulls_stay_unattributed(self):
        data = self.make_scoring_match()
        self.start_legacy_team_match(data["match"])
        first_rack = record_rack(
            data["match"],
            winning_player=data["home_player_one"],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        record_rack(
            data["match"],
            winning_player=data["home_player_two"],
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )

        team_statistics = team_entry_rack_statistics(data["home_entry"])
        self.assertEqual(team_statistics["racks_won"], 2)
        self.assertEqual(team_statistics["attributed_racks_won"], 2)
        self.assertEqual(team_statistics["unattributed_racks_won"], 0)

        team_response = self.client.get(
            reverse("pool:team_detail", args=[data["home_team"].pk]),
            {"competition": data["competition"].slug},
        )
        self.assertEqual(team_response.status_code, 200)
        self.assertContains(team_response, "Team Racks")
        self.assertNotContains(team_response, "Player-attributed racks")
        self.assertContains(
            team_response,
            f"team={data['home_entry'].pk}",
        )

        player_response = self.client.get(
            reverse("pool:player_detail", args=[data["home_player_one"].public_id]),
            {
                "competition": data["competition"].slug,
                "team": data["home_entry"].pk,
            },
        )
        self.assertEqual(player_response.status_code, 200)
        self.assertContains(player_response, "Representing Give Thanks")
        self.assertContains(player_response, "Green apples")

        stats_response = self.client.get(
            reverse("pool:competition_detail", args=[data["competition"].slug]),
            {"tab": "stats"},
        )
        self.assertEqual(stats_response.status_code, 200)
        self.assertContains(stats_response, "Racks Lost")
        self.assertNotContains(stats_response, "Player-attributed")
        self.assertContains(stats_response, data["home_team"].name)

        # Simulate a row created before this nullable attribution field existed.
        PoolRack.objects.filter(pk=first_rack.pk).update(winning_player=None)
        first_rack.refresh_from_db()
        legacy_correction_form = PoolRackCorrectionForm(
            {
                "winning_player": "",
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "breaker": "",
                "reason": "Correct the historical outcome only.",
            },
            match=data["match"],
            preserved_winning_player=None,
            require_complete_team_rosters=False,
        )
        self.assertTrue(legacy_correction_form.is_valid())
        self.assertIsNone(legacy_correction_form.cleaned_data["winning_player"])
        team_statistics = team_entry_rack_statistics(data["home_entry"])
        self.assertEqual(team_statistics["racks_won"], 2)
        self.assertEqual(team_statistics["attributed_racks_won"], 1)
        self.assertEqual(team_statistics["unattributed_racks_won"], 1)
        match_response = self.client.get(
            reverse("pool:match_detail", args=[data["match"].pk])
        )
        self.assertContains(match_response, "Player not recorded")

    def test_player_and_team_statistics_do_not_cross_competition_scope(self):
        first = self.make_scoring_match()
        self.start_legacy_team_match(first["match"])
        record_rack(
            first["match"],
            winning_player=first["home_player_one"],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )

        second_competition, second_stage = self.make_competition()
        second_home_entry = PoolCompetitionEntry.objects.create(
            competition=second_competition,
            team=first["home_team"],
        )
        _, second_away_entry = self.make_team_entry(second_competition, "Second Reps")
        PoolTeamRoster.objects.create(
            competition_entry=second_home_entry,
            player=first["home_player_one"],
        )
        self.add_roster_player(second_away_entry, "Second Siya")
        second_match = self.make_match(
            second_competition,
            second_stage,
            second_home_entry,
            second_away_entry,
        )
        self.start_legacy_team_match(second_match)
        record_rack(
            second_match,
            winning_player=first["home_player_one"],
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )

        first_player_stats = player_rack_win_statistics(
            first["home_player_one"],
            competition=first["competition"],
            team_entry=first["home_entry"],
        )
        second_player_stats = player_rack_win_statistics(
            first["home_player_one"],
            competition=second_competition,
            team_entry=second_home_entry,
        )
        self.assertEqual(first_player_stats["racks_won"], 1)
        self.assertEqual(first_player_stats["green_apples"], 1)
        self.assertEqual(first_player_stats["one_visits"], 0)
        self.assertEqual(second_player_stats["racks_won"], 1)
        self.assertEqual(second_player_stats["green_apples"], 0)
        self.assertEqual(second_player_stats["one_visits"], 1)

        # A player cannot use a team entry from another competition as context.
        response = self.client.get(
            reverse("pool:player_detail", args=[first["home_player_one"].public_id]),
            {
                "competition": first["competition"].slug,
                "team": second_home_entry.pk,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_singles_stats_link_opens_the_competition_scoped_rack_view(self):
        competition = PoolCompetition.objects.create(
            name="Singles Attribution Competition",
            slug="singles-attribution-competition",
            owner=self.owner,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=2,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home_player = PoolPlayer.objects.create(full_name="Singles Home")
        away_player = PoolPlayer.objects.create(full_name="Singles Away")
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=home_player,
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=away_player,
        )
        pool_match = self.make_match(competition, stage, home_entry, away_entry)
        start_match(pool_match, recorded_by=self.owner)
        record_rack(
            pool_match,
            winner=home_entry,
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )

        stats_response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]),
            {"tab": "stats"},
        )
        self.assertEqual(stats_response.status_code, 200)
        self.assertContains(
            stats_response,
            f"?competition={competition.slug}",
        )
        player_response = self.client.get(
            reverse("pool:player_detail", args=[home_player.public_id]),
            {"competition": competition.slug},
        )
        self.assertEqual(player_response.status_code, 200)
        self.assertContains(player_response, "Singles Attribution Competition")
        self.assertContains(player_response, "One visits")

    def test_draft_team_rack_statistics_do_not_become_public(self):
        data = self.make_scoring_match(published=False)
        self.start_legacy_team_match(data["match"])
        record_rack(
            data["match"],
            winning_player=data["home_player_one"],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )

        self.assertEqual(
            self.client.get(
                reverse("pool:competition_detail", args=[data["competition"].slug]),
                {"tab": "stats"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("pool:player_detail", args=[data["home_player_one"].public_id]),
                {
                    "competition": data["competition"].slug,
                    "team": data["home_entry"].pk,
                },
            ).status_code,
            404,
        )
        team_response = self.client.get(
            reverse("pool:team_detail", args=[data["home_team"].pk]),
            {"competition": data["competition"].slug},
        )
        self.assertEqual(team_response.status_code, 200)
        self.assertContains(team_response, "No published competition rack statistics")

        # A manager can preview the draft fixture, but its attributed-player
        # name must not point to the public, published-only scoped profile
        # endpoint.  That keeps the preview useful without a dead link or a
        # draft-statistics visibility bypass.
        self.client.force_login(self.owner)
        match_response = self.client.get(
            reverse("pool:match_detail", args=[data["match"].pk])
        )
        self.assertEqual(match_response.status_code, 200)
        self.assertContains(match_response, data["home_player_one"].full_name)
        self.assertNotContains(
            match_response,
            f"?competition={data['competition'].slug}&amp;team={data['home_entry'].pk}",
        )
