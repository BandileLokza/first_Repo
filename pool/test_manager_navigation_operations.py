"""Regression coverage for Pool manager navigation and lifecycle actions."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolMatch,
    PoolPlayer,
    PoolTeam,
)
from .admin import PoolCompetitionAdmin
from .forms import PoolFixtureForm, PoolGroupForm
from .manager_views import _locked_group_entries
from .services import record_non_played_result, record_rack, start_match


User = get_user_model()


class PoolManagerNavigationOperationsTestCase(TestCase):
    """Small HTTP/service fixtures for the manager-action regression suite."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-navigation-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        values = {
            "name": f"Navigation Operations {self._competition_number}",
            "slug": f"navigation-operations-{self._competition_number}",
            "owner": self.owner,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "operational_state": PoolCompetition.OperationalState.UPCOMING,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    @staticmethod
    def league_stage(competition):
        return competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE,
        )

    @staticmethod
    def group_and_knockout_stages(competition):
        group_stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE,
        )
        group_stage.stage_type = PoolCompetitionStage.StageType.GROUP
        group_stage.name = "Group Stage"
        group_stage.save()
        knockout_stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
        )
        return group_stage, knockout_stage

    @staticmethod
    def make_entry(competition, name):
        return PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=name),
        )

    def make_match(self, competition, *, race_target=7):
        home = self.make_entry(competition, f"Home {competition.pk}-{PoolMatch.objects.count()}")
        away = self.make_entry(competition, f"Away {competition.pk}-{PoolMatch.objects.count()}")
        return PoolMatch.objects.create(
            competition=competition,
            stage=self.league_stage(competition),
            home_entry=home,
            away_entry=away,
            race_target=race_target,
            scheduled_at=timezone.now(),
        )

    @staticmethod
    def fixture_post_data(stage, home, away):
        return {
            "stage": str(stage.pk),
            "group": "",
            "home_entry": str(home.pk),
            "away_entry": str(away.pk),
            "scheduled_at": "",
            "race_target": "7",
            "round_label": "",
            "venue": "",
            "stream_url": "",
        }

    @staticmethod
    def player_registration_data(full_name, **overrides):
        values = {
            "full_name": full_name,
            "nickname": "",
            "region": "",
            "country": "",
            "gender": "",
            "contact_email": "",
            "contact_phone": "",
            "seed": "",
        }
        values.update(overrides)
        return values


