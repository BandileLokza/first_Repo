"""Public group-match context labels."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupEntry,
    PoolMatch,
    PoolPlayer,
)


User = get_user_model()


class PoolPublicGroupMatchLabelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="group-match-label-owner",
            password="test-password",
        )

    def make_group_match(self, group_name):
        competition = PoolCompetition.objects.create(
            name=f"{group_name} Label Competition",
            slug=group_name.lower().replace(" ", "-") + "-label-competition",
            owner=self.owner,
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE,
        )
        stage.stage_type = PoolCompetitionStage.StageType.GROUP
        stage.name = "Group stage"
        stage.save()
        group = PoolGroup.objects.create(
            stage=stage,
            name=group_name,
            order=1,
            qualifiers=1,
        )
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=f"{group_name} Home"),
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=f"{group_name} Away"),
        )
        for index, entry in enumerate((home_entry, away_entry)):
            PoolGroupEntry.objects.create(
                stage=stage,
                group=group,
                entry=entry,
                sort_order=index,
            )
        return competition, PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            group=group,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )

    def test_group_a_match_uses_the_actual_group_name_in_public_context(self):
        competition, pool_match = self.make_group_match("Group A")

        detail = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))
        competition_page = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug])
        )

        self.assertContains(detail, "Group stage · Group A")
        self.assertContains(competition_page, "Group stage · Group A")

    def test_group_b_match_uses_its_own_group_name(self):
        _competition, pool_match = self.make_group_match("Group B")

        response = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))

        self.assertContains(response, "Group stage · Group B")
        self.assertNotContains(response, "Group stage · Group A")

    def test_non_group_match_keeps_its_existing_stage_context(self):
        competition = PoolCompetition.objects.create(
            name="Ordinary league labels",
            slug="ordinary-league-labels",
            owner=self.owner,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE,
        )
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name="Ordinary Home"),
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name="Ordinary Away"),
        )
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )

        response = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))

        self.assertContains(response, "League")
        self.assertNotContains(response, "Group stage ·")
