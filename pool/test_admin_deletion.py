"""Regression coverage for complete pool-competition deletion in Django admin.

Pool result, standings, and bracket internals deliberately remain protected
from direct admin mutation.  These tests exercise the separate parent-object
path: an active Django superuser must still be able to review and delete an
entire competition, including all competition-owned data, through Django's
normal confirmation and bulk-delete views.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from .brackets import generate_random_bracket
from .models import (
    PoolBracketAuditEvent,
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMarginPointRule,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolKnockoutBracket,
    PoolKnockoutRoundRule,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolMatchResultRevision,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchLineup,
    PoolTeamMatchLineupMember,
    PoolTeamMatchScheduledRack,
    PoolTeamRoster,
)
from .services import correct_rack, open_result_correction, record_rack, start_match
from .standings import replace_pool_standings_points_configuration
from .team_operations import (
    complete_team_match_rack,
    start_next_eligible_team_rack,
    submit_team_match_lineup,
)


User = get_user_model()


class PoolCompetitionAdminDeletionTestCase(TestCase):
    """Build only service-valid pool records for real admin deletion flows."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-admin-delete-owner",
            password="test-password",
        )
        self.superuser = User.objects.create_superuser(
            username="pool-admin-delete-superuser",
            email="pool-admin-delete-superuser@example.test",
            password="test-password",
        )
        self._competition_number = 0
        self._player_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        values = {
            "name": f"Admin deletion pool competition {self._competition_number}",
            "slug": f"admin-deletion-pool-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "default_race_target": 1,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    def make_entries(self, competition, count):
        entries = []
        for _number in range(count):
            self._player_number += 1
            player = PoolPlayer.objects.create(
                full_name=f"Admin deletion player {self._player_number}",
            )
            entries.append(
                PoolCompetitionEntry.objects.create(
                    competition=competition,
                    player=player,
                )
            )
        return entries

    @staticmethod
    def stage_for(competition, stage_type):
        return competition.stages.get(stage_type=stage_type)

    @staticmethod
    def delete_url(competition):
        return reverse("admin:pool_poolcompetition_delete", args=[competition.pk])

    def assert_deletion_confirmation_is_allowed(self, response):
        """Assert Django's own collector found neither protection nor perms gaps."""
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            response.context["perms_lacking"],
            response.context["perms_lacking"],
        )
        self.assertFalse(response.context["protected"], response.context["protected"])

    def delete_competition_as_superuser(self, competition):
        self.client.force_login(self.superuser)
        url = self.delete_url(competition)
        confirmation = self.client.get(url)
        self.assert_deletion_confirmation_is_allowed(confirmation)
        response = self.client.post(url, {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PoolCompetition.objects.filter(pk=competition.pk).exists())

    def make_completed_match_with_history(self, competition):
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        home_entry, away_entry = self.make_entries(competition, 2)
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
        )
        start_match(pool_match, recorded_by=self.owner)
        rack = record_rack(pool_match, winner=home_entry, recorded_by=self.owner)
        pool_match.refresh_from_db()

        # Record a genuine correction, rather than manufacturing immutable
        # revision rows directly. This is the path which exposed the original
        # deletion-confirmation failure for result revisions.
        open_result_correction(
            pool_match,
            recorded_by=self.owner,
            reason="Regression test correction.",
        )
        correct_rack(
            pool_match,
            rack,
            winner=away_entry,
            recorded_by=self.owner,
            reason="Corrected rack winner for deletion regression.",
        )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertGreaterEqual(pool_match.result_revisions.count(), 4)
        return pool_match

    def make_competition_with_standings_and_result_history(self):
        competition = self.make_competition()
        competition, margin_rules = replace_pool_standings_points_configuration(
            competition,
            points_mode=PoolCompetition.PointsMode.MARGIN_BASED,
            points_for_win=2,
            points_for_loss=0,
            margin_rules=(
                (1, 1, 3, 1),
            ),
        )
        pool_match = self.make_completed_match_with_history(competition)
        self.assertTrue(competition.tie_breakers.exists())
        self.assertEqual(len(margin_rules), 1)
        self.assertTrue(pool_match.racks.exists())
        return competition, pool_match

    def make_competition_with_generated_bracket(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.KNOCKOUT)
        self.make_entries(competition, 4)
        bracket = generate_random_bracket(
            competition,
            stage=stage,
            bracket_size=4,
            actor=self.owner,
            confirmed=True,
        )
        preview = bracket.confirmed_preview
        self.assertTrue(bracket.round_rules.exists())
        self.assertTrue(bracket.ties.exists())
        self.assertTrue(PoolBracketAuditEvent.objects.filter(bracket=bracket).exists())
        return competition, bracket, preview


