"""Public competition tab-panel regression coverage."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import PoolCompetition


User = get_user_model()


class PoolPublicCompetitionTabTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="public-tab-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        values = {
            "name": f"Public Tab Competition {self._competition_number}",
            "slug": f"public-tab-competition-{self._competition_number}",
            "owner": self.owner,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    @staticmethod
    def competition_url(competition):
        return reverse("pool:competition_detail", args=[competition.slug])

    def test_initial_response_contains_only_the_active_public_panel(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )

        response = self.client.get(self.competition_url(competition))

        self.assertEqual(response.status_code, 200)
        for panel in ("overview", "entrants", "fixtures", "results", "standings", "bracket", "stats"):
            self.assertContains(response, f'data-pool-tab="{panel}"')
        self.assertContains(response, 'id="pool-panel-overview"')
        for panel in ("entrants", "fixtures", "results", "standings", "bracket", "stats"):
            self.assertNotContains(response, f'id="pool-panel-{panel}"')
        self.assertContains(response, 'data-pool-competition-tabs')
        self.assertContains(response, 'pool/js/competition_tabs.js')

    def test_direct_results_url_selects_the_results_panel(self):
        competition = self.make_competition()

        response = self.client.get(self.competition_url(competition), {"tab": "results"})
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "results")
        self.assertIn(
            'data-pool-tab="results" aria-controls="pool-panel-results" '
            'aria-selected="true" tabindex="0"',
            content,
        )
        self.assertIn('id="pool-panel-results"', content)
        self.assertIn('aria-labelledby="pool-tab-results"', content)
        self.assertIn('data-loaded="true"', content)

    def test_filtered_entrant_tab_keeps_its_bookmarkable_search_url(self):
        competition = self.make_competition()

        response = self.client.get(
            self.competition_url(competition),
            {"tab": "entrants", "q": "Siyabonga"},
        )

        self.assertContains(
            response,
            f'href="{self.competition_url(competition)}?tab=entrants&amp;q=Siyabonga"',
        )

    def test_irrelevant_format_panels_are_not_rendered(self):
        knockout = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )
        league = self.make_competition()

        knockout_response = self.client.get(self.competition_url(knockout))
        league_response = self.client.get(self.competition_url(league))

        self.assertNotContains(knockout_response, 'id="pool-panel-standings"')
        self.assertNotContains(knockout_response, 'pool-tab-standings')
        self.assertContains(knockout_response, 'pool-tab-bracket')
        self.assertNotContains(knockout_response, 'id="pool-panel-bracket"')
        self.assertNotContains(league_response, 'id="pool-panel-bracket"')
        self.assertNotContains(league_response, 'pool-tab-bracket')

    def test_tab_script_intercepts_navigation_and_restores_history_state(self):
        script_path = finders.find("pool/js/competition_tabs.js")

        self.assertIsNotNone(script_path)
        script = Path(script_path).read_text(encoding="utf-8")
        self.assertIn("event.preventDefault()", script)
        self.assertIn("window.history.pushState", script)
        self.assertIn('addEventListener("popstate"', script)
        self.assertIn("panel.hidden = false", script)
