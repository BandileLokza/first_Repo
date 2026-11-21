"""Regression coverage for post-draw direct-knockout BYE registration."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .brackets import (
    PoolBracketValidationError,
    _write_trusted_late_bye_slot,
    create_draw_preview,
    generate_bracket_from_preview,
    generate_random_bracket,
    place_late_entry_in_open_bye,
    validate_bracket,
)
from .models import (
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolPlayer,
)
from .services import start_match


User = get_user_model()


class LateByeEntryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="late-bye-owner",
            password="test-password",
        )
        self._competition_number = 0
        self._player_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        defaults = {
            "name": f"Late BYE Competition {self._competition_number}",
            "slug": f"late-bye-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.KNOCKOUT,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "default_race_target": 2,
        }
        defaults.update(overrides)
        return PoolCompetition.objects.create(**defaults)

    def make_entry(self, competition, name):
        self._player_number += 1
        return PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=name),
        )

    @staticmethod
    def stage_for(competition):
        return competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        )

    def manual_bracket(self, competition, draw_order, *, life_order=None):
        preview = create_draw_preview(
            competition,
            stage=self.stage_for(competition),
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=len(draw_order),
            manual_entry_ids=None if life_order else draw_order,
            manual_life_ids=life_order,
            actor=self.owner,
        )
        return generate_bracket_from_preview(preview, actor=self.owner)

    @staticmethod
    def first_round_tie(bracket, position):
        return PoolKnockoutTie.objects.get(
            bracket=bracket,
            round_number=1,
            is_third_place=False,
            position=position,
        )

    def test_late_player_fills_open_bye_without_moving_existing_entrants(self):
        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
        ]
        bracket = self.manual_bracket(
            competition,
            [alpha.pk, None, bravo.pk, charlie.pk, delta.pk, None, echo.pk, None],
        )
        original_order = list(bracket.draw_order)
        late_player = PoolPlayer.objects.create(full_name="Late Entrant")

        late_entry = place_late_entry_in_open_bye(
            competition,
            player=late_player,
            actor=self.owner,
        )

        bracket.refresh_from_db()
        self.assertEqual(bracket.size, 8)
        self.assertEqual(
            bracket.draw_order,
            [alpha.pk, late_entry.pk, bravo.pk, charlie.pk, delta.pk, None, echo.pk, None],
        )
        self.assertEqual(original_order[0], bracket.draw_order[0])
        first_tie = self.first_round_tie(bracket, 1)
        self.assertIsNotNone(first_tie.match_id)
        self.assertEqual(
            {first_tie.match.home_entry_id, first_tie.match.away_entry_id},
            {alpha.pk, late_entry.pk},
        )
        self.assertEqual(late_entry.seed, None)

    def test_late_entry_retracts_only_an_untouched_provisional_bye_advance(self):
        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo, foxtrot = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")
        ]
        bracket = self.manual_bracket(
            competition,
            [
                alpha.pk,
                None,
                bravo.pk,
                None,
                charlie.pk,
                delta.pk,
                echo.pk,
                foxtrot.pk,
            ],
        )
        round_two = PoolKnockoutTie.objects.get(
            bracket=bracket,
            round_number=2,
            position=1,
            is_third_place=False,
        )
        original_round_two_match_id = round_two.match_id
        self.assertIsNotNone(original_round_two_match_id)

        late_entry = place_late_entry_in_open_bye(
            competition,
            player=PoolPlayer.objects.create(full_name="Late Reconciliation"),
            actor=self.owner,
        )

        round_two.refresh_from_db()
        self.assertIsNone(round_two.match_id)
        self.assertFalse(PoolMatch.objects.filter(pk=original_round_two_match_id).exists())
        reopened = self.first_round_tie(bracket, 1)
        self.assertEqual(
            {reopened.match.home_entry_id, reopened.match.away_entry_id},
            {alpha.pk, late_entry.pk},
        )

    def test_started_downstream_fixture_seals_its_bye_and_rolls_back_entry(self):
        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo, foxtrot = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")
        ]
        bracket = self.manual_bracket(
            competition,
            [
                alpha.pk,
                None,
                bravo.pk,
                None,
                charlie.pk,
                delta.pk,
                echo.pk,
                foxtrot.pk,
            ],
        )
        round_two = PoolKnockoutTie.objects.get(
            bracket=bracket,
            round_number=2,
            position=1,
            is_third_place=False,
        )
        start_match(round_two.match, recorded_by=self.owner)
        late_player = PoolPlayer.objects.create(full_name="Blocked Late Entrant")

        with self.assertRaises(PoolBracketValidationError):
            place_late_entry_in_open_bye(
                competition,
                player=late_player,
                actor=self.owner,
            )

        self.assertFalse(
            PoolCompetitionEntry.objects.filter(
                competition=competition,
                player=late_player,
            ).exists()
        )

    def test_two_life_late_entry_uses_two_open_byes_in_opposite_halves(self):
        competition = self.make_competition(allow_two_lives=True)
        entries = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie")
        ]
        for entry in entries:
            PoolCompetitionEntryLife.objects.create(
                competition_entry=entry,
                life_number=2,
            )
        life_order = [
            entries[0].lives.get(life_number=1).pk,
            None,
            entries[1].lives.get(life_number=1).pk,
            entries[2].lives.get(life_number=1).pk,
            entries[0].lives.get(life_number=2).pk,
            None,
            entries[1].lives.get(life_number=2).pk,
            entries[2].lives.get(life_number=2).pk,
        ]
        bracket = self.manual_bracket(
            competition,
            [None] * 8,
            life_order=life_order,
        )

        entry = place_late_entry_in_open_bye(
            competition,
            player=PoolPlayer.objects.create(full_name="Two Life Late Entrant"),
            requested_lives=2,
            actor=self.owner,
        )

        lives = list(entry.lives.order_by("life_number"))
        self.assertEqual([life.life_number for life in lives], [1, 2])
        bracket.refresh_from_db()
        positions = [
            index + 1
            for index, life_id in enumerate(bracket.life_draw_order)
            if life_id in {life.pk for life in lives}
        ]
        self.assertEqual(len(positions), 2)
        life_positions = {
            life_id: index + 1
            for index, life_id in enumerate(bracket.life_draw_order)
            if life_id in {life.pk for life in lives}
        }
        self.assertLessEqual(life_positions[lives[0].pk], 4)
        self.assertGreater(life_positions[lives[1].pk], 4)

    def test_trusted_late_bye_write_rejects_a_slot_changed_concurrently(self):
        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
        ]
        bracket = self.manual_bracket(
            competition,
            [alpha.pk, None, bravo.pk, charlie.pk, delta.pk, None, echo.pk, None],
        )
        tie = self.first_round_tie(bracket, 1)
        bye_slot = tie.slots.get(source_type=PoolKnockoutTieSlot.SourceType.BYE)
        # Use an existing competition entrant only to exercise the helper's
        # conditional protection; a normal late registration never reaches
        # this changed source state.
        PoolKnockoutTieSlot.objects.filter(pk=bye_slot.pk).update(
            source_type=PoolKnockoutTieSlot.SourceType.TBD
        )

        with self.assertRaises(PoolBracketValidationError):
            _write_trusted_late_bye_slot(
                bye_slot,
                tie=tie,
                entry=alpha,
            )

    def test_late_bye_path_does_not_call_generic_slot_save(self):
        from unittest.mock import patch

        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
        ]
        bracket = self.manual_bracket(
            competition,
            [alpha.pk, None, bravo.pk, charlie.pk, delta.pk, None, echo.pk, None],
        )
        original_bye_slot_id = self.first_round_tie(
            bracket, 1
        ).slots.get(source_type=PoolKnockoutTieSlot.SourceType.BYE).pk
        original_save = PoolKnockoutTieSlot.save
        generic_save_slot_ids = []

        def record_generic_save(instance, *args, **kwargs):
            generic_save_slot_ids.append(instance.pk)
            return original_save(instance, *args, **kwargs)

        with patch.object(
            PoolKnockoutTieSlot,
            "save",
            autospec=True,
            side_effect=record_generic_save,
        ):
            late_entry = place_late_entry_in_open_bye(
                competition,
                player=PoolPlayer.objects.create(full_name="Fast Late Entrant"),
                actor=self.owner,
            )

        self.assertTrue(late_entry.pk)
        self.assertNotIn(original_bye_slot_id, generic_save_slot_ids)

    def test_two_life_draw_methods_keep_life_one_and_life_two_in_fixed_halves(self):
        competition = self.make_competition(allow_two_lives=True)
        entries = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie")
        ]
        for seed, entry in enumerate(entries, start=1):
            entry.seed = seed
            entry.save()
            PoolCompetitionEntryLife.objects.create(
                competition_entry=entry,
                life_number=2,
            )

        lives = list(
            PoolCompetitionEntryLife.objects.filter(
                competition_entry__competition=competition
            ).select_related("competition_entry")
        )
        life_by_id = {life.pk: life for life in lives}
        stage = self.stage_for(competition)

        seeded = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.SEEDED,
            bracket_size=8,
            actor=self.owner,
        )
        for position, life_id in enumerate(seeded.life_draw_order, start=1):
            if life_id:
                self.assertEqual(
                    life_by_id[life_id].life_number,
                    1 if position <= 4 else 2,
                )

        # The same strict invariant is rechecked from persisted topology, not
        # just from the preview/form boundary.
        bracket = generate_bracket_from_preview(seeded, actor=self.owner)
        validate_bracket(bracket)

        # A separate random competition verifies that randomisation remains
        # inside its life-number half and is not a sibling-only shuffle.
        random_competition = self.make_competition(allow_two_lives=True)
        random_entries = [
            self.make_entry(random_competition, name)
            for name in ("Delta", "Echo", "Foxtrot")
        ]
        for entry in random_entries:
            PoolCompetitionEntryLife.objects.create(
                competition_entry=entry,
                life_number=2,
            )
        random_bracket = generate_random_bracket(
            random_competition,
            stage=self.stage_for(random_competition),
            bracket_size=8,
            actor=self.owner,
            confirmed=True,
        )
        random_life_numbers = {
            life.pk: life.life_number
            for life in PoolCompetitionEntryLife.objects.filter(
                competition_entry__competition=random_competition
            )
        }
        for position, life_id in enumerate(random_bracket.life_draw_order, start=1):
            if life_id:
                self.assertEqual(
                    random_life_numbers[life_id],
                    1 if position <= 4 else 2,
                )

    def test_manual_two_life_draw_rejects_a_life_in_the_wrong_half(self):
        competition = self.make_competition(allow_two_lives=True)
        entries = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie")
        ]
        for entry in entries:
            PoolCompetitionEntryLife.objects.create(
                competition_entry=entry,
                life_number=2,
            )
        life_one = [entry.lives.get(life_number=1).pk for entry in entries]
        life_two = [entry.lives.get(life_number=2).pk for entry in entries]

        with self.assertRaises(PoolBracketValidationError):
            create_draw_preview(
                competition,
                stage=self.stage_for(competition),
                draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
                bracket_size=8,
                manual_life_ids=[
                    life_two[0],
                    life_one[0],
                    life_one[1],
                    life_one[2],
                    life_two[1],
                    None,
                    life_two[2],
                    None,
                ],
                actor=self.owner,
            )

    def test_entry_create_http_uses_late_bye_path(self):
        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
        ]
        self.manual_bracket(
            competition,
            [alpha.pk, None, bravo.pk, charlie.pk, delta.pk, None, echo.pk, None],
        )
        player = PoolPlayer.objects.create(full_name="HTTP Late Entrant")
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("pool:manage_competition_entry_create", args=[competition.slug]),
            {"player_ids": str(player.pk)},
        )

        self.assertRedirects(
            response,
            reverse("pool:manage_competition_entries", args=[competition.slug]),
        )
        self.assertTrue(
            PoolCompetitionEntry.objects.filter(
                competition=competition,
                player=player,
            ).exists()
        )

    def test_normal_direct_creation_remains_frozen_after_bracket(self):
        competition = self.make_competition()
        alpha, bravo, charlie, delta, echo = [
            self.make_entry(competition, name)
            for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
        ]
        self.manual_bracket(
            competition,
            [alpha.pk, None, bravo.pk, charlie.pk, delta.pk, None, echo.pk, None],
        )
        with self.assertRaises(ValidationError):
            PoolCompetitionEntry.objects.create(
                competition=competition,
                player=PoolPlayer.objects.create(full_name="Unsafe Direct Late Entrant"),
            )