class PoolCompetitionAdminDeletionTests(PoolCompetitionAdminDeletionTestCase):
    def test_superuser_can_confirm_and_delete_a_simple_competition(self):
        competition = self.make_competition()

        self.delete_competition_as_superuser(competition)

    def test_superuser_deletion_cascades_result_history_and_standings_policy(self):
        competition, pool_match = self.make_competition_with_standings_and_result_history()
        player_ids = list(competition.entries.values_list("player_id", flat=True))
        ids = {
            "membership": list(competition.memberships.values_list("pk", flat=True)),
            "stage": list(competition.stages.values_list("pk", flat=True)),
            "entry": list(competition.entries.values_list("pk", flat=True)),
            "tie_breaker": list(competition.tie_breakers.values_list("pk", flat=True)),
            "margin_rule": list(
                competition.margin_point_rules.values_list("pk", flat=True)
            ),
            "match": pool_match.pk,
            "rack": list(pool_match.racks.values_list("pk", flat=True)),
            "revision": list(
                pool_match.result_revisions.values_list("pk", flat=True)
            ),
        }
        self.assertTrue(ids["revision"])

        self.delete_competition_as_superuser(competition)

        self.assertFalse(
            PoolCompetitionMembership.objects.filter(pk__in=ids["membership"]).exists()
        )
        self.assertFalse(PoolCompetitionStage.objects.filter(pk__in=ids["stage"]).exists())
        self.assertFalse(PoolCompetitionEntry.objects.filter(pk__in=ids["entry"]).exists())
        self.assertFalse(
            PoolCompetitionTieBreaker.objects.filter(pk__in=ids["tie_breaker"]).exists()
        )
        self.assertFalse(
            PoolCompetitionMarginPointRule.objects.filter(
                pk__in=ids["margin_rule"]
            ).exists()
        )
        self.assertFalse(PoolMatch.objects.filter(pk=ids["match"]).exists())
        self.assertFalse(PoolRack.objects.filter(pk__in=ids["rack"]).exists())
        self.assertFalse(
            PoolMatchResultRevision.objects.filter(pk__in=ids["revision"]).exists()
        )
        self.assertEqual(PoolPlayer.objects.filter(pk__in=player_ids).count(), len(player_ids))

    def test_confirmation_previews_only_competitions_without_rendering_racks(self):
        competition, pool_match = self.make_competition_with_standings_and_result_history()
        self.client.force_login(self.superuser)

        with patch.object(PoolRack, "__str__", side_effect=AssertionError("rack rendered")):
            confirmation = self.client.get(self.delete_url(competition))

        self.assert_deletion_confirmation_is_allowed(confirmation)
        self.assertEqual(confirmation.context["deleted_objects"], [str(competition)])
        self.assertContains(confirmation, "Competition-specific fixtures, results, stages, rosters, racks")
        self.assertTrue(PoolMatch.objects.filter(pk=pool_match.pk).exists())

    def test_team_competition_deletion_keeps_global_team(self):
        competition = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        team = PoolTeam.objects.create(name="Admin deletion retained team")
        PoolCompetitionEntry.objects.create(competition=competition, team=team)

        self.delete_competition_as_superuser(competition)

        self.assertTrue(PoolTeam.objects.filter(pk=team.pk).exists())

    def test_superuser_deletion_cascades_persistent_bracket_and_audit_graph(self):
        competition, bracket, preview = self.make_competition_with_generated_bracket()
        ids = {
            "preview": preview.pk,
            "bracket": bracket.pk,
            "round_rule": list(bracket.round_rules.values_list("pk", flat=True)),
            "tie": list(bracket.ties.values_list("pk", flat=True)),
            "slot": list(
                PoolKnockoutTieSlot.objects.filter(tie__bracket=bracket).values_list(
                    "pk", flat=True
                )
            ),
            "match": list(
                PoolMatch.objects.filter(knockout_tie__bracket=bracket).values_list(
                    "pk", flat=True
                )
            ),
            "audit": list(
                PoolBracketAuditEvent.objects.filter(competition=competition).values_list(
                    "pk", flat=True
                )
            ),
        }
        self.assertTrue(ids["round_rule"])
        self.assertTrue(ids["tie"])
        self.assertTrue(ids["slot"])
        self.assertTrue(ids["audit"])

        self.delete_competition_as_superuser(competition)

        self.assertFalse(PoolBracketDrawPreview.objects.filter(pk=ids["preview"]).exists())
        self.assertFalse(PoolKnockoutBracket.objects.filter(pk=ids["bracket"]).exists())
        self.assertFalse(
            PoolKnockoutRoundRule.objects.filter(pk__in=ids["round_rule"]).exists()
        )
        self.assertFalse(PoolKnockoutTie.objects.filter(pk__in=ids["tie"]).exists())
        self.assertFalse(PoolKnockoutTieSlot.objects.filter(pk__in=ids["slot"]).exists())
        self.assertFalse(PoolMatch.objects.filter(pk__in=ids["match"]).exists())
        self.assertFalse(PoolBracketAuditEvent.objects.filter(pk__in=ids["audit"]).exists())

    def test_superuser_deletion_cascades_group_qualification_source_graph(self):
        """RESTRICT links stay safe directly but permit the owned parent graph."""

        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        group_stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Group stage"
        group_stage.save()
        knockout_stage = self.stage_for(
            competition,
            PoolCompetitionStage.StageType.KNOCKOUT,
        )
        first_entry, second_entry = self.make_entries(competition, 2)
        group = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=2,
        )
        memberships = [
            PoolGroupEntry.objects.create(
                stage=group_stage,
                group=group,
                entry=entry,
                sort_order=number,
            )
            for number, entry in enumerate((first_entry, second_entry), start=1)
        ]
        qualification_rule = PoolGroupQualificationRule.objects.create(
            source_stage=group_stage,
            group=group,
            knockout_stage=knockout_stage,
            position=1,
            destination_slot=1,
        )
        bracket = PoolKnockoutBracket.objects.create(
            competition=competition,
            stage=knockout_stage,
            size=2,
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            draw_order=[first_entry.pk, second_entry.pk],
        )
        tie = PoolKnockoutTie.objects.create(
            bracket=bracket,
            round_number=1,
            position=1,
            round_name="Final",
            race_target=1,
        )
        source_slot = PoolKnockoutTieSlot.objects.create(
            tie=tie,
            slot=PoolKnockoutTieSlot.Slot.HOME,
            entry=first_entry,
            source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
            source_group=group,
            source_position=1,
        )
        other_slot = PoolKnockoutTieSlot.objects.create(
            tie=tie,
            slot=PoolKnockoutTieSlot.Slot.AWAY,
            entry=second_entry,
            source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
            source_group=group,
            source_position=2,
        )

        self.delete_competition_as_superuser(competition)

        self.assertFalse(PoolGroup.objects.filter(pk=group.pk).exists())
        self.assertFalse(
            PoolGroupEntry.objects.filter(pk__in=[membership.pk for membership in memberships]).exists()
        )
        self.assertFalse(
            PoolGroupQualificationRule.objects.filter(pk=qualification_rule.pk).exists()
        )
        self.assertFalse(PoolKnockoutBracket.objects.filter(pk=bracket.pk).exists())
        self.assertFalse(PoolKnockoutTie.objects.filter(pk=tie.pk).exists())
        self.assertFalse(
            PoolKnockoutTieSlot.objects.filter(pk__in=[source_slot.pk, other_slot.pk]).exists()
        )

    def test_superuser_can_bulk_delete_competitions_with_related_pool_data(self):
        first, _pool_match = self.make_competition_with_standings_and_result_history()
        second, bracket, _preview = self.make_competition_with_generated_bracket()
        second_audit_ids = list(
            PoolBracketAuditEvent.objects.filter(competition=second).values_list(
                "pk", flat=True
            )
        )
        self.assertTrue(second_audit_ids)

        self.client.force_login(self.superuser)
        changelist_url = reverse("admin:pool_poolcompetition_changelist")
        selected_ids = [str(first.pk), str(second.pk)]
        selection = {
            "action": "delete_selected",
            "select_across": "0",
            "_selected_action": selected_ids,
        }
        confirmation = self.client.post(changelist_url, selection)
        self.assert_deletion_confirmation_is_allowed(confirmation)

        response = self.client.post(
            changelist_url,
            {**selection, "post": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PoolCompetition.objects.filter(pk__in=[first.pk, second.pk]).exists())
        self.assertFalse(PoolKnockoutBracket.objects.filter(pk=bracket.pk).exists())
        self.assertFalse(
            PoolBracketAuditEvent.objects.filter(pk__in=second_audit_ids).exists()
        )

    def test_hardened_internal_history_admins_remain_directly_undeletable(self):
        competition, pool_match = self.make_competition_with_standings_and_result_history()
        rack = pool_match.racks.first()
        revision = pool_match.result_revisions.first()
        self.client.force_login(self.superuser)

        for model_name, object_id in (
            ("poolrack", rack.pk),
            ("poolmatchresultrevision", revision.pk),
        ):
            with self.subTest(model_name=model_name):
                response = self.client.get(
                    reverse(f"admin:pool_{model_name}_delete", args=[object_id])
                )
                self.assertEqual(response.status_code, 403)

        self.assertTrue(PoolCompetition.objects.filter(pk=competition.pk).exists())
        self.assertTrue(PoolRack.objects.filter(pk=rack.pk).exists())
        self.assertTrue(PoolMatchResultRevision.objects.filter(pk=revision.pk).exists())

    def test_pool_membership_and_staff_permission_do_not_grant_global_admin_deletion(self):
        competition = self.make_competition()
        staff_pool_admin = User.objects.create_user(
            username="pool-admin-delete-staff-manager",
            password="test-password",
            is_staff=True,
        )
        # Grant Django's ordinary model deletion permission to prove the pool
        # ModelAdmin's superuser boundary, rather than missing auth data, is
        # what protects this route.
        staff_pool_admin.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="pool",
                content_type__model="poolcompetition",
                codename="delete_poolcompetition",
            )
        )
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=staff_pool_admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )

        self.client.force_login(staff_pool_admin)
        response = self.client.get(self.delete_url(competition))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(PoolCompetition.objects.filter(pk=competition.pk).exists())


