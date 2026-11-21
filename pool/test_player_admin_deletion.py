"""Safety coverage for superuser-only PoolPlayer hard deletion."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import PoolCompetition, PoolCompetitionEntry, PoolPlayer


User = get_user_model()


class PoolPlayerAdminDeletionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("player-delete-owner", password="test-password")
        self.staff = User.objects.create_user(
            "player-delete-staff", password="test-password", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            "player-delete-superuser",
            "player-delete-superuser@example.test",
            "test-password",
        )

    @staticmethod
    def delete_url(player):
        return reverse("admin:pool_poolplayer_delete", args=[player.pk])

    def test_only_superuser_can_delete_unused_player(self):
        player = PoolPlayer.objects.create(full_name="Unused profile")
        self.client.force_login(self.staff)
        self.assertIn(self.client.get(self.delete_url(player)).status_code, {302, 403})

        self.client.force_login(self.superuser)
        confirmation = self.client.get(self.delete_url(player))
        self.assertEqual(confirmation.status_code, 200)
        self.assertFalse(confirmation.context["perms_lacking"])
        response = self.client.post(self.delete_url(player), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PoolPlayer.objects.filter(pk=player.pk).exists())

    def test_superuser_cannot_delete_player_with_protected_history(self):
        player = PoolPlayer.objects.create(full_name="Historical profile")
        competition = PoolCompetition.objects.create(
            name="Historical player league",
            slug="historical-player-league",
            owner=self.owner,
        )
        entry = PoolCompetitionEntry.objects.create(competition=competition, player=player)
        self.client.force_login(self.superuser)

        confirmation = self.client.get(self.delete_url(player))
        self.assertEqual(confirmation.status_code, 200)
        self.assertTrue(confirmation.context["protected"])
        self.assertContains(confirmation, "Set is_active to False instead")
        response = self.client.post(self.delete_url(player), {"post": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PoolPlayer.objects.filter(pk=player.pk).exists())
        self.assertTrue(PoolCompetitionEntry.objects.filter(pk=entry.pk).exists())

    def test_bulk_delete_uses_the_same_superuser_and_protection_rules(self):
        unused = PoolPlayer.objects.create(full_name="Bulk unused profile")
        protected = PoolPlayer.objects.create(full_name="Bulk historical profile")
        competition = PoolCompetition.objects.create(
            name="Bulk historical player league",
            slug="bulk-historical-player-league",
            owner=self.owner,
        )
        PoolCompetitionEntry.objects.create(competition=competition, player=protected)
        changelist_url = reverse("admin:pool_poolplayer_changelist")
        payload = {
            "action": "delete_selected",
            "select_across": "0",
            "_selected_action": [str(unused.pk), str(protected.pk)],
        }
        self.client.force_login(self.superuser)

        confirmation = self.client.post(changelist_url, payload)
        self.assertEqual(confirmation.status_code, 200)
        self.assertTrue(confirmation.context["protected"])
        response = self.client.post(changelist_url, {**payload, "post": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PoolPlayer.objects.filter(pk=unused.pk).exists())
        self.assertTrue(PoolPlayer.objects.filter(pk=protected.pk).exists())
