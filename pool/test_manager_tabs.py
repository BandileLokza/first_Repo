"""Manager tab-shell and lazy-fragment regression coverage."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import PoolCompetition, PoolPlayer
from .services import create_money_match


User = get_user_model()


class PoolManagerTabTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="manager-tab-owner",
            password="test-password",
        )
        self.outsider = User.objects.create_user(
            username="manager-tab-outsider",
            password="test-password",
        )
        self.competition = PoolCompetition.objects.create(
            name="Manager Tab Competition",
            slug="manager-tab-competition",
            owner=self.owner,
            format_type=PoolCompetition.FormatType.LEAGUE,
        )

    def tab_url(self, tab):
        return reverse(
            "pool:manage_competition_tab_content",
            args=[self.competition.slug, tab],
        )

    def test_dashboard_renders_an_accessible_overview_shell_and_lazy_panels(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_competition_dashboard", args=[self.competition.slug])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-pool-manager-tab-shell')
        self.assertContains(response, 'data-pool-manager-active-tab="overview"')
        self.assertContains(response, 'data-pool-manager-tablist')
        self.assertContains(response, 'role="tab"')
        self.assertContains(response, 'aria-controls="pool-manager-panel-entries"')
        self.assertContains(response, 'data-pool-manager-tab-panel="overview"')
        self.assertContains(response, 'data-loaded="true"')
        self.assertContains(response, 'data-pool-manager-tab-panel="entries"')
        self.assertContains(response, 'data-pool-manager-partial-url="/pool/manage/competitions/manager-tab-competition/tabs/__tab__/"')
        self.assertContains(response, "pool/js/manager_tabs.js")

    def test_direct_manager_urls_remain_renderable_and_tab_query_is_enhanced(self):
        self.client.force_login(self.owner)
        entries_url = reverse(
            "pool:manage_competition_entries", args=[self.competition.slug]
        )

        entries = self.client.get(entries_url)
        dashboard_bookmark = self.client.get(
            reverse("pool:manage_competition_dashboard", args=[self.competition.slug]),
            {"tab": "entries"},
        )

        self.assertEqual(entries.status_code, 200)
        self.assertContains(entries, "Competition entries")
        self.assertContains(entries, 'data-pool-manager-active-tab="entries"')
        self.assertEqual(dashboard_bookmark.status_code, 200)
        self.assertContains(dashboard_bookmark, 'data-pool-manager-tab-shell')
        self.assertContains(dashboard_bookmark, "pool/js/manager_tabs.js")

    def test_lazy_fragment_endpoint_is_auth_and_format_protected(self):
        entries_fragment_url = self.tab_url("entries")

        anonymous = self.client.get(entries_fragment_url)
        self.assertEqual(anonymous.status_code, 302)

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(entries_fragment_url).status_code, 404)

        self.client.force_login(self.owner)
        response = self.client.get(entries_fragment_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Competition entries")
        self.assertNotContains(response, 'class="pool-manager-header"')
        self.assertNotContains(response, 'data-pool-manager-tab-shell')
        self.assertNotIn("<!doctype", response.content.decode().lower())
        self.assertEqual(self.client.get(self.tab_url("bracket")).status_code, 404)

    def test_manager_tab_script_uses_history_and_one_time_fragment_cache(self):
        script = Path(__file__).with_name("static").joinpath(
            "pool", "js", "manager_tabs.js"
        ).read_text(encoding="utf-8")

        self.assertIn('panel.dataset.loaded === "true"', script)
        self.assertIn("window.fetch(partialUrl", script)
        self.assertIn("window.history[mode === \"replace\" ? \"replaceState\" : \"pushState\"]", script)
        self.assertIn('window.addEventListener("popstate"', script)
        self.assertIn('shell.dataset.poolManagerActiveTab || "overview"', script)
        self.assertIn('var initialTab = currentLocationTab();', script)
        self.assertIn('form.setAttribute("action", sourceUrl)', script)

    def test_money_match_information_tab_is_a_fragment_not_a_redirected_shell(self):
        home = PoolPlayer.objects.create(full_name="Money tab home")
        away = PoolPlayer.objects.create(full_name="Money tab away")
        money_match, _pool_match = create_money_match(
            opponent_one=home,
            opponent_two=away,
            race_target=7,
            owner=self.owner,
        )
        self.client.force_login(self.owner)

        information_fragment = self.client.get(
            reverse(
                "pool:manage_competition_tab_content",
                args=[money_match.slug, "information"],
            )
        )
        self.assertEqual(information_fragment.status_code, 200)
        self.assertContains(information_fragment, "Save Money Match")
        self.assertContains(information_fragment, 'name="money_match_prize_pot"')
        self.assertNotContains(information_fragment, 'class="pool-manager-header"')
        self.assertNotContains(information_fragment, 'data-pool-manager-tab-shell')

        information_page = self.client.get(
            reverse("pool:manage_competition_information", args=[money_match.slug])
        )
        self.assertEqual(information_page.status_code, 200)
        self.assertContains(information_page, 'class="pool-manager-header"', count=1)
        self.assertContains(information_page, 'class="pool-manager-nav"', count=1)
        self.assertContains(
            information_page,
            'data-pool-manager-active-tab="information"',
            count=1,
        )

    def test_money_match_tabs_and_direct_edit_retain_their_existing_shells(self):
        home = PoolPlayer.objects.create(full_name="Money direct home")
        away = PoolPlayer.objects.create(full_name="Money direct away")
        money_match, _pool_match = create_money_match(
            opponent_one=home,
            opponent_two=away,
            race_target=7,
            owner=self.owner,
        )
        self.client.force_login(self.owner)

        for tab, expected_content in (
            ("overview", "Match information"),
            ("fixtures", "Money Match"),
            ("administrators", "Competition administrators"),
        ):
            with self.subTest(tab=tab):
                response = self.client.get(
                    reverse(
                        "pool:manage_competition_tab_content",
                        args=[money_match.slug, tab],
                    )
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_content)
                self.assertNotContains(response, 'class="pool-manager-header"')

        direct_edit = self.client.get(
            reverse("pool:manage_money_match_edit", args=[money_match.slug])
        )
        self.assertEqual(direct_edit.status_code, 200)
        self.assertContains(direct_edit, 'class="pool-manager-header"', count=1)
        self.assertContains(direct_edit, "Save Money Match")

        normal_information = self.client.get(self.tab_url("information"))
        self.assertEqual(normal_information.status_code, 200)
        self.assertContains(normal_information, "Competition information")
        self.assertNotContains(normal_information, 'class="pool-manager-header"')