class PoolMatchAdminDeletionTests(PoolCompetitionAdminDeletionTestCase):
    """Keep fixture deletion confirmation independent of its owned history."""

    @staticmethod
    def delete_url(pool_match):
        return reverse("admin:pool_poolmatch_delete", args=[pool_match.pk])

    def make_team_match_with_owned_history(self):
        competition = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        sides = []
        for team_name in ("Admin delete home", "Admin delete away"):
            team = PoolTeam.objects.create(name=team_name)
            entry = PoolCompetitionEntry.objects.create(
                competition=competition,
                team=team,
            )
            players = []
            for position in range(1, 6):
                self._player_number += 1
                player = PoolPlayer.objects.create(
                    full_name=f"{team_name} player {self._player_number}",
                )
                PoolTeamRoster.objects.create(
                    competition_entry=entry,
                    player=player,
                    sort_order=position,
                )
                players.append(player)
            sides.append((team, entry, players))

        home, away = sides
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home[1],
            away_entry=away[1],
            race_target=1,
        )
        for _team, entry, players in sides:
            submit_team_match_lineup(
                pool_match,
                entry,
                starters={position: players[position - 1] for position in range(1, 6)},
                actor=self.owner,
            )
        start_match(pool_match, recorded_by=self.owner)
        scheduled_rack = start_next_eligible_team_rack(
            pool_match,
            table_number=1,
            actor=self.owner,
        )
        complete_team_match_rack(
            pool_match,
            scheduled_rack,
            winning_player=scheduled_rack.home_player,
            actor=self.owner,
        )
        pool_match.refresh_from_db()
        self.assertTrue(pool_match.racks.exists())
        self.assertTrue(pool_match.team_lineups.exists())
        self.assertTrue(pool_match.team_scheduled_racks.exists())
        return pool_match, home[0], away[0], (*home[2], *away[2])

    def test_confirmation_does_not_render_team_lineup_members(self):
        pool_match, _home_team, _away_team, _players = (
            self.make_team_match_with_owned_history()
        )
        self.client.force_login(self.superuser)

        with patch.object(
            PoolTeamMatchLineupMember,
            "__str__",
            side_effect=AssertionError("lineup member rendered"),
        ):
            confirmation = self.client.get(self.delete_url(pool_match))

        self.assert_deletion_confirmation_is_allowed(confirmation)
        self.assertEqual(
            confirmation.context["deleted_objects"],
            [f"Pool match #{pool_match.pk}"],
        )
        self.assertContains(confirmation, "Match-owned racks, lineups, scorer assignments")
        self.assertTrue(PoolMatch.objects.filter(pk=pool_match.pk).exists())

    def test_individual_confirmation_deletes_owned_history_but_keeps_teams_and_players(self):
        pool_match, home_team, away_team, players = self.make_team_match_with_owned_history()
        ids = {
            "rack": list(pool_match.racks.values_list("pk", flat=True)),
            "revision": list(pool_match.result_revisions.values_list("pk", flat=True)),
            "lineup": list(pool_match.team_lineups.values_list("pk", flat=True)),
            "lineup_member": list(
                PoolTeamMatchLineupMember.objects.filter(
                    lineup__match=pool_match
                ).values_list("pk", flat=True)
            ),
            "schedule": list(
                pool_match.team_scheduled_racks.values_list("pk", flat=True)
            ),
        }
        self.client.force_login(self.superuser)
        response = self.client.post(self.delete_url(pool_match), {"post": "yes"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PoolMatch.objects.filter(pk=pool_match.pk).exists())
        self.assertFalse(PoolRack.objects.filter(pk__in=ids["rack"]).exists())
        self.assertFalse(
            PoolMatchResultRevision.objects.filter(pk__in=ids["revision"]).exists()
        )
        self.assertFalse(PoolTeamMatchLineup.objects.filter(pk__in=ids["lineup"]).exists())
        self.assertFalse(
            PoolTeamMatchLineupMember.objects.filter(
                pk__in=ids["lineup_member"]
            ).exists()
        )
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(pk__in=ids["schedule"]).exists()
        )
        self.assertTrue(PoolTeam.objects.filter(pk__in=(home_team.pk, away_team.pk)).exists())
        self.assertEqual(PoolPlayer.objects.filter(pk__in=[player.pk for player in players]).count(), 10)

    def test_bulk_confirmation_and_deletion_are_lightweight(self):
        pool_match, _home_team, _away_team, _players = (
            self.make_team_match_with_owned_history()
        )
        self.client.force_login(self.superuser)
        changelist_url = reverse("admin:pool_poolmatch_changelist")
        selection = {
            "action": "delete_selected",
            "select_across": "0",
            "_selected_action": [str(pool_match.pk)],
        }

        with patch.object(
            PoolTeamMatchLineupMember,
            "__str__",
            side_effect=AssertionError("lineup member rendered"),
        ):
            confirmation = self.client.post(changelist_url, selection)

        self.assert_deletion_confirmation_is_allowed(confirmation)
        self.assertContains(confirmation, f"Pool match #{pool_match.pk}")
        response = self.client.post(changelist_url, {**selection, "post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PoolMatch.objects.filter(pk=pool_match.pk).exists())

    def test_generated_knockout_fixtures_cannot_be_deleted_individually_or_in_bulk(self):
        _competition, bracket, _preview = self.make_competition_with_generated_bracket()
        generated_match = PoolMatch.objects.filter(
            knockout_tie__bracket=bracket
        ).order_by("pk").first()
        self.assertIsNotNone(generated_match)
        generated_tie = generated_match.knockout_tie

        # Include an ordinary fixture in the selection to prove that the
        # rejection happens before any part of a mixed selection is removed.
        ordinary_competition = self.make_competition()
        ordinary_stage = self.stage_for(
            ordinary_competition,
            PoolCompetitionStage.StageType.LEAGUE,
        )
        ordinary_home, ordinary_away = self.make_entries(ordinary_competition, 2)
        ordinary_match = PoolMatch.objects.create(
            competition=ordinary_competition,
            stage=ordinary_stage,
            home_entry=ordinary_home,
            away_entry=ordinary_away,
        )

        self.client.force_login(self.superuser)
        individual_response = self.client.post(
            self.delete_url(generated_match),
            {"post": "yes"},
        )
        self.assertEqual(individual_response.status_code, 403)
        self.assertTrue(PoolMatch.objects.filter(pk=generated_match.pk).exists())
        self.assertEqual(generated_tie.match_id, generated_match.pk)

        changelist_url = reverse("admin:pool_poolmatch_changelist")
        selection = {
            "action": "delete_selected",
            "select_across": "0",
            "_selected_action": [str(generated_match.pk), str(ordinary_match.pk)],
        }
        bulk_response = self.client.post(changelist_url, selection, follow=True)

        self.assertEqual(bulk_response.status_code, 200)
        self.assertContains(
            bulk_response,
            "Generated bracket fixtures cannot be deleted from the match admin.",
        )
        self.assertTrue(PoolMatch.objects.filter(pk=generated_match.pk).exists())
        self.assertTrue(PoolMatch.objects.filter(pk=ordinary_match.pk).exists())
        generated_tie.refresh_from_db()
        self.assertEqual(generated_tie.match_id, generated_match.pk)

        confirmed_bulk_response = self.client.post(
            changelist_url,
            {**selection, "post": "yes"},
            follow=True,
        )
        self.assertEqual(confirmed_bulk_response.status_code, 200)
        self.assertTrue(PoolMatch.objects.filter(pk=generated_match.pk).exists())
        self.assertTrue(PoolMatch.objects.filter(pk=ordinary_match.pk).exists())
        generated_tie.refresh_from_db()
        self.assertEqual(generated_tie.match_id, generated_match.pk)

    def test_non_superusers_cannot_delete_pool_matches(self):
        competition = self.make_competition()
        stage = self.stage_for(competition, PoolCompetitionStage.StageType.LEAGUE)
        home_entry, away_entry = self.make_entries(competition, 2)
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
        )
        staff_user = User.objects.create_user(
            username="pool-match-delete-staff-manager",
            password="test-password",
            is_staff=True,
        )
        staff_user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="pool",
                content_type__model="poolmatch",
                codename="delete_poolmatch",
            )
        )

        self.client.force_login(staff_user)
        individual_response = self.client.get(self.delete_url(pool_match))
        bulk_response = self.client.post(
            reverse("admin:pool_poolmatch_changelist"),
            {
                "action": "delete_selected",
                "select_across": "0",
                "_selected_action": [str(pool_match.pk)],
            },
        )

        self.assertEqual(individual_response.status_code, 403)
        self.assertEqual(bulk_response.status_code, 403)
        self.assertTrue(PoolMatch.objects.filter(pk=pool_match.pk).exists())
