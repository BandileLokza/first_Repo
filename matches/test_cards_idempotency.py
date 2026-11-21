"""Regression coverage for football disciplinary events and safe retries."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import LeagueMembership
from leagues.models import Competition, Stage
from matches.models import Goal, Match, Player, YellowCard
from teams.models import Team


User = get_user_model()


class CardAndGoalIdempotencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="card-controller", password="pass")
        self.competition = Competition.objects.create(name="Cards League", slug="cards-league", season="2026")
        LeagueMembership.objects.create(user=self.user, competition=self.competition, role=LeagueMembership.Role.ADMIN)
        self.stage = Stage.objects.create(competition=self.competition, name="League", stage_type="LEAGUE", order=1)
        self.home = Team.objects.create(name="Home Cards", competition=self.competition)
        self.away = Team.objects.create(name="Away Cards", competition=self.competition)
        self.unrelated = Team.objects.create(name="Unrelated Cards", competition=self.competition)
        self.match = Match.objects.create(
            competition=self.competition, stage=self.stage, home_team=self.home, away_team=self.away,
            status="LIVE", phase="FIRST_HALF", phase_start_time=timezone.now(), match_date=timezone.now(),
        )
        self.home_player = Player.objects.create(name="John Dlamini", team=self.home)
        self.away_player = Player.objects.create(name="Away Player", team=self.away)
        self.unrelated_player = Player.objects.create(name="Other Player", team=self.unrelated)
        self.client.force_login(self.user)

    def card_post(self, kind, *, team=None, player=None, player_name="", minute=20, token=None):
        return self.client.post(
            reverse("add_red_card" if kind == "red" else "add_yellow_card_control", args=[self.match.id]),
            {
                "team": (team or self.home).id,
                "player": player.id if player else "",
                "player_name": player_name,
                "minute": minute,
                "idempotency_key": str(token or uuid.uuid4()),
            },
        )

    def test_first_and_second_yellow_are_distinct_and_direct_red_remains_distinct(self):
        self.card_post("yellow", player=self.home_player, minute=27)
        first = YellowCard.objects.get()
        self.assertEqual(first.card_type, YellowCard.CardType.YELLOW)

        self.card_post("yellow", player=self.home_player, minute=64)
        second = YellowCard.objects.order_by("minute").last()
        self.assertEqual(second.card_type, YellowCard.CardType.SECOND_YELLOW_RED)

        self.card_post("red", team=self.away, player=self.away_player, minute=71)
        self.assertEqual(
            YellowCard.objects.get(player=self.away_player).card_type,
            YellowCard.CardType.DIRECT_RED,
        )

    def test_yellows_are_match_scoped_and_correction_or_cancellation_recalculates_second_yellow(self):
        first_response = self.card_post("yellow", player=self.home_player, minute=20)
        self.assertEqual(first_response.status_code, 302)
        self.card_post("yellow", player=self.home_player, minute=60)
        first, second = YellowCard.objects.order_by("minute")

        correction = self.client.post(reverse("edit_card", args=[first.id]), {
            "team": self.home.id,
            "player": self.home_player.id,
            "minute": 80,
            "idempotency_key": str(uuid.uuid4()),
        })
        self.assertEqual(correction.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.card_type, YellowCard.CardType.SECOND_YELLOW_RED)
        self.assertEqual(second.card_type, YellowCard.CardType.YELLOW)

        self.client.post(reverse("cancel_card", args=[second.id]))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(second.canceled)
        self.assertEqual(first.card_type, YellowCard.CardType.YELLOW)

        other_match = Match.objects.create(
            competition=self.competition, stage=self.stage, home_team=self.home, away_team=self.away,
            status="LIVE", phase="FIRST_HALF", phase_start_time=timezone.now(), match_date=timezone.now(), leg=2,
        )
        self.client.post(reverse("add_yellow_card_control", args=[other_match.id]), {
            "team": self.home.id, "player": self.home_player.id, "minute": 12,
            "idempotency_key": str(uuid.uuid4()),
        })
        self.assertEqual(YellowCard.objects.get(match=other_match).card_type, YellowCard.CardType.YELLOW)

    def test_cards_validate_match_teams_players_and_reuse_normalised_new_player(self):
        response = self.card_post("yellow", team=self.unrelated, player=self.unrelated_player)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(YellowCard.objects.exists())

        response = self.card_post("yellow", team=self.home, player=self.away_player)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(YellowCard.objects.exists())

        self.card_post("yellow", player_name="  jane   dlamini  ", minute=15)
        self.card_post("yellow", player_name="JANE DLAMINI", minute=35)
        player = Player.objects.get(team=self.home, name="jane dlamini")
        self.assertEqual(Player.objects.filter(team=self.home, name__iexact="jane dlamini").count(), 1)
        self.assertEqual(YellowCard.objects.filter(player=player).count(), 2)

    def test_blank_card_minute_uses_the_existing_live_clock(self):
        recorded_at = timezone.now().replace(second=0, microsecond=0)
        self.match.phase_start_time = recorded_at - timedelta(minutes=12)
        self.match.save(update_fields=["phase_start_time"])

        with patch("matches.models.timezone.now", return_value=recorded_at):
            response = self.client.post(
                reverse("add_yellow_card_control", args=[self.match.id]),
                {
                    "team": self.home.id,
                    "player": self.home_player.id,
                    "minute": "",
                    "idempotency_key": str(uuid.uuid4()),
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(YellowCard.objects.get().minute, 12)

    def test_card_and_goal_retries_with_the_same_token_create_one_event_and_one_new_player(self):
        card_token = uuid.uuid4()
        card_data = {
            "team": self.home.id, "player": self.home_player.id, "minute": 30,
            "idempotency_key": str(card_token),
        }
        url = reverse("add_yellow_card_control", args=[self.match.id])
        self.client.post(url, card_data)
        self.client.post(url, card_data)
        self.assertEqual(YellowCard.objects.filter(idempotency_key=card_token).count(), 1)

        goal_token = uuid.uuid4()
        goal_data = {"player": "", "player_name": "Retry Goal Scorer", "goal_type": Goal.GoalType.NORMAL, "idempotency_key": str(goal_token)}
        goal_url = reverse("add_goal", args=[self.match.id, self.home.id])
        self.client.post(goal_url, goal_data)
        self.client.post(goal_url, goal_data)
        self.assertEqual(Goal.objects.filter(idempotency_key=goal_token).count(), 1)
        self.assertEqual(Player.objects.filter(team=self.home, name__iexact="retry goal scorer").count(), 1)
        self.match.refresh_from_db()
        self.assertEqual(self.match.home_score, 1)

    def test_separate_goal_submission_keys_remain_separate_legitimate_events(self):
        url = reverse("add_goal", args=[self.match.id, self.home.id])
        for token in (uuid.uuid4(), uuid.uuid4()):
            self.client.post(url, {"player": self.home_player.id, "goal_type": Goal.GoalType.NORMAL, "idempotency_key": str(token)})
        self.assertEqual(Goal.objects.filter(match=self.match).count(), 2)

    def test_failed_goal_write_rolls_back_its_player_and_leaves_the_token_reusable(self):
        token = uuid.uuid4()
        data = {
            "player": "",
            "player_name": "Atomic Retry Scorer",
            "goal_type": Goal.GoalType.NORMAL,
            "idempotency_key": str(token),
        }
        url = reverse("add_goal", args=[self.match.id, self.home.id])
        with patch("matches.views.Goal.objects.create", side_effect=ValidationError("No goal")):
            with self.assertRaises(ValidationError):
                self.client.post(url, data)
        self.assertFalse(Goal.objects.filter(idempotency_key=token).exists())
        self.assertFalse(Player.objects.filter(name="Atomic Retry Scorer").exists())

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Goal.objects.filter(idempotency_key=token).count(), 1)
        self.assertEqual(Player.objects.filter(name="Atomic Retry Scorer").count(), 1)

    def test_database_uniqueness_is_per_event_model_not_per_match_or_event_type(self):
        token = uuid.uuid4()
        self.card_post("yellow", player=self.home_player, token=token)
        card = YellowCard.objects.get(idempotency_key=token)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                YellowCard.objects.bulk_create([YellowCard(
                    match=self.match,
                    team=self.home,
                    player=self.home_player,
                    minute=43,
                    idempotency_key=token,
                )])

        # Goal and card submissions intentionally have independent token spaces.
        goal_url = reverse("add_goal", args=[self.match.id, self.home.id])
        self.client.post(goal_url, {
            "player": self.home_player.id,
            "goal_type": Goal.GoalType.NORMAL,
            "idempotency_key": str(token),
        })
        goal = Goal.objects.get(idempotency_key=token)
        self.assertEqual(card.idempotency_key, goal.idempotency_key)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Goal.objects.bulk_create([Goal(
                    match=self.match,
                    team=self.home,
                    player=self.home_player,
                    player_name=self.home_player.name,
                    goal_type=Goal.GoalType.NORMAL,
                    idempotency_key=token,
                )])
