"""Public competition discovery coverage for Pool landing and entrant views."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolTeam,
    PoolTeamRoster,
)
from .services import record_rack, start_match


User = get_user_model()


class PoolPublicDiscoveryTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="public-discovery-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        values = {
            "name": f"Public Discovery {self._competition_number}",
            "slug": f"public-discovery-{self._competition_number}",
            "owner": self.owner,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "operational_state": PoolCompetition.OperationalState.UPCOMING,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    @staticmethod
    def competition_url(competition, **query):
        return reverse("pool:competition_detail", args=[competition.slug]), query

    @staticmethod
    def home_card_attributes(response, competition):
        """Return the opening wrapper tag for one preloaded home card."""

        content = response.content.decode()
        competition_url = reverse("pool:competition_detail", args=[competition.slug])
        card_link = content.index(f'href="{competition_url}"')
        card_start = content.rfind("<div", 0, card_link)
        return content[card_start : content.index(">", card_start) + 1]

    @staticmethod
    def tab_panel_markup(response, tab_name):
        """Return a simple public tab panel without parsing unrelated panels."""
        content = response.content.decode()
        panel_start = content.index(f'id="pool-panel-{tab_name}"')
        panel_end = content.index("</section>", panel_start)
        return content[panel_start:panel_end]

    def make_singles_entry(self, competition, name, **player_values):
        player = PoolPlayer.objects.create(full_name=name, **player_values)
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=player,
        )
        return player, entry

    def test_home_status_filters_combine_and_never_show_drafts(self):
        active = self.make_competition(
            name="Active Singles League",
            slug="active-singles-league",
            operational_state=PoolCompetition.OperationalState.ACTIVE,
        )
        upcoming = self.make_competition(
            name="Upcoming Singles League",
            slug="upcoming-singles-league",
        )
        finished_teams = self.make_competition(
            name="Finished Team Tournament",
            slug="finished-team-tournament",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            operational_state=PoolCompetition.OperationalState.COMPLETED,
        )
        draft = self.make_competition(
            name="Draft Active Competition",
            slug="draft-active-competition",
            visibility=PoolCompetition.Visibility.DRAFT,
            operational_state=PoolCompetition.OperationalState.ACTIVE,
        )
        active_home_player, active_home_entry = self.make_singles_entry(
            active,
            "Status Panel Home",
        )
        _active_away_player, active_away_entry = self.make_singles_entry(
            active,
            "Status Panel Away",
        )
        active_match = PoolMatch.objects.create(
            competition=active,
            stage=active.stages.get(
                stage_type=PoolCompetitionStage.StageType.LEAGUE
            ),
            home_entry=active_home_entry,
            away_entry=active_away_entry,
            scheduled_at=timezone.now(),
        )
        start_match(active_match, recorded_by=self.owner)

        response = self.client.get(reverse("pool:home"))

        self.assertEqual(response.context["current_status"], "active")
        self.assertContains(response, active.name)
        self.assertIn(
            'data-category="leagues"',
            self.home_card_attributes(response, active),
        )
        self.assertNotIn("hidden", self.home_card_attributes(response, active))
        self.assertIn("hidden", self.home_card_attributes(response, upcoming))
        self.assertIn("hidden", self.home_card_attributes(response, finished_teams))
        self.assertNotContains(response, draft.name)

        response = self.client.get(
            reverse("pool:home"),
            {"status": "finished", "category": "tournaments", "mode": "teams"},
        )
        self.assertEqual(response.context["current_status"], "finished")
        self.assertContains(response, finished_teams.name)
        self.assertContains(response, "Finished")
        self.assertNotIn(
            "hidden",
            self.home_card_attributes(response, finished_teams),
        )
        self.assertIn("hidden", self.home_card_attributes(response, active))
        # Card rows are preloaded for client-side discovery filtering, so an
        # active card can contain its public match rows while remaining hidden
        # on this Finished-only server-rendered view. The card visibility
        # assertion above is the public filter boundary.
        self.assertNotContains(response, 'id="pool-live-title"')
        self.assertNotContains(response, draft.name)

        response = self.client.get(reverse("pool:home"), {"status": "all"})
        self.assertContains(response, active.name)
        self.assertContains(response, upcoming.name)
        self.assertContains(response, finished_teams.name)
        self.assertNotIn("hidden", self.home_card_attributes(response, active))
        self.assertNotIn("hidden", self.home_card_attributes(response, upcoming))
        self.assertNotIn("hidden", self.home_card_attributes(response, finished_teams))
        self.assertNotContains(response, draft.name)

    def test_home_defaults_to_upcoming_when_no_active_competition_matches(self):
        upcoming = self.make_competition(
            name="Default Upcoming Competition",
            slug="default-upcoming-competition",
        )
        finished = self.make_competition(
            name="Historical Competition",
            slug="historical-competition",
            operational_state=PoolCompetition.OperationalState.COMPLETED,
        )

        response = self.client.get(reverse("pool:home"))

        self.assertEqual(response.context["current_status"], "upcoming")
        self.assertContains(response, upcoming.name)
        self.assertNotIn("hidden", self.home_card_attributes(response, upcoming))
        self.assertIn("hidden", self.home_card_attributes(response, finished))

    def test_home_direct_filter_url_preloads_cards_with_safe_initial_state(self):
        upcoming_tournament = self.make_competition(
            name="Upcoming Singles Tournament",
            slug="upcoming-singles-tournament",
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )
        active_singles_league = self.make_competition(
            name="Active Singles League",
            slug="active-singles-league-filter",
            operational_state=PoolCompetition.OperationalState.ACTIVE,
        )
        active_teams_league = self.make_competition(
            name="Active Teams League",
            slug="active-teams-league-filter",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            operational_state=PoolCompetition.OperationalState.ACTIVE,
        )
        finished_team_tournament = self.make_competition(
            name="Finished Team Tournament",
            slug="finished-team-tournament-filter",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            operational_state=PoolCompetition.OperationalState.COMPLETED,
        )
        draft = self.make_competition(
            name="Never Leak Draft",
            slug="never-leak-draft",
            visibility=PoolCompetition.Visibility.DRAFT,
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )

        response = self.client.get(
            reverse("pool:home"),
            {
                "category": "tournaments",
                "mode": "singles",
                "status": "upcoming",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["home_filter_category"], "tournaments")
        self.assertEqual(response.context["home_filter_mode"], "singles")
        self.assertEqual(response.context["current_status"], "upcoming")
        self.assertContains(response, "data-pool-home-filters")
        self.assertContains(response, "data-pool-home-card-grid")
        self.assertContains(response, "data-pool-home-filter-status")
        self.assertContains(response, "pool/js/home_filters.js")
        self.assertIn(
            'data-category="tournaments"',
            self.home_card_attributes(response, upcoming_tournament),
        )
        self.assertIn(
            'data-mode="singles"',
            self.home_card_attributes(response, upcoming_tournament),
        )
        self.assertIn(
            'data-status="upcoming"',
            self.home_card_attributes(response, upcoming_tournament),
        )
        self.assertNotIn(
            "hidden",
            self.home_card_attributes(response, upcoming_tournament),
        )
        self.assertIn(
            "hidden",
            self.home_card_attributes(response, active_singles_league),
        )
        self.assertIn(
            "hidden",
            self.home_card_attributes(response, active_teams_league),
        )
        self.assertIn(
            "hidden",
            self.home_card_attributes(response, finished_team_tournament),
        )
        self.assertNotContains(response, draft.name)

        league_teams_response = self.client.get(
            reverse("pool:home"),
            {
                "category": "leagues",
                "mode": "teams",
                "status": "active",
            },
        )
        self.assertEqual(league_teams_response.status_code, 200)
        self.assertNotIn(
            "hidden",
            self.home_card_attributes(league_teams_response, active_teams_league),
        )
        self.assertIn(
            "hidden",
            self.home_card_attributes(league_teams_response, upcoming_tournament),
        )
        self.assertIn(
            "hidden",
            self.home_card_attributes(league_teams_response, finished_team_tournament),
        )

    def test_home_filter_script_represents_history_and_accessible_card_updates(self):
        script_path = finders.find("pool/js/home_filters.js")

        self.assertIsNotNone(script_path)
        script = Path(script_path).read_text(encoding="utf-8")
        self.assertIn("event.preventDefault()", script)
        self.assertIn("window.history.pushState", script)
        self.assertIn('addEventListener("popstate"', script)
        self.assertIn("card.hidden = !visible", script)
        self.assertIn('setAttribute("aria-current", "page")', script)
        self.assertIn("data-pool-home-filter-status", script)
        self.assertIn("hasRequestedFilters", script)
        self.assertIn('url.searchParams.delete(key)', script)

    def test_started_and_finished_play_activate_without_auto_completing_competition(self):
        competition = self.make_competition(default_race_target=1)
        home_player, home_entry = self.make_singles_entry(
            competition,
            "State Home",
        )
        _away_player, away_entry = self.make_singles_entry(
            competition,
            "State Away",
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )

        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.UPCOMING,
        )

        start_match(pool_match, recorded_by=self.owner)
        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.ACTIVE,
        )
        record_rack(pool_match, winner=home_entry, recorded_by=self.owner)
        pool_match.refresh_from_db()
        competition.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertNotEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.COMPLETED,
        )
        self.assertTrue(home_player.pk)

    def test_singles_entrants_are_scoped_safe_and_filterable(self):
        competition = self.make_competition(
            name="Singles Entrants",
            slug="singles-entrants",
        )
        account = User.objects.create_user(
            username="private-entrant-account",
            email="account@example.test",
            password="test-password",
        )
        siya, _siya_entry = self.make_singles_entry(
            competition,
            "Siyabonga Dlamini",
            nickname="Siya",
            region="Manzini",
            contact_email="siya-private@example.test",
            contact_phone="+268-555-0101",
            user=account,
        )
        nandi, _nandi_entry = self.make_singles_entry(
            competition,
            "Nandi Maseko",
            nickname="Nands",
            region="Mbabane",
        )
        other_competition = self.make_competition(
            name="Other Entrants",
            slug="other-entrants",
        )
        outsider, _outsider_entry = self.make_singles_entry(
            other_competition,
            "Outside Competition Player",
            nickname="Outside",
            region="Manzini",
        )

        response = self.client.get(
            *self.competition_url(competition, tab="entrants")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, siya.full_name)
        self.assertContains(response, siya.nickname)
        self.assertContains(response, siya.region)
        self.assertContains(
            response,
            reverse("pool:player_detail", args=[siya.public_id]),
        )
        self.assertContains(response, nandi.full_name)
        self.assertNotContains(response, outsider.full_name)
        self.assertNotContains(response, siya.contact_email)
        self.assertNotContains(response, siya.contact_phone)
        self.assertNotContains(response, account.username)
        self.assertNotContains(response, "1 life")

        response = self.client.get(
            *self.competition_url(competition, tab="entrants", q="Dlamini")
        )
        entrant_panel = self.tab_panel_markup(response, "entrants")
        self.assertIn(siya.full_name, entrant_panel)
        self.assertNotIn(nandi.full_name, entrant_panel)

        response = self.client.get(
            *self.competition_url(competition, tab="entrants", q="Siya")
        )
        entrant_panel = self.tab_panel_markup(response, "entrants")
        self.assertIn(siya.full_name, entrant_panel)
        self.assertNotIn(nandi.full_name, entrant_panel)

        response = self.client.get(
            *self.competition_url(
                competition,
                tab="entrants",
                region="Mbabane",
            )
        )
        entrant_panel = self.tab_panel_markup(response, "entrants")
        self.assertIn(nandi.full_name, entrant_panel)
        self.assertNotIn(siya.full_name, entrant_panel)

        response = self.client.get(
            *self.competition_url(competition, tab="entrants", q="Nobody")
        )
        self.assertContains(response, "No entrants match your search.")

    def test_two_life_entrants_are_counted_once_per_person(self):
        competition = self.make_competition(
            name="Two Life Entrants",
            slug="two-life-entrants",
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            allow_two_lives=True,
        )
        siya, siya_entry = self.make_singles_entry(
            competition,
            "Two Life Siya",
            region="Manzini",
        )
        _other, _other_entry = self.make_singles_entry(
            competition,
            "One Life Nandi",
            region="Mbabane",
        )
        PoolCompetitionEntryLife.objects.create(
            competition_entry=siya_entry,
            life_number=2,
        )

        response = self.client.get(
            *self.competition_url(competition, tab="entrants")
        )

        self.assertEqual(len(response.context["entrants"]), 2)
        self.assertContains(response, "2 players registered.")
        self.assertContains(response, "3 tournament lives.")
        self.assertContains(response, "2 lives")
        self.assertNotContains(response, "Life 1")
        self.assertNotContains(response, "Life 2")
        self.assertContains(response, siya.full_name)

    def test_team_entrants_show_teams_roster_counts_and_team_search(self):
        competition = self.make_competition(
            name="Team Entrants",
            slug="team-entrants",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        cue_kings = PoolTeam.objects.create(
            name="Cue Kings",
            description="Private team administration note",
        )
        cue_kings_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=cue_kings,
        )
        PoolTeamRoster.objects.create(
            competition_entry=cue_kings_entry,
            player=PoolPlayer.objects.create(full_name="Roster One"),
        )
        PoolTeamRoster.objects.create(
            competition_entry=cue_kings_entry,
            player=PoolPlayer.objects.create(full_name="Roster Two"),
        )
        PoolTeamRoster.objects.create(
            competition_entry=cue_kings_entry,
            player=PoolPlayer.objects.create(full_name="Inactive Roster"),
            is_active=False,
        )
        other_team = PoolTeam.objects.create(name="Break Masters")
        PoolCompetitionEntry.objects.create(competition=competition, team=other_team)

        response = self.client.get(
            *self.competition_url(competition, tab="entrants")
        )
        self.assertContains(response, cue_kings.name)
        self.assertContains(response, "2 registered players")
        self.assertContains(
            response,
            reverse("pool:team_detail", args=[cue_kings.pk]),
        )
        self.assertNotContains(response, "Private team administration note")
        self.assertNotContains(response, "Roster One")

        response = self.client.get(
            *self.competition_url(competition, tab="entrants", q="kings")
        )
        entrant_panel = self.tab_panel_markup(response, "entrants")
        self.assertIn(cue_kings.name, entrant_panel)
        self.assertNotIn(other_team.name, entrant_panel)

    def test_upcoming_overview_limits_the_entrant_preview_and_zero_state_is_clear(self):
        competition = self.make_competition(
            name="Overview Preview",
            slug="overview-preview",
        )
        for index in range(10):
            self.make_singles_entry(
                competition,
                f"Preview Entrant {index:02d}",
            )

        response = self.client.get(*self.competition_url(competition))
        self.assertContains(response, "Who's playing?")
        self.assertContains(response, "View all 10 players")
        self.assertContains(response, "Preview Entrant 00")
        self.assertContains(response, "Preview Entrant 07")
        self.assertEqual(len(response.context["entrant_preview"]), 8)
        self.assertEqual(
            [entry.player.full_name for entry in response.context["entrant_preview"]],
            [f"Preview Entrant {index:02d}" for index in range(8)],
        )
        # Public detail now lazy-loads the full entrants tab. The overview
        # request contains only its intentionally small preview.
        self.assertNotContains(response, "Preview Entrant 08")
        self.assertNotContains(response, "Preview Entrant 09")

        empty_competition = self.make_competition(
            name="Empty Entrants",
            slug="empty-entrants",
        )
        response = self.client.get(
            *self.competition_url(empty_competition, tab="entrants")
        )
        self.assertContains(response, "No entrants have been published yet.")

    def test_draft_entrant_tab_is_not_public(self):
        competition = self.make_competition(
            name="Draft Entrants",
            slug="draft-entrants",
            visibility=PoolCompetition.Visibility.DRAFT,
        )
        self.make_singles_entry(competition, "Private Draft Entrant")

        response = self.client.get(
            *self.competition_url(competition, tab="entrants")
        )

        self.assertEqual(response.status_code, 404)

    def test_manager_draft_preview_uses_a_safe_unscoped_profile_link(self):
        competition = self.make_competition(
            name="Draft Manager Entrants",
            slug="draft-manager-entrants",
            visibility=PoolCompetition.Visibility.DRAFT,
        )
        player, _entry = self.make_singles_entry(
            competition,
            "Draft Preview Player",
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            *self.competition_url(competition, tab="entrants")
        )

        player_url = reverse("pool:player_detail", args=[player.public_id])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, player_url)
        self.assertNotContains(
            response,
            f"{player_url}?competition={competition.slug}",
        )
