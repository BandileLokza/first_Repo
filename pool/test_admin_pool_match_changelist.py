"""Regression coverage for the PoolMatch Django-admin changelist."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection, reset_queries
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolTeam,
)


User = get_user_model()


class PoolMatchAdminChangelistTests(TestCase):
    """Exercise the exact relation paths used by the changelist columns."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-match-changelist-owner",
            password="test-password",
        )
        self.superuser = User.objects.create_superuser(
            username="pool-match-changelist-superuser",
            email="pool-match-changelist-superuser@example.test",
            password="test-password",
        )
        self.client.force_login(self.superuser)
        self._competition_number = 0
        self._participant_number = 0

    def make_competition(self, *, participant_mode=PoolCompetition.ParticipantMode.SINGLES):
        self._competition_number += 1
        competition = PoolCompetition.objects.create(
            name=f"Admin match list competition {self._competition_number}",
            slug=f"admin-match-list-{self._competition_number}",
            owner=self.owner,
            participant_mode=participant_mode,
            format_type=PoolCompetition.FormatType.LEAGUE,
            default_race_target=1,
        )
        return competition, competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )

    def make_match(self, competition, stage, *, prefix, team_match=False, finished=False):
        self._participant_number += 1
        if team_match:
            home_participant = PoolTeam.objects.create(
                name=f"{prefix} Home Team {self._participant_number}"
            )
            away_participant = PoolTeam.objects.create(
                name=f"{prefix} Away Team {self._participant_number}"
            )
            home_entry = PoolCompetitionEntry.objects.create(
                competition=competition,
                team=home_participant,
            )
            away_entry = PoolCompetitionEntry.objects.create(
                competition=competition,
                team=away_participant,
            )
        else:
            home_participant = PoolPlayer.objects.create(
                full_name=f"{prefix} Home Player {self._participant_number}"
            )
            away_participant = PoolPlayer.objects.create(
                full_name=f"{prefix} Away Player {self._participant_number}"
            )
            home_entry = PoolCompetitionEntry.objects.create(
                competition=competition,
                player=home_participant,
            )
            away_entry = PoolCompetitionEntry.objects.create(
                competition=competition,
                player=away_participant,
            )

        values = {
            "competition": competition,
            "stage": stage,
            "home_entry": home_entry,
            "away_entry": away_entry,
            "race_target": 1,
            "scheduled_at": timezone.now(),
        }
        if finished:
            values.update(
                status=PoolMatch.Status.FINISHED,
                home_score=1,
                winner=home_entry,
            )
        return PoolMatch.objects.create(**values)

    @staticmethod
    def changelist_url():
        return reverse("admin:pool_poolmatch_changelist")

    def test_changelist_renders_complete_singles_and_team_names_without_row_queries(self):
        singles_competition, singles_stage = self.make_competition()
        team_competition, team_stage = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        matches = [
            self.make_match(
                singles_competition,
                singles_stage,
                prefix="Singles winner",
                finished=True,
            ),
            self.make_match(
                singles_competition,
                singles_stage,
                prefix="Singles pending",
            ),
            self.make_match(
                team_competition,
                team_stage,
                prefix="Team winner",
                team_match=True,
                finished=True,
            ),
            self.make_match(
                team_competition,
                team_stage,
                prefix="Team pending",
                team_match=True,
            ),
        ]

        response = self.client.get(self.changelist_url())

        self.assertEqual(response.status_code, 200)
        for pool_match in matches:
            self.assertContains(response, pool_match.home_entry.display_name)
            self.assertContains(response, pool_match.away_entry.display_name)
        self.assertContains(response, "Singles winner Home Player")
        self.assertContains(response, "Team winner Home Team")
        self.assertContains(response, 'name="_selected_action"', count=len(matches))

        model_admin = admin.site._registry[PoolMatch]
        self.assertEqual(model_admin.list_per_page, 50)
        request = RequestFactory().get(self.changelist_url())
        selected_match = model_admin.get_queryset(request).get(pk=matches[0].pk)
        with self.assertNumQueries(0):
            self.assertEqual(str(selected_match), "Singles winner Home Player 1 vs Singles winner Away Player 1")
            self.assertEqual(
                str(selected_match.winner),
                f"{singles_competition.name}: Singles winner Home Player 1",
            )
            self.assertEqual(
                str(selected_match.stage),
                f"{singles_competition.name}: {singles_stage.name}",
            )

    def _changelist_query_count(self):
        reset_queries()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.changelist_url())
            self.assertEqual(response.status_code, 200)
            # Force the rendered changelist, including action checkboxes and
            # the winner column, inside the measured block.
            response.render()
        return len(queries), response

    def test_changelist_query_count_does_not_grow_per_rendered_row(self):
        competition, stage = self.make_competition()
        for number in range(3):
            self.make_match(
                competition,
                stage,
                prefix=f"Small query sample {number}",
                finished=number % 2 == 0,
            )
        small_query_count, small_response = self._changelist_query_count()
        self.assertContains(small_response, 'name="_selected_action"', count=3)
        self.assertContains(small_response, "Small query sample 0 Home Player")

        for number in range(57):
            self.make_match(
                competition,
                stage,
                prefix=f"Large query sample {number}",
                finished=number % 2 == 0,
            )
        large_query_count, large_response = self._changelist_query_count()

        # The first page now renders 50 rows, including 25 populated winner
        # columns and 50 action checkboxes.  Leave room for harmless backend
        # query-shape differences while rejecting any per-row relationship
        # lookups.
        self.assertContains(large_response, 'name="_selected_action"', count=50)
        self.assertContains(large_response, "Large query sample 46 Home Player")
        self.assertLessEqual(
            large_query_count,
            small_query_count + 3,
            (small_query_count, large_query_count),
        )

    def test_search_filter_pagination_date_navigation_and_change_view_work(self):
        competition, stage = self.make_competition()
        searchable_match = self.make_match(
            competition,
            stage,
            prefix="Searchable participant",
            finished=True,
        )
        for number in range(50):
            self.make_match(
                competition,
                stage,
                prefix=f"Pagination participant {number}",
            )

        search_response = self.client.get(
            self.changelist_url(),
            {"q": "Searchable participant Home Player"},
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertContains(search_response, "Searchable participant Home Player")

        filtered_response = self.client.get(
            self.changelist_url(),
            {"status__exact": PoolMatch.Status.FINISHED},
        )
        self.assertEqual(filtered_response.status_code, 200)
        self.assertContains(filtered_response, "Searchable participant Home Player")

        pagination_response = self.client.get(self.changelist_url(), {"p": 1})
        self.assertEqual(pagination_response.status_code, 200)
        self.assertEqual(pagination_response.context["cl"].paginator.per_page, 50)

        date_response = self.client.get(
            self.changelist_url(),
            {"scheduled_at__year": searchable_match.scheduled_at.year},
        )
        self.assertEqual(date_response.status_code, 200)
        self.assertContains(date_response, "Searchable participant Home Player")

        change_response = self.client.get(
            reverse("admin:pool_poolmatch_change", args=[searchable_match.pk])
        )
        self.assertEqual(change_response.status_code, 200)