class PoolPageNavigationMarkupTests(PoolManagerNavigationOperationsTestCase):
    def test_public_match_page_uses_reusable_back_and_pool_home_navigation(self):
        competition = self.make_competition()
        pool_match = self.make_match(competition)
        competition_url = reverse("pool:competition_detail", args=[competition.slug])

        response = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="pool-page-navigation__back"')
        self.assertContains(response, 'type="button"')
        self.assertContains(response, "data-pool-history-back")
        self.assertContains(response, f'data-pool-fallback="{competition_url}"')
        self.assertContains(response, ">Pool Home</a>")

    def test_manager_entry_page_uses_reusable_back_and_scoped_safe_fallback(self):
        competition = self.make_competition()
        self.client.force_login(self.owner)
        entries_url = reverse("pool:manage_competition_entries", args=[competition.slug])

        response = self.client.get(
            reverse("pool:manage_competition_entry_create", args=[competition.slug])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="pool-page-navigation__back"')
        self.assertContains(response, "data-pool-history-back")
        self.assertContains(response, f'data-pool-fallback="{entries_url}"')
        self.assertContains(response, 'class="pool-page-navigation__home" href="/pool/"')
        self.assertContains(response, ">Pool Home</a>")

    def test_competition_detail_has_back_and_distinct_pool_home_actions(self):
        competition = self.make_competition()

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="pool-page-navigation__back"')
        self.assertContains(response, 'data-pool-fallback="/pool/"')
        self.assertContains(response, 'class="pool-page-navigation__home" href="/pool/"')


class PoolManagerSaveAndAddAnotherTests(PoolManagerNavigationOperationsTestCase):
    def test_entry_normal_save_returns_to_the_competition_entry_list(self):
        competition = self.make_competition()
        player = PoolPlayer.objects.create(full_name="Entry Normal Save Player")
        url = reverse("pool:manage_competition_entry_create", args=[competition.slug])
        list_url = reverse(
            "pool:manage_competition_entries",
            args=[competition.slug],
        )
        self.client.force_login(self.owner)

        response = self.client.post(url, {"player": str(player.pk), "seed": ""})

        self.assertRedirects(response, list_url)
        self.assertTrue(
            PoolCompetitionEntry.objects.filter(
                competition=competition,
                player=player,
            ).exists()
        )

    def test_entry_save_and_add_another_creates_entry_and_invalid_post_stays_on_form(self):
        competition = self.make_competition()
        player = PoolPlayer.objects.create(full_name="Entry Add Another Player")
        url = reverse("pool:manage_competition_entry_create", args=[competition.slug])
        self.client.force_login(self.owner)

        response = self.client.post(
            url,
            {
                "player": str(player.pk),
                "seed": "",
                "save_and_add_another": "1",
            },
        )

        self.assertRedirects(response, url)
        self.assertTrue(
            PoolCompetitionEntry.objects.filter(
                competition=competition,
                player=player,
            ).exists()
        )

        entry_count = competition.entries.count()
        response = self.client.post(
            url,
            {"player": "", "seed": "", "save_and_add_another": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pool/manage/entry_form.html")
        self.assertEqual(competition.entries.count(), entry_count)

    def test_fixture_save_and_add_another_creates_fixture_and_invalid_post_stays_on_form(self):
        competition = self.make_competition()
        home = self.make_entry(competition, "Fixture Home")
        away = self.make_entry(competition, "Fixture Away")
        stage = self.league_stage(competition)
        url = reverse("pool:manage_competition_fixture_create", args=[competition.slug])
        self.client.force_login(self.owner)

        post_data = self.fixture_post_data(stage, home, away)
        post_data["save_and_add_another"] = "1"
        response = self.client.post(url, post_data)

        self.assertRedirects(response, url)
        self.assertEqual(PoolMatch.objects.filter(competition=competition).count(), 1)

        invalid_data = self.fixture_post_data(stage, home, home)
        invalid_data["save_and_add_another"] = "1"
        response = self.client.post(url, invalid_data)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pool/manage/fixture_form.html")
        self.assertEqual(PoolMatch.objects.filter(competition=competition).count(), 1)

    def test_fixture_normal_save_returns_to_the_competition_fixture_list(self):
        competition = self.make_competition()
        home = self.make_entry(competition, "Fixture Normal Home")
        away = self.make_entry(competition, "Fixture Normal Away")
        stage = self.league_stage(competition)
        url = reverse("pool:manage_competition_fixture_create", args=[competition.slug])
        list_url = reverse(
            "pool:manage_competition_fixtures",
            args=[competition.slug],
        )
        self.client.force_login(self.owner)

        response = self.client.post(url, self.fixture_post_data(stage, home, away))

        self.assertRedirects(response, list_url)
        self.assertEqual(PoolMatch.objects.filter(competition=competition).count(), 1)


class PoolManagerBulkWorkflowTests(PoolManagerNavigationOperationsTestCase):
    def test_register_new_player_uses_save_actions_and_save_add_another(self):
        competition = self.make_competition()
        registration_url = reverse(
            "pool:manage_competition_entry_register_player",
            args=[competition.slug],
        )
        self.client.force_login(self.owner)

        response = self.client.get(registration_url)

        self.assertContains(response, "Save player")
        self.assertContains(response, "Save &amp; add another")
        self.assertContains(response, "Select existing player")
        self.assertNotContains(response, "Check for existing players")

        response = self.client.post(
            registration_url,
            self.player_registration_data(
                "Register Add Another",
                save_mode="add_another",
            ),
        )

        self.assertRedirects(response, registration_url)
        player = PoolPlayer.objects.get(full_name="Register Add Another")
        self.assertTrue(
            PoolCompetitionEntry.objects.filter(
                competition=competition,
                player=player,
            ).exists()
        )

        entries_url = reverse(
            "pool:manage_competition_entries",
            args=[competition.slug],
        )
        response = self.client.post(
            registration_url,
            self.player_registration_data(
                "Register Normal Save",
                save_mode="save",
            ),
        )
        self.assertRedirects(response, entries_url)

    def test_duplicate_selection_preserves_save_add_another_mode(self):
        competition = self.make_competition()
        existing = PoolPlayer.objects.create(
            full_name="Duplicate Registration Player",
            region="Manzini",
        )
        registration_url = reverse(
            "pool:manage_competition_entry_register_player",
            args=[competition.slug],
        )
        self.client.force_login(self.owner)
        form_data = self.player_registration_data(
            existing.full_name,
            region=existing.region,
            save_mode="add_another",
        )

        response = self.client.post(registration_url, form_data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Possible existing players")
        self.assertContains(response, 'name="save_mode" value="add_another"')
        self.assertContains(response, "Use selected player")
        self.assertContains(response, "Save as new player")
        self.assertEqual(PoolPlayer.objects.filter(full_name=existing.full_name).count(), 1)

        form_data.update(
            {
                "existing_player": str(existing.pk),
                "action": "use_existing",
            }
        )
        response = self.client.post(registration_url, form_data)

        self.assertRedirects(response, registration_url)
        self.assertEqual(PoolPlayer.objects.filter(full_name=existing.full_name).count(), 1)
        self.assertTrue(
            PoolCompetitionEntry.objects.filter(
                competition=competition,
                player=existing,
            ).exists()
        )

        form_data.update(
            {
                "save_mode": "save",
                "action": "save_as_new",
            }
        )
        entries_url = reverse(
            "pool:manage_competition_entries",
            args=[competition.slug],
        )
        response = self.client.post(registration_url, form_data)

        self.assertRedirects(response, entries_url)
        self.assertEqual(PoolPlayer.objects.filter(full_name=existing.full_name).count(), 2)

    def test_failed_new_player_entry_rolls_back_the_new_player(self):
        competition = self.make_competition()
        registration_url = reverse(
            "pool:manage_competition_entry_register_player",
            args=[competition.slug],
        )
        self.client.force_login(self.owner)

        with patch(
            "pool.manager_views.PoolCompetitionEntry.objects.create",
            side_effect=IntegrityError("forced entry failure"),
        ):
            response = self.client.post(
                registration_url,
                self.player_registration_data("Rolled Back New Player"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            PoolPlayer.objects.filter(full_name="Rolled Back New Player").exists()
        )

    def test_existing_player_basket_adds_multiple_players_atomically(self):
        competition = self.make_competition()
        first = PoolPlayer.objects.create(full_name="Basket First")
        second = PoolPlayer.objects.create(full_name="Basket Second")
        entry_url = reverse("pool:manage_competition_entry_create", args=[competition.slug])
        entries_url = reverse("pool:manage_competition_entries", args=[competition.slug])
        self.client.force_login(self.owner)

        response = self.client.post(
            entry_url,
            {"player_ids": [str(first.pk), str(second.pk)]},
        )

        self.assertRedirects(response, entries_url)
        entries = PoolCompetitionEntry.objects.filter(
            competition=competition,
            player__in=[first, second],
        )
        self.assertEqual(entries.count(), 2)
        self.assertTrue(all(entry.seed is None for entry in entries))

        already_entered = first
        third = PoolPlayer.objects.create(full_name="Basket Third")
        entry_count = competition.entries.count()
        response = self.client.post(
            entry_url,
            {"player_ids": [str(already_entered.pk), str(third.pk)]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already entered")
        self.assertEqual(competition.entries.count(), entry_count)
        self.assertFalse(
            PoolCompetitionEntry.objects.filter(competition=competition, player=third).exists()
        )

    def test_existing_player_basket_rejects_invalid_or_inactive_players_without_partial_entry(self):
        competition = self.make_competition()
        valid_player = PoolPlayer.objects.create(full_name="Active Basket Player")
        inactive_player = PoolPlayer.objects.create(
            full_name="Inactive Basket Player",
            is_active=False,
        )
        entry_url = reverse("pool:manage_competition_entry_create", args=[competition.slug])
        self.client.force_login(self.owner)

        response = self.client.post(
            entry_url,
            {"player_ids": [str(valid_player.pk), str(inactive_player.pk)]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only active players")
        self.assertFalse(PoolCompetitionEntry.objects.filter(competition=competition).exists())

        response = self.client.post(
            entry_url,
            {"player_ids": [str(valid_player.pk), "999999999"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer exist")
        self.assertFalse(PoolCompetitionEntry.objects.filter(competition=competition).exists())

    def test_two_life_basket_uses_one_entry_per_real_player(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            allow_two_lives=True,
        )
        two_life_player = PoolPlayer.objects.create(full_name="Two Life Basket Player")
        one_life_player = PoolPlayer.objects.create(full_name="One Life Basket Player")
        entry_url = reverse("pool:manage_competition_entry_create", args=[competition.slug])
        entries_url = reverse("pool:manage_competition_entries", args=[competition.slug])
        self.client.force_login(self.owner)

        response = self.client.post(
            entry_url,
            {
                "player_ids": [str(two_life_player.pk), str(one_life_player.pk)],
                f"player_lives_{two_life_player.pk}": "2",
                f"player_lives_{one_life_player.pk}": "1",
            },
        )

        self.assertRedirects(response, entries_url)
        self.assertEqual(
            PoolCompetitionEntry.objects.filter(competition=competition).count(),
            2,
        )
        two_life_entry = PoolCompetitionEntry.objects.get(
            competition=competition,
            player=two_life_player,
        )
        one_life_entry = PoolCompetitionEntry.objects.get(
            competition=competition,
            player=one_life_player,
        )
        self.assertEqual(
            set(
                PoolCompetitionEntryLife.objects.filter(
                    competition_entry=two_life_entry
                ).values_list("life_number", flat=True)
            ),
            {1, 2},
        )
        self.assertEqual(
            list(
                PoolCompetitionEntryLife.objects.filter(
                    competition_entry=one_life_entry
                ).values_list("life_number", flat=True)
            ),
            [1],
        )

    def test_group_entry_basket_appends_sequential_order_and_rejects_invalid_batch(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        group_stage, _knockout_stage = self.group_and_knockout_stages(competition)
        group = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=2,
        )
        existing = self.make_entry(competition, "Existing Group Member")
        PoolGroupEntry.objects.create(
            stage=group_stage,
            group=group,
            entry=existing,
            sort_order=3,
        )
        first = self.make_entry(competition, "Group Basket First")
        second = self.make_entry(competition, "Group Basket Second")
        create_url = reverse(
            "pool:manage_competition_group_entry_create",
            args=[competition.slug, group_stage.pk, group.pk],
        )
        group_stage_url = reverse(
            "pool:manage_competition_group_stage",
            args=[competition.slug, group_stage.pk],
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            create_url,
            {"entry_ids": [str(first.pk), str(second.pk)]},
        )

        self.assertRedirects(response, group_stage_url)
        memberships = list(
            PoolGroupEntry.objects.filter(group=group).order_by("sort_order")
        )
        self.assertEqual([membership.sort_order for membership in memberships], [3, 4, 5])
        self.assertEqual(
            [membership.entry_id for membership in memberships],
            [existing.pk, first.pk, second.pk],
        )

        save_another_entry = self.make_entry(competition, "Group Save Another")
        response = self.client.post(
            create_url,
            {
                "entry_ids": [str(save_another_entry.pk)],
                "save_and_add_another": "1",
            },
        )
        self.assertRedirects(response, create_url)
        self.assertTrue(
            PoolGroupEntry.objects.filter(group=group, entry=save_another_entry).exists()
        )

        other_competition = self.make_competition()
        foreign_entry = self.make_entry(other_competition, "Foreign Group Entry")
        third = self.make_entry(competition, "Unassigned Group Entry")
        membership_count = PoolGroupEntry.objects.filter(group=group).count()
        response = self.client.post(
            create_url,
            {"entry_ids": [str(third.pk), str(foreign_entry.pk)]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must belong to this competition")
        self.assertEqual(PoolGroupEntry.objects.filter(group=group).count(), membership_count)
        self.assertFalse(PoolGroupEntry.objects.filter(group=group, entry=third).exists())

    def test_group_entry_basket_search_is_manager_scoped(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        group_stage, _knockout_stage = self.group_and_knockout_stages(competition)
        group = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=1,
        )
        entry = self.make_entry(competition, "Searchable Group Player")
        search_url = reverse(
            "pool:manage_competition_group_entry_search",
            args=[competition.slug, group_stage.pk, group.pk],
        )

        self.client.force_login(self.owner)
        response = self.client.get(search_url, {"q": "Searchable"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], entry.pk)

    def test_qualification_save_add_another_preserves_only_destination_stage(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        group_stage, knockout_stage = self.group_and_knockout_stages(competition)
        group = PoolGroup.objects.create(
            stage=group_stage,
            name="Group A",
            order=1,
            qualifiers=2,
        )
        for sort_order, name in enumerate(
            ("Qualification Group One", "Qualification Group Two"),
            start=1,
        ):
            PoolGroupEntry.objects.create(
                stage=group_stage,
                group=group,
                entry=self.make_entry(competition, name),
                sort_order=sort_order,
            )
        create_url = reverse(
            "pool:manage_competition_qualification_create",
            args=[competition.slug, group_stage.pk],
        )
        list_url = (
            reverse("pool:manage_competition_qualifications", args=[competition.slug])
            + f"?stage={group_stage.pk}"
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            create_url,
            {
                "group": str(group.pk),
                "knockout_stage": str(knockout_stage.pk),
                "position": "1",
                "destination_slot": "1",
            },
        )

        self.assertRedirects(response, list_url)
        self.assertTrue(
            PoolGroupQualificationRule.objects.filter(
                source_stage=group_stage,
                group=group,
                position=1,
            ).exists()
        )

        response = self.client.post(
            create_url,
            {
                "group": str(group.pk),
                "knockout_stage": str(knockout_stage.pk),
                "position": "2",
                "destination_slot": "2",
                "save_and_add_another": "1",
            },
        )

        expected_add_another_url = f"{create_url}?knockout_stage={knockout_stage.pk}"
        self.assertRedirects(response, expected_add_another_url)
        response = self.client.get(expected_add_another_url)
        self.assertEqual(response.context["form"].initial["knockout_stage"], knockout_stage.pk)
        self.assertNotIn("group", response.context["form"].initial)
        self.assertContains(response, "Save &amp; add another")

        response = self.client.post(
            create_url,
            {
                "group": str(group.pk),
                "knockout_stage": str(knockout_stage.pk),
                "position": "2",
                "destination_slot": "2",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pool/manage/qualification_form.html")


class PoolManagerInformationAndStateTests(PoolManagerNavigationOperationsTestCase):
    def test_superuser_admin_retains_explicit_completed_control(self):
        competition = self.make_competition()
        superuser = User.objects.create_superuser(
            username="pool-status-superuser",
            email="pool-status-superuser@example.test",
            password="test-password",
        )
        request = RequestFactory().get("/")
        request.user = superuser
        model_admin = PoolCompetitionAdmin(PoolCompetition, admin.site)

        self.assertTrue(model_admin.has_change_permission(request, competition))
        self.assertNotIn(
            "operational_state",
            model_admin.get_readonly_fields(request, competition),
        )
        competition.operational_state = PoolCompetition.OperationalState.COMPLETED
        competition.save()
        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.COMPLETED,
        )

    def test_information_form_omits_operational_state_and_ignores_crafted_value(self):
        competition = self.make_competition()
        url = reverse("pool:manage_competition_information", args=[competition.slug])
        self.client.force_login(self.owner)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("operational_state", response.context["form"].fields)

        response = self.client.post(
            url,
            {
                "name": competition.name,
                "slug": competition.slug,
                "description": "",
                "participant_mode": competition.participant_mode,
                "visibility": competition.visibility,
                "default_race_target": str(competition.default_race_target),
                "start_date": "",
                "end_date": "",
                "venue": "",
                "rules": "",
                "operational_state": PoolCompetition.OperationalState.COMPLETED,
            },
        )

        self.assertRedirects(response, url)
        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.UPCOMING,
        )

    def test_only_played_match_lifecycle_moves_upcoming_competition_to_active(self):
        competition = self.make_competition()
        non_played_match = self.make_match(competition)

        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.UPCOMING,
        )

        record_non_played_result(
            non_played_match,
            result_method=PoolMatch.ResultMethod.WALKOVER,
            winner=non_played_match.home_entry,
            recorded_by=self.owner,
        )
        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.UPCOMING,
        )

        played_match = self.make_match(competition, race_target=1)
        start_match(played_match, recorded_by=self.owner)
        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.ACTIVE,
        )

        record_rack(
            played_match,
            winner=played_match.home_entry,
            recorded_by=self.owner,
        )
        played_match.refresh_from_db()
        competition.refresh_from_db()
        self.assertEqual(played_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.ACTIVE,
        )


class PoolGroupOrderRegressionTests(PoolManagerNavigationOperationsTestCase):
    def make_group_stage(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage, _knockout_stage = self.group_and_knockout_stages(competition)
        return competition, stage

    def test_new_group_order_is_stage_scoped_and_edits_keep_their_order(self):
        _competition, stage = self.make_group_stage()
        self.assertEqual(PoolGroupForm(stage=stage).fields["order"].initial, 1)
        first = PoolGroup.objects.create(stage=stage, name="Group A", order=1)
        self.assertEqual(PoolGroupForm(stage=stage).fields["order"].initial, 2)
        PoolGroup.objects.create(stage=stage, name="Group B", order=2)
        self.assertEqual(PoolGroupForm(stage=stage).fields["order"].initial, 3)
        self.assertEqual(
            PoolGroupForm(stage=stage, instance=first).fields["order"].initial,
            None,
        )
        self.assertEqual(PoolGroupForm(stage=stage, instance=first).initial["order"], 1)

    def test_duplicate_group_values_are_form_errors_and_model_constraints_remain(self):
        competition, stage = self.make_group_stage()
        PoolGroup.objects.create(stage=stage, name="Group A", order=1)
        self.client.force_login(self.owner)
        url = reverse(
            "pool:manage_competition_group_create", args=[competition.slug, stage.pk]
        )
        duplicate_order = self.client.post(
            url, {"name": "Group B", "order": 1, "qualifiers": 2}
        )
        self.assertEqual(duplicate_order.status_code, 200)
        self.assertContains(duplicate_order, "A group with this order already exists")
        duplicate_name = self.client.post(
            url, {"name": "Group A", "order": 2, "qualifiers": 2}
        )
        self.assertEqual(duplicate_name.status_code, 200)
        self.assertContains(duplicate_name, "A group with this name already exists")
        with self.assertRaises(ValidationError):
            PoolGroup(stage=stage, name="Group A", order=3).full_clean()


class PoolManualFixtureGameweekTests(PoolManagerNavigationOperationsTestCase):
    def test_manual_league_gameweek_is_optional_and_persisted_without_generator_metadata(self):
        competition = self.make_competition()
        stage = self.league_stage(competition)
        home = self.make_entry(competition, "Manual Gameweek Home")
        away = self.make_entry(competition, "Manual Gameweek Away")
        data = self.fixture_post_data(stage, home, away)
        data["gameweek"] = "8"

        form = PoolFixtureForm(data, competition=competition)

        self.assertIn("gameweek", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        fixture = form.save()
        self.assertEqual(fixture.gameweek, 8)
        self.assertFalse(fixture.is_generated_league_fixture)
        self.assertIsNone(fixture.league_leg)
        edit_data = self.fixture_post_data(stage, home, away)
        edit_data["gameweek"] = "9"
        edit_form = PoolFixtureForm(edit_data, competition=competition, instance=fixture)
        self.assertTrue(edit_form.is_valid(), edit_form.errors)
        self.assertEqual(edit_form.save().gameweek, 9)

    def test_group_gameweek_is_allowed_and_knockout_gameweek_is_rejected_by_model(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        group_stage, knockout_stage = self.group_and_knockout_stages(competition)
        group = PoolGroup.objects.create(stage=group_stage, name="Group A", order=1)
        home = self.make_entry(competition, "Group Gameweek Home")
        away = self.make_entry(competition, "Group Gameweek Away")
        PoolGroupEntry.objects.create(stage=group_stage, group=group, entry=home)
        PoolGroupEntry.objects.create(stage=group_stage, group=group, entry=away)
        data = self.fixture_post_data(group_stage, home, away)
        data.update({"group": str(group.pk), "gameweek": "4"})
        form = PoolFixtureForm(data, competition=competition)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().gameweek, 4)
        invalid = PoolMatch(
            competition=competition,
            stage=knockout_stage,
            home_entry=home,
            away_entry=away,
            gameweek=1,
        )
        with self.assertRaises(ValidationError) as error:
            invalid.full_clean()
        self.assertIn("gameweek", error.exception.message_dict)

    def test_public_tabs_group_manual_and_generated_gameweeks_and_keep_unassigned_rows(self):
        competition = self.make_competition()
        stage = self.league_stage(competition)
        home = self.make_entry(competition, "Grouped Home")
        away = self.make_entry(competition, "Grouped Away")
        PoolMatch.objects.create(
            competition=competition, stage=stage, home_entry=home, away_entry=away,
            gameweek=8, scheduled_at=timezone.now(),
        )
        PoolMatch.objects.create(
            competition=competition, stage=stage, home_entry=away, away_entry=home,
            gameweek=8, league_leg=1, is_generated_league_fixture=True,
            scheduled_at=timezone.now(),
        )
        PoolMatch.objects.create(
            competition=competition, stage=stage, home_entry=home, away_entry=away,
            scheduled_at=timezone.now(),
        )

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )

        self.assertContains(response, "Gameweek 8", count=1)
        self.assertContains(response, "Other fixtures")
        self.client.force_login(self.owner)
        manager = self.client.get(
            reverse("pool:manage_competition_fixtures", args=[competition.slug])
        )
        self.assertContains(manager, "Gameweek")
        self.assertContains(manager, "GW 8")

    def test_public_results_tab_groups_completed_gameweeks_and_keeps_unassigned_rows(self):
        competition = self.make_competition()
        stage = self.league_stage(competition)
        home = self.make_entry(competition, "Results Home")
        away = self.make_entry(competition, "Results Away")
        gameweek_match = PoolMatch.objects.create(
            competition=competition, stage=stage, home_entry=home, away_entry=away,
            gameweek=3, scheduled_at=timezone.now(),
        )
        unassigned_match = PoolMatch.objects.create(
            competition=competition, stage=stage, home_entry=away, away_entry=home,
            scheduled_at=timezone.now(),
        )
        for fixture in (gameweek_match, unassigned_match):
            record_non_played_result(
                fixture,
                result_method=PoolMatch.ResultMethod.WALKOVER,
                winner=fixture.home_entry,
                recorded_by=self.owner,
            )

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=results"
        )

        self.assertContains(response, "Gameweek 3")
        self.assertContains(response, "Other fixtures")


class PoolGroupEntryLockingRegressionTests(PoolManagerNavigationOperationsTestCase):
    def test_group_entry_lock_queryset_locks_only_base_entry_rows(self):
        queryset = _locked_group_entries([1])

        self.assertTrue(queryset.query.select_for_update)
        self.assertFalse(queryset.query.select_related)

    def test_team_entry_can_be_assigned_through_the_group_basket(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        group_stage, _knockout_stage = self.group_and_knockout_stages(competition)
        group = PoolGroup.objects.create(stage=group_stage, name="Group A", order=1)
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Group Basket Team"),
        )
        url = reverse(
            "pool:manage_competition_group_entry_create",
            args=[competition.slug, group_stage.pk, group.pk],
        )
        self.client.force_login(self.owner)

        response = self.client.post(url, {"entry_ids": [str(entry.pk)]})

        self.assertRedirects(
            response,
            reverse(
                "pool:manage_competition_group_stage",
                args=[competition.slug, group_stage.pk],
            ),
        )
        self.assertTrue(
            PoolGroupEntry.objects.filter(
                stage=group_stage,
                group=group,
                entry=entry,
            ).exists()
        )


class PoolGroupKnockoutGamePresentationTests(PoolManagerNavigationOperationsTestCase):
    def make_group_fixture(self, competition, stage, group, suffix, game):
        home = self.make_entry(competition, f"{group.name} {suffix} Home")
        away = self.make_entry(competition, f"{group.name} {suffix} Away")
        PoolGroupEntry.objects.create(stage=stage, group=group, entry=home)
        PoolGroupEntry.objects.create(stage=stage, group=group, entry=away)
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            group=group,
            home_entry=home,
            away_entry=away,
            gameweek=game,
            scheduled_at=timezone.now(),
        )

    def test_group_knockout_rounds_share_game_number_and_public_grouping(self):
        competition = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        stage, _knockout_stage = self.group_and_knockout_stages(competition)
        group_a = PoolGroup.objects.create(stage=stage, name="Group A", order=1)
        group_b = PoolGroup.objects.create(stage=stage, name="Group B", order=2)
        first_a = self.make_group_fixture(competition, stage, group_a, "First", 1)
        first_b = self.make_group_fixture(competition, stage, group_b, "First", 1)
        second_a = self.make_group_fixture(competition, stage, group_a, "Second", 2)

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=fixtures"
        )

        self.assertEqual((first_a.gameweek, first_b.gameweek), (1, 1))
        self.assertContains(response, "Game 1", count=1)
        self.assertContains(response, "Game 2", count=1)
        self.assertContains(response, "Group A")
        self.assertContains(response, "Group B")
        self.assertNotContains(response, "Gameweek 1")
        self.client.force_login(self.owner)
        manager = self.client.get(
            reverse("pool:manage_competition_fixtures", args=[competition.slug])
        )
        self.assertContains(manager, "Game 1")

        for fixture in (first_a, first_b, second_a):
            record_non_played_result(
                fixture,
                result_method=PoolMatch.ResultMethod.WALKOVER,
                winner=fixture.home_entry,
                recorded_by=self.owner,
            )
        results = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]) + "?tab=results"
        )
        self.assertContains(results, "Game 1", count=1)
        self.assertContains(results, "Game 2", count=1)
        self.assertContains(results, "Group A")
        self.assertContains(results, "Group B")
