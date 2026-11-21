"""Regression coverage for competition-scoped team roster registration."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from .models import PoolCompetition, PoolCompetitionEntry, PoolPlayer, PoolTeam, PoolTeamRoster


User = get_user_model()


class TeamRosterRegistrationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("roster-owner", password="test-password")
        self.other = User.objects.create_user("roster-other", password="test-password")
        self.competition = PoolCompetition.objects.create(
            name="Roster registration league",
            slug="roster-registration-league",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        self.entry = self.make_entry("Roster Alpha")
        self.other_entry = self.make_entry("Roster Bravo")

    def make_entry(self, name):
        return PoolCompetitionEntry.objects.create(
            competition=self.competition,
            team=PoolTeam.objects.create(name=name),
        )

    def roster_add_url(self, entry=None):
        return reverse(
            "pool:manage_competition_team_roster_add",
            args=[self.competition.slug, (entry or self.entry).pk],
        )

    def register_url(self, entry=None):
        return reverse(
            "pool:manage_competition_team_roster_register_player",
            args=[self.competition.slug, (entry or self.entry).pk],
        )

    def roster_url(self, entry=None):
        return reverse(
            "pool:manage_competition_team_roster",
            args=[self.competition.slug, (entry or self.entry).pk],
        )

    def test_existing_players_are_added_in_order_and_save_add_another_stays_scoped(self):
        first = PoolPlayer.objects.create(full_name="Roster first")
        second = PoolPlayer.objects.create(full_name="Roster second")
        self.client.force_login(self.owner)

        response = self.client.post(
            self.roster_add_url(),
            {"player_ids": [str(first.pk), str(second.pk)], "save_and_add_another": "1"},
        )

        self.assertRedirects(response, self.roster_add_url())
        memberships = list(PoolTeamRoster.objects.filter(competition_entry=self.entry).order_by("sort_order"))
        self.assertEqual([membership.player_id for membership in memberships], [first.pk, second.pk])
        self.assertEqual([membership.sort_order for membership in memberships], [0, 1])

    def test_existing_player_rejects_same_or_other_team_membership(self):
        player = PoolPlayer.objects.create(full_name="Already rostered")
        PoolTeamRoster.objects.create(competition_entry=self.entry, player=player)
        self.client.force_login(self.owner)

        same_team = self.client.post(self.roster_add_url(), {"player_ids": [str(player.pk)]})
        self.assertEqual(same_team.status_code, 200)
        self.assertContains(same_team, "already on this competition roster")

        PoolTeamRoster.objects.filter(competition_entry=self.entry, player=player).delete()
        PoolTeamRoster.objects.create(competition_entry=self.other_entry, player=player)
        other_team = self.client.post(self.roster_add_url(), {"player_ids": [str(player.pk)]})
        self.assertEqual(other_team.status_code, 200)
        self.assertContains(other_team, "already registered for Roster Bravo")

    def test_registering_new_player_creates_global_profile_and_roster_membership(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.register_url(),
            {"full_name": "New roster profile", "region": "Manzini"},
        )

        self.assertRedirects(response, self.roster_url())
        player = PoolPlayer.objects.get(full_name="New roster profile")
        self.assertTrue(PoolTeamRoster.objects.filter(competition_entry=self.entry, player=player).exists())
        self.assertEqual(player.region, "Manzini")

    def test_duplicate_suggestion_uses_existing_profile_without_creating_another(self):
        existing = PoolPlayer.objects.create(full_name="Possible duplicate", region="Mbabane")
        self.client.force_login(self.owner)
        data = {"full_name": existing.full_name, "region": existing.region}

        suggestion = self.client.post(self.register_url(), data)
        self.assertEqual(suggestion.status_code, 200)
        self.assertContains(suggestion, "Possible existing players")
        response = self.client.post(
            self.register_url(),
            {**data, "existing_player": str(existing.pk), "action": "use_existing"},
        )

        self.assertRedirects(response, self.roster_url())
        self.assertEqual(PoolPlayer.objects.filter(full_name=existing.full_name).count(), 1)
        self.assertTrue(PoolTeamRoster.objects.filter(competition_entry=self.entry, player=existing).exists())

    def test_registration_rolls_back_profile_when_roster_addition_fails(self):
        self.client.force_login(self.owner)
        with patch(
            "pool.manager_views._add_players_to_team_roster",
            side_effect=ValidationError("forced roster failure"),
        ):
            response = self.client.post(
                self.register_url(),
                {"full_name": "Rolled back roster player"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PoolPlayer.objects.filter(full_name="Rolled back roster player").exists())

    def test_register_save_add_another_and_cross_competition_access_are_scoped(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.register_url(),
            {"full_name": "Another roster player", "save_mode": "add_another"},
        )
        self.assertRedirects(response, self.register_url())
        self.assertTrue(PoolTeamRoster.objects.filter(competition_entry=self.entry).exists())

        other_competition = PoolCompetition.objects.create(
            name="Other team league",
            slug="other-team-league",
            owner=self.other,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        foreign_entry = PoolCompetitionEntry.objects.create(
            competition=other_competition,
            team=PoolTeam.objects.create(name="Foreign roster team"),
        )
        foreign_url = reverse(
            "pool:manage_competition_team_roster_register_player",
            args=[other_competition.slug, foreign_entry.pk],
        )
        self.assertEqual(self.client.get(foreign_url).status_code, 404)
