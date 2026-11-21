"""HTTP coverage for the final-rack scorer redirect boundary.

The scoring services deliberately work on locked, freshly loaded match rows.
These tests exercise the manager endpoints so a stale view-level ``PoolMatch``
cannot accidentally send an operator back to the scorer after the service has
actually finished the fixture.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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
from .services import start_match
from .team_operations import (
    start_next_eligible_team_rack,
    submit_team_match_lineup,
)


User = get_user_model()


class ScoringRedirectTests(TestCase):
    """Only a rack that changes a published match to FINISHED goes public."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="scoring-redirect-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_singles_match(self, *, visibility, race_target):
        self._competition_number += 1
        competition = PoolCompetition.objects.create(
            name=f"Scoring redirect singles {self._competition_number}",
            slug=f"scoring-redirect-singles-{self._competition_number}",
            owner=self.owner,
            visibility=visibility,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(
                full_name=f"Singles Home {self._competition_number}",
            ),
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(
                full_name=f"Singles Away {self._competition_number}",
            ),
        )
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            race_target=race_target,
            scheduled_at=timezone.now(),
        )
        start_match(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()
        return competition, pool_match, home_entry

    def post_singles_rack(self, competition, pool_match, winner):
        self.client.force_login(self.owner)
        return self.client.post(
            reverse(
                "pool:manage_match_record_rack",
                args=[competition.slug, pool_match.pk],
            ),
            {
                "winner": winner.pk,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "breaker": "",
                "reason": "",
            },
            follow=False,
        )

    def test_non_final_singles_rack_stays_in_manager_scorer_workflow(self):
        competition, pool_match, home_entry = self.make_singles_match(
            visibility=PoolCompetition.Visibility.PUBLISHED,
            race_target=2,
        )

        response = self.post_singles_rack(competition, pool_match, home_entry)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse(
                "pool:manage_match",
                args=[competition.slug, pool_match.pk],
            ),
        )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.LIVE)

    def test_final_published_singles_rack_redirects_to_public_match_detail(self):
        competition, pool_match, home_entry = self.make_singles_match(
            visibility=PoolCompetition.Visibility.PUBLISHED,
            race_target=1,
        )

        response = self.post_singles_rack(competition, pool_match, home_entry)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("pool:match_detail", args=[pool_match.pk]),
        )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)

    def test_final_published_scheduled_team_rack_redirects_to_public_match_detail(self):
        self._competition_number += 1
        competition = PoolCompetition.objects.create(
            name="Scoring redirect teams",
            slug="scoring-redirect-teams",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home_entry, home_players = self.make_team_entry(competition, "Home Sharks")
        away_entry, away_players = self.make_team_entry(competition, "Away Eagles")
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            race_target=1,
            scheduled_at=timezone.now(),
        )
        for entry, players in (
            (home_entry, home_players),
            (away_entry, away_players),
        ):
            submit_team_match_lineup(
                pool_match,
                entry,
                starters={position: players[position - 1] for position in range(1, 6)},
                actor=self.owner,
            )
        start_match(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()
        scheduled_rack = start_next_eligible_team_rack(
            pool_match,
            table_number=1,
            actor=self.owner,
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "pool:manage_team_match_rack_complete",
                args=[competition.slug, pool_match.pk, scheduled_rack.pk],
            ),
            {
                "winner_player": scheduled_rack.home_player_id,
                "outcome_type": PoolRack.OutcomeType.NORMAL,
                "reason": "",
            },
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("pool:match_detail", args=[pool_match.pk]),
        )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)

    def test_final_draft_rack_falls_back_to_authorised_manager_workflow(self):
        competition, pool_match, home_entry = self.make_singles_match(
            visibility=PoolCompetition.Visibility.DRAFT,
            race_target=1,
        )

        response = self.post_singles_rack(competition, pool_match, home_entry)

        manager_url = reverse(
            "pool:manage_match",
            args=[competition.slug, pool_match.pk],
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, manager_url)
        self.assertEqual(self.client.get(manager_url).status_code, 200)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)

    def make_team_entry(self, competition, name):
        team = PoolTeam.objects.create(name=name)
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=team,
        )
        players = []
        for position in range(1, 6):
            player = PoolPlayer.objects.create(
                full_name=f"{name} Player {position}",
            )
            PoolTeamRoster.objects.create(
                competition_entry=entry,
                player=player,
                sort_order=position,
            )
            players.append(player)
        return entry, players
