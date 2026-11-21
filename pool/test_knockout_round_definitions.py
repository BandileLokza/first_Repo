"""Regression coverage for independent manually drawn knockout rounds."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .forms import PoolFixtureForm, PoolKnockoutRoundConfigurationForm
from .brackets import (
    PoolBracketValidationError,
    create_handoff_draw_preview,
    generate_bracket_from_preview,
    handoff_field_for_round,
)
from .knockout_rounds import configure_knockout_rounds, knockout_round_structure
from .models import (
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolKnockoutBracket,
    PoolKnockoutRoundDefinition,
    PoolMatch,
    PoolPlayer,
)


User = get_user_model()


class PoolKnockoutRoundDefinitionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="round-definition-owner",
            password="test-password",
        )
        self._number = 0

    def make_competition(self, **overrides):
        self._number += 1
        values = {
            "name": f"Round definition event {self._number}",
            "slug": f"round-definition-event-{self._number}",
            "owner": self.owner,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.KNOCKOUT,
            "operational_state": PoolCompetition.OperationalState.UPCOMING,
            "default_race_target": 5,
        }
        values.update(overrides)
        competition = PoolCompetition.objects.create(**values)
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        )
        return competition, stage

    def make_entries(self, competition, names):
        return [
            PoolCompetitionEntry.objects.create(
                competition=competition,
                player=PoolPlayer.objects.create(full_name=name),
            )
            for name in names
        ]

    def test_canonical_round_structure_uses_next_supported_power_of_two(self):
        expected_names = {
            2: ["Final"],
            3: ["Semi-final", "Final"],
            4: ["Semi-final", "Final"],
            5: ["Quarter-final", "Semi-final", "Final"],
            8: ["Quarter-final", "Semi-final", "Final"],
            9: ["Round of 16", "Quarter-final", "Semi-final", "Final"],
            16: ["Round of 16", "Quarter-final", "Semi-final", "Final"],
            20: [
                "Round of 32",
                "Round of 16",
                "Quarter-final",
                "Semi-final",
                "Final",
            ],
            64: [
                "Round of 64",
                "Round of 32",
                "Round of 16",
                "Quarter-final",
                "Semi-final",
                "Final",
            ],
            128: [
                "Round of 128",
                "Round of 64",
                "Round of 32",
                "Round of 16",
                "Quarter-final",
                "Semi-final",
                "Final",
            ],
            256: [
                "Round of 256",
                "Round of 128",
                "Round of 64",
                "Round of 32",
                "Round of 16",
                "Quarter-final",
                "Semi-final",
                "Final",
            ],
        }
        for count, names in expected_names.items():
            with self.subTest(count=count):
                self.assertEqual(
                    [name for _order, _size, name in knockout_round_structure(count)],
                    names,
                )
        for invalid_count in (0, 1, -2, 257):
            with self.subTest(invalid_count=invalid_count):
                with self.assertRaises(ValidationError):
                    knockout_round_structure(invalid_count)

    def test_manager_configures_rounds_and_uses_dynamic_competitor_wording(self):
        competition, stage = self.make_competition()
        self.client.force_login(self.owner)
        url = reverse("pool:manage_competition_knockout_rounds", args=[competition.slug])

        response = self.client.get(url)
        self.assertContains(response, "Number of players")
        self.assertContains(response, "Rounds")

        response = self.client.post(
            url,
            {"stage": stage.pk, "competitor_count": 20},
        )
        self.assertRedirects(response, f"{url}?stage={stage.pk}")
        self.assertEqual(
            list(
                PoolKnockoutRoundDefinition.objects.filter(stage=stage).values_list(
                    "name", flat=True
                )
            ),
            [
                "Round of 32",
                "Round of 16",
                "Quarter-final",
                "Semi-final",
                "Final",
            ],
        )

        team_competition, _team_stage = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        self.assertEqual(
            PoolKnockoutRoundConfigurationForm(
                competition=team_competition
            ).fields["competitor_count"].label,
            "Number of teams",
        )
        lives_competition, _lives_stage = self.make_competition(allow_two_lives=True)
        self.assertEqual(
            PoolKnockoutRoundConfigurationForm(
                competition=lives_competition
            ).fields["competitor_count"].label,
            "Number of tournament lives",
        )

    def test_round_reconfiguration_is_safe_until_a_manual_fixture_references_it(self):
        competition, stage = self.make_competition()
        first = configure_knockout_rounds(stage, 16, actor=self.owner)
        first_ids = {definition.pk for definition in first}

        second = configure_knockout_rounds(stage, 20, actor=self.owner)
        self.assertFalse(
            PoolKnockoutRoundDefinition.objects.filter(pk__in=first_ids).exists()
        )
        self.assertEqual(second[0].name, "Round of 32")

        home, away = self.make_entries(competition, ["Reconfigure A", "Reconfigure B"])
        PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            knockout_round=second[0],
            home_entry=home,
            away_entry=away,
            race_target=5,
        )
        with self.assertRaises(ValidationError):
            configure_knockout_rounds(stage, 8, actor=self.owner)

    def test_manual_fixture_round_is_scoped_persisted_and_displayed(self):
        competition, stage = self.make_competition()
        definitions = configure_knockout_rounds(stage, 8, actor=self.owner)
        quarter_final = definitions[0]
        home, away = self.make_entries(competition, ["Manual A", "Manual B"])
        self.client.force_login(self.owner)
        create_url = reverse("pool:manage_competition_fixture_create", args=[competition.slug])

        response = self.client.post(
            create_url,
            {
                "stage": stage.pk,
                "group": "",
                "knockout_round": quarter_final.pk,
                "home_entry": home.pk,
                "away_entry": away.pk,
                "scheduled_at": "",
                "race_target": 5,
                "venue": "",
                "stream_url": "",
            },
        )
        self.assertRedirects(
            response,
            reverse("pool:manage_competition_fixtures", args=[competition.slug]),
        )
        pool_match = PoolMatch.objects.get(competition=competition)
        self.assertEqual(pool_match.knockout_round_id, quarter_final.pk)
        self.assertEqual(pool_match.round_label, "Quarter-final")

        self.assertContains(
            self.client.get(reverse("pool:competition_detail", args=[competition.slug])),
            "Quarter-final",
        )
        self.assertContains(
            self.client.get(reverse("pool:match_detail", args=[pool_match.pk])),
            "Quarter-final",
        )
        self.assertContains(
            self.client.get(
                reverse("pool:manage_competition_fixtures", args=[competition.slug])
            ),
            "Quarter-final",
        )

        legacy = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=away,
            away_entry=home,
            race_target=5,
            round_label="Legacy final",
        )
        self.assertEqual(legacy.display_round_name, "Legacy final")

    def test_cross_stage_round_manipulation_is_rejected_and_league_fixture_is_unchanged(self):
        competition, knockout_stage = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
        )
        league_stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        round_definition = configure_knockout_rounds(knockout_stage, 4)[0]
        home, away = self.make_entries(competition, ["League A", "League B"])
        form = PoolFixtureForm(
            {
                "stage": league_stage.pk,
                "group": "",
                "knockout_round": round_definition.pk,
                "home_entry": home.pk,
                "away_entry": away.pk,
                "scheduled_at": "",
                "race_target": 5,
                "round_label": "",
                "venue": "",
                "stream_url": "",
            },
            competition=competition,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("stage", form.errors)

        other_competition, other_stage = self.make_competition()
        other_definition = configure_knockout_rounds(other_stage, 4)[0]
        with self.assertRaises(ValidationError):
            PoolMatch.objects.create(
                competition=competition,
                stage=knockout_stage,
                knockout_round=other_definition,
                home_entry=home,
                away_entry=away,
                race_target=5,
            )

    def test_two_life_manual_fixture_uses_life_identity(self):
        competition, stage = self.make_competition(allow_two_lives=True)
        definition = configure_knockout_rounds(stage, 4)[0]
        home, away = self.make_entries(competition, ["Life Home", "Life Away"])
        home_life_two = PoolCompetitionEntryLife.objects.create(
            competition_entry=home,
            life_number=2,
        )
        away_life_one = PoolCompetitionEntryLife.objects.get(
            competition_entry=away,
            life_number=1,
        )
        form = PoolFixtureForm(
            {
                "stage": stage.pk,
                "group": "",
                "knockout_round": definition.pk,
                "home_entry_life": home_life_two.pk,
                "away_entry_life": away_life_one.pk,
                "scheduled_at": "",
                "race_target": 5,
                "venue": "",
                "stream_url": "",
            },
            competition=competition,
        )
        self.assertTrue(form.is_valid(), form.errors)
        pool_match = form.save(commit=False)
        pool_match.competition = competition
        pool_match.save()
        self.assertEqual(pool_match.home_entry_id, home.pk)
        self.assertEqual(pool_match.away_entry_id, away.pk)
        self.assertEqual(pool_match.home_entry_life_id, home_life_two.pk)
        self.assertEqual(pool_match.away_entry_life_id, away_life_one.pk)
        self.assertIn(
            "Life Home - Life 2",
            [str(life) for life in form.fields["home_entry_life"].queryset],
        )

    def test_handoff_provenance_is_stage_scoped_without_enabling_handoff(self):
        competition, stage = self.make_competition()
        definition = configure_knockout_rounds(stage, 4)[0]
        preview = PoolBracketDrawPreview.objects.create(
            competition=competition,
            stage=stage,
            handoff_from_round=definition,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
        )
        self.assertEqual(preview.handoff_from_round_id, definition.pk)

    def test_completed_manual_round_can_handoff_without_rewriting_history(self):
        competition, stage = self.make_competition()
        quarter_final = configure_knockout_rounds(stage, 8, actor=self.owner)[0]
        entries = self.make_entries(
            competition,
            [f"Handoff {number}" for number in range(1, 9)],
        )
        for seed, entry in enumerate(entries, start=1):
            entry.seed = seed
            entry.save()
        source_match_ids = []
        for index in range(0, 8, 2):
            match = PoolMatch.objects.create(
                competition=competition,
                stage=stage,
                knockout_round=quarter_final,
                home_entry=entries[index],
                away_entry=entries[index + 1],
                race_target=5,
                status=PoolMatch.Status.FINISHED,
                winner=entries[index],
                result_method=PoolMatch.ResultMethod.WALKOVER,
            )
            source_match_ids.append(match.pk)

        field = handoff_field_for_round(
            competition,
            stage=stage,
            source_round=quarter_final,
        )
        self.assertEqual(field["bracket_size"], 4)
        self.assertEqual(set(field["entry_ids"]), {entry.pk for entry in entries[::2]})
        preview = create_handoff_draw_preview(
            competition,
            stage=stage,
            source_round=quarter_final,
            actor=self.owner,
        )
        self.assertEqual(preview.handoff_from_round_id, quarter_final.pk)
        bracket = generate_bracket_from_preview(preview, actor=self.owner)
        self.assertEqual(bracket.handoff_from_round_id, quarter_final.pk)
        self.assertEqual(bracket.size, 4)
        self.assertEqual(
            list(
                PoolMatch.objects.filter(pk__in=source_match_ids)
                .order_by("pk")
                .values_list("pk", flat=True)
            ),
            source_match_ids,
        )
        self.assertEqual(
            PoolKnockoutBracket.objects.get(pk=bracket.pk).handoff_from_round_id,
            quarter_final.pk,
        )

        source = PoolMatch.objects.get(pk=source_match_ids[0])
        source.venue = "Attempted historic rewrite"
        with self.assertRaises(ValidationError):
            source.save()

    def test_handoff_rejects_incomplete_or_later_manual_round_activity(self):
        competition, stage = self.make_competition()
        definitions = configure_knockout_rounds(stage, 8, actor=self.owner)
        quarter_final, semi_final = definitions[:2]
        entries = self.make_entries(
            competition,
            [f"Incomplete {number}" for number in range(1, 9)],
        )
        # Three completed quarter-finals are not the structural four required
        # by the selected Round of 8 / quarter-final definition.
        for index in range(0, 6, 2):
            PoolMatch.objects.create(
                competition=competition,
                stage=stage,
                knockout_round=quarter_final,
                home_entry=entries[index],
                away_entry=entries[index + 1],
                race_target=5,
                status=PoolMatch.Status.FINISHED,
                winner=entries[index],
                result_method=PoolMatch.ResultMethod.WALKOVER,
            )
        with self.assertRaises(PoolBracketValidationError):
            handoff_field_for_round(
                competition,
                stage=stage,
                source_round=quarter_final,
            )

        # A downstream manual fixture must never be replaced by a handoff.
        PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            knockout_round=semi_final,
            home_entry=entries[0],
            away_entry=entries[2],
            race_target=5,
        )
        with self.assertRaises(PoolBracketValidationError):
            handoff_field_for_round(
                competition,
                stage=stage,
                source_round=quarter_final,
            )
