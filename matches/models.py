from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.db.models import Q, F
from leagues.models import Competition, Stage, Group
from teams.models import Team
from teams.storage_backends import SupabaseStorage
from django.contrib.auth.models import User
from leagues.models import Standing
from django.core.exceptions import ValidationError
from django.db import transaction


SCHEDULE_LOCKED_FIELDS = {
    "competition",
    "competition_id",
    "stage",
    "stage_id",
    "group",
    "group_id",
    "home_team",
    "home_team_id",
    "away_team",
    "away_team_id",
    "gameweek",
    "leg",
    "knockout_tie",
    "knockout_tie_id",
}

SCHEDULE_LOCKED_FIELD_LABELS = {
    "competition_id": "competition",
    "stage_id": "stage",
    "group_id": "group",
    "home_team_id": "home team",
    "away_team_id": "away team",
    "gameweek": "gameweek",
    "leg": "leg",
    "knockout_tie_id": "knockout tie",
}


class FootballKnockoutTieQuerySet(models.QuerySet):
    """Keep published bracket relationships out of bulk-write bypasses."""

    STRUCTURAL_FIELDS = {
        "stage",
        "stage_id",
        "position",
        "team_one",
        "team_one_id",
        "team_two",
        "team_two_id",
        "is_bye",
        "winner_destination",
        "winner_destination_id",
        "winner_destination_slot",
    }

    def _assert_not_locked(self, fields=(), *, object_ids=None):
        if fields and not self.STRUCTURAL_FIELDS.intersection(fields):
            return
        locked = self.filter(stage__competition__schedule_locked_at__isnull=False)
        if object_ids is not None:
            locked = locked.filter(pk__in=object_ids)
        if locked.exists():
            raise ValidationError(
                "This competition's schedule is locked. Bracket structure cannot be changed."
            )

        if not hasattr(fields, "get"):
            return
        target = None
        if "stage_id" in fields:
            target = fields["stage_id"]
        elif "stage" in fields:
            target = fields["stage"]
        if target is not None:
            target_id = getattr(target, "pk", target)
            if not isinstance(target_id, int) or Stage.objects.filter(
                pk=target_id,
                competition__schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Bracket relationships cannot be moved into a locked competition."
                )

    def update(self, **kwargs):
        self._assert_not_locked(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        self._assert_not_locked(fields, object_ids=[obj.pk for obj in objs if obj.pk])
        if "stage" in fields or "stage_id" in fields:
            stage_ids = {obj.stage_id for obj in objs if obj.stage_id is not None}
            if Stage.objects.filter(
                pk__in=stage_ids,
                competition__schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Bracket relationships cannot be moved into a locked competition."
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        objs = list(objs)
        stage_ids = {obj.stage_id for obj in objs if obj.stage_id is not None}
        if Stage.objects.filter(
            pk__in=stage_ids,
            competition__schedule_locked_at__isnull=False,
        ).exists():
            raise ValidationError(
                "Bracket relationships cannot be created in a locked competition."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        self._assert_not_locked()
        return super().delete()


class FootballKnockoutTieSlotQuerySet(models.QuerySet):
    """Block bulk changes to published bracket entrant slots."""

    STRUCTURAL_FIELDS = {"tie", "tie_id", "slot", "team", "team_id", "is_bye"}

    def _assert_not_locked(self, fields=(), *, object_ids=None):
        if fields and not self.STRUCTURAL_FIELDS.intersection(fields):
            return
        locked = self.filter(tie__stage__competition__schedule_locked_at__isnull=False)
        if object_ids is not None:
            locked = locked.filter(pk__in=object_ids)
        if locked.exists():
            raise ValidationError(
                "This competition's schedule is locked. Bracket entrant slots cannot be changed."
            )

        if not hasattr(fields, "get"):
            return
        target = None
        if "tie_id" in fields:
            target = fields["tie_id"]
        elif "tie" in fields:
            target = fields["tie"]
        if target is not None:
            target_id = getattr(target, "pk", target)
            if not isinstance(target_id, int) or FootballKnockoutTie.objects.filter(
                pk=target_id,
                stage__competition__schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Bracket entrant slots cannot be moved into a locked competition."
                )

    def update(self, **kwargs):
        self._assert_not_locked(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        self._assert_not_locked(fields, object_ids=[obj.pk for obj in objs if obj.pk])
        if "tie" in fields or "tie_id" in fields:
            tie_ids = {obj.tie_id for obj in objs if obj.tie_id is not None}
            if FootballKnockoutTie.objects.filter(
                pk__in=tie_ids,
                stage__competition__schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Bracket entrant slots cannot be moved into a locked competition."
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        objs = list(objs)
        tie_ids = {obj.tie_id for obj in objs if obj.tie_id is not None}
        if FootballKnockoutTie.objects.filter(
            pk__in=tie_ids,
            stage__competition__schedule_locked_at__isnull=False,
        ).exists():
            raise ValidationError(
                "Bracket entrant slots cannot be created in a locked competition."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        self._assert_not_locked()
        return super().delete()


class FootballKnockoutRoundRuleQuerySet(models.QuerySet):
    """Prevent bulk edits from changing locked knockout round rules."""

    STRUCTURAL_FIELDS = {"stage", "stage_id", "number_of_legs", "decider_policy"}

    def _assert_not_locked(self, fields=(), *, object_ids=None):
        if fields and not self.STRUCTURAL_FIELDS.intersection(fields):
            return
        locked = self.filter(stage__competition__schedule_locked_at__isnull=False)
        if object_ids is not None:
            locked = locked.filter(pk__in=object_ids)
        if locked.exists():
            raise ValidationError(
                "This competition's schedule is locked. Knockout round rules cannot be changed."
            )
        if not hasattr(fields, "get"):
            return
        target = fields.get("stage_id", fields.get("stage"))
        if target is not None:
            target_id = getattr(target, "pk", target)
            if not isinstance(target_id, int) or Stage.objects.filter(
                pk=target_id,
                competition__schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Knockout round rules cannot be moved into a locked competition."
                )

    def update(self, **kwargs):
        self._assert_not_locked(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        self._assert_not_locked(fields, object_ids=[obj.pk for obj in objs if obj.pk])
        if "stage" in fields or "stage_id" in fields:
            stage_ids = {obj.stage_id for obj in objs if obj.stage_id is not None}
            if Stage.objects.filter(
                pk__in=stage_ids,
                competition__schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Knockout round rules cannot be moved into a locked competition."
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        objs = list(objs)
        stage_ids = {obj.stage_id for obj in objs if obj.stage_id is not None}
        if Stage.objects.filter(
            pk__in=stage_ids,
            competition__schedule_locked_at__isnull=False,
        ).exists():
            raise ValidationError(
                "Knockout round rules cannot be created in a locked competition."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        self._assert_not_locked()
        return super().delete()


class FootballKnockoutRoundRule(models.Model):
    """Football-specific deciding rules for one knockout stage."""

    class Legs(models.IntegerChoices):
        ONE = 1, "Single leg"
        TWO = 2, "Two legs"

    class DeciderPolicy(models.TextChoices):
        EXTRA_TIME_THEN_PENALTIES = (
            "EXTRA_TIME_THEN_PENALTIES",
            "Extra time, then penalties",
        )
        DIRECT_TO_PENALTIES = (
            "DIRECT_TO_PENALTIES",
            "Directly to penalties",
        )

    stage = models.OneToOneField(
        Stage,
        on_delete=models.CASCADE,
        related_name="football_knockout_rule",
    )
    number_of_legs = models.PositiveSmallIntegerField(
        choices=Legs.choices,
        default=Legs.ONE,
    )
    decider_policy = models.CharField(
        max_length=32,
        choices=DeciderPolicy.choices,
        default=DeciderPolicy.EXTRA_TIME_THEN_PENALTIES,
    )

    objects = FootballKnockoutRoundRuleQuerySet.as_manager()

    def clean(self):
        errors = {}
        if self.stage_id and self.stage.stage_type != "KNOCKOUT":
            errors["stage"] = "Knockout rules can only be attached to a knockout stage."

        original = None
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "stage_id",
                "number_of_legs",
                "decider_policy",
            ).first()
        affected_stage_ids = {
            stage_id
            for stage_id in (
                self.stage_id,
                original["stage_id"] if original else None,
            )
            if stage_id
        }
        schedule_locked = Stage.objects.filter(
            pk__in=affected_stage_ids,
            competition__schedule_locked_at__isnull=False,
        ).exists()
        if schedule_locked:
            if not self.pk:
                errors["stage"] = (
                    "This competition's schedule is locked. Knockout round rules cannot be added."
                )
            else:
                if original and any(
                    original[field] != getattr(self, field)
                    for field in ("stage_id", "number_of_legs", "decider_policy")
                ):
                    errors["stage"] = (
                        "This competition's schedule is locked. Knockout round rules cannot be changed."
                    )

        # A first-time rule can be added to a legacy round that has already
        # started, but it must be compatible with its existing fixture legs.
        if self.stage_id and self.number_of_legs:
            if Match.objects.filter(
                stage_id=self.stage_id,
                leg__gt=self.number_of_legs,
            ).exists():
                errors["number_of_legs"] = (
                    "Existing fixtures use a higher leg number."
                )

        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "stage_id",
                "number_of_legs",
                "decider_policy",
            ).first()
            if original:
                if original["stage_id"] != self.stage_id and Match.objects.filter(
                    stage_id=original["stage_id"],
                ).exists():
                    errors["stage"] = (
                        "A round rule cannot move after fixtures exist in its round."
                    )

                rules_changed = (
                    original["number_of_legs"] != self.number_of_legs
                    or original["decider_policy"] != self.decider_policy
                )
                legs_changed = (
                    original["number_of_legs"] != self.number_of_legs
                )
                affected_stage_ids = {
                    original["stage_id"],
                    self.stage_id,
                }
                if legs_changed and Match.objects.filter(
                    stage_id__in=affected_stage_ids,
                    knockout_tie__position__isnull=False,
                ).exists():
                    errors["number_of_legs"] = (
                        "Round leg configuration is locked once connected bracket "
                        "fixtures exist."
                    )
                elif rules_changed and Match.objects.filter(
                    stage_id__in=affected_stage_ids,
                ).exclude(status="UPCOMING").exists():
                    errors["number_of_legs"] = (
                        "Round rules are locked once a fixture has started or finished."
                    )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.stage}: {self.get_number_of_legs_display()}"


class FootballKnockoutTie(models.Model):
    """A football pairing that may contain one or two match legs."""

    class DestinationSlot(models.TextChoices):
        HOME = "HOME", "Home slot"
        AWAY = "AWAY", "Away slot"

    class WinnerResolutionMethod(models.TextChoices):
        REGULATION = "REGULATION", "Regulation"
        AGGREGATE = "AGGREGATE", "Aggregate"
        EXTRA_TIME = "EXTRA_TIME", "Extra time"
        PENALTIES = "PENALTIES", "Penalties"
        BYE = "BYE", "Bye"

    stage = models.ForeignKey(
        Stage,
        on_delete=models.CASCADE,
        related_name="football_knockout_ties",
    )
    # Legacy Phase One ties intentionally have no bracket position.  A
    # position marks a tie managed by the connected Phase Two bracket.
    position = models.PositiveIntegerField(null=True, blank=True)
    team_one = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="football_knockout_ties_as_team_one",
    )
    team_two = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="football_knockout_ties_as_team_two",
    )
    winner = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="football_knockout_ties_won",
    )
    winner_resolution_method = models.CharField(
        max_length=20,
        choices=WinnerResolutionMethod.choices,
        blank=True,
        default="",
    )
    # A bye is deliberately represented on the tie and its empty bracket slot;
    # it never creates a fake Match or a fake finished result.
    is_bye = models.BooleanField(default=False)
    # Fixture times are configured before generation.  Keeping them on the tie
    # lets a future round be scheduled without inventing a Match or a kickoff.
    leg_one_scheduled_for = models.DateTimeField(null=True, blank=True)
    leg_two_scheduled_for = models.DateTimeField(null=True, blank=True)
    winner_destination = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="winner_sources",
    )
    winner_destination_slot = models.CharField(
        max_length=4,
        choices=DestinationSlot.choices,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = FootballKnockoutTieQuerySet.as_manager()

    class Meta:
        ordering = ["stage__order", "position", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(team_one=F("team_two")),
                name="football_tie_teams_differ",
            ),
            models.UniqueConstraint(
                fields=["stage", "team_one", "team_two"],
                name="unique_football_tie_pairing",
            ),
            models.UniqueConstraint(
                fields=["stage", "position"],
                condition=Q(position__isnull=False),
                name="unique_bracket_tie_position_per_stage",
            ),
            models.CheckConstraint(
                condition=(
                    Q(winner_destination__isnull=True)
                    | ~Q(pk=F("winner_destination"))
                ),
                name="football_tie_cannot_feed_itself",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        winner_destination__isnull=True,
                        winner_destination_slot="",
                    )
                    | Q(
                        winner_destination__isnull=False,
                        winner_destination_slot__in=[
                            "HOME",
                            "AWAY",
                        ],
                    )
                ),
                name="football_tie_destination_slot_valid",
            ),
            models.UniqueConstraint(
                fields=["winner_destination", "winner_destination_slot"],
                condition=Q(winner_destination__isnull=False),
                name="unique_source_for_bracket_destination_slot",
            ),
        ]

    def _canonicalize_teams(self):
        """Keep stored pairings independent of home/away ordering."""
        if (
            self.team_one_id
            and self.team_two_id
            and self.team_one_id > self.team_two_id
        ):
            self.team_one_id, self.team_two_id = (
                self.team_two_id,
                self.team_one_id,
            )
            self._state.fields_cache.pop("team_one", None)
            self._state.fields_cache.pop("team_two", None)

    def clean(self):
        self._canonicalize_teams()
        errors = {}
        if self.stage_id and self.stage.stage_type != "KNOCKOUT":
            errors["stage"] = "A football knockout tie requires a knockout stage."

        for field_name in ("team_one", "team_two"):
            team = getattr(self, field_name, None)
            if team and self.stage_id and team.competition_id != self.stage.competition_id:
                errors[field_name] = (
                    "Both tie participants must belong to the stage competition."
                )

        if self.team_one_id and self.team_two_id and self.team_one_id == self.team_two_id:
            errors["team_two"] = "A team cannot play itself."

        if self.winner_id and self.stage_id:
            if self.winner.competition_id != self.stage.competition_id:
                errors["winner"] = (
                    "The resolved winner must belong to the stage competition."
                )
            participant_ids = {
                team_id
                for team_id in (self.team_one_id, self.team_two_id)
                if team_id
            }
            if not participant_ids or self.winner_id not in participant_ids:
                errors["winner"] = "The resolved winner must be a tie participant."

        if self.winner_id and not self.winner_resolution_method:
            errors["winner_resolution_method"] = (
                "Record how a resolved knockout tie was decided."
            )
        elif not self.winner_id and self.winner_resolution_method:
            errors["winner_resolution_method"] = (
                "A resolution method requires a resolved tie winner."
            )

        if self.is_bye and self.team_one_id and self.team_two_id:
            errors["is_bye"] = "A bye tie cannot have two participating teams."
        if self.is_bye and not (self.team_one_id or self.team_two_id):
            errors["is_bye"] = "A bye tie requires exactly one participating team."
        if (
            self.winner_resolution_method == self.WinnerResolutionMethod.BYE
            and not self.is_bye
        ):
            errors["winner_resolution_method"] = (
                "Only an explicit bye tie can resolve by bye."
            )

        if self.stage_id:
            try:
                round_rule = self.stage.football_knockout_rule
            except FootballKnockoutRoundRule.DoesNotExist:
                round_rule = None
            if (
                round_rule
                and round_rule.number_of_legs == FootballKnockoutRoundRule.Legs.ONE
                and self.leg_two_scheduled_for
            ):
                errors["leg_two_scheduled_for"] = (
                    "A single-leg round cannot have a second-leg kickoff."
                )
        if (
            self.leg_one_scheduled_for
            and self.leg_two_scheduled_for
            and self.leg_two_scheduled_for <= self.leg_one_scheduled_for
        ):
            errors["leg_two_scheduled_for"] = (
                "The second leg must be scheduled after the first leg."
            )

        if self.winner_destination_id:
            destination = self.winner_destination
            if destination.pk == self.pk:
                errors["winner_destination"] = "A tie cannot feed itself."
            elif (
                not self.stage_id
                or destination.stage.competition_id != self.stage.competition_id
            ):
                errors["winner_destination"] = (
                    "Bracket links must stay within one competition."
                )
            elif destination.stage.stage_type != "KNOCKOUT":
                errors["winner_destination"] = (
                    "A tie winner can only advance to a knockout tie."
                )
            elif destination.stage.order <= self.stage.order:
                errors["winner_destination"] = (
                    "A tie can only advance to a later knockout round."
                )

            if self.winner_destination_slot not in {
                self.DestinationSlot.HOME,
                self.DestinationSlot.AWAY,
            }:
                errors["winner_destination_slot"] = (
                    "Select the destination home or away slot."
                )
        elif self.winner_destination_slot:
            errors["winner_destination_slot"] = (
                "A destination slot requires a destination tie."
            )

        if (
            self.pk
            and self.matches.exists()
            and not getattr(self, "_allow_schedule_locked_bracket_progression", False)
        ):
            original = type(self).objects.filter(pk=self.pk).values(
                "stage_id",
                "team_one_id",
                "team_two_id",
            ).first()
            if original:
                original_pair = {
                    original["team_one_id"],
                    original["team_two_id"],
                }
                current_pair = {self.team_one_id, self.team_two_id}
                if (
                    original["stage_id"] != self.stage_id
                    or original_pair != current_pair
                ):
                    errors["stage"] = (
                        "The stage and teams of a tie cannot change after fixtures "
                        "are linked to it."
                    )

        schedule_changes = self._schedule_locked_structure_changes()
        if schedule_changes and not (
            getattr(self, "_allow_schedule_locked_bracket_progression", False)
            and self._is_valid_schedule_locked_bracket_progression(schedule_changes)
        ):
            errors["stage"] = (
                "This competition's schedule is locked. Bracket structure cannot be changed."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_progression = kwargs.pop(
            "_allow_schedule_locked_bracket_progression",
            False,
        )
        # Canonical ordering makes a pairing unique regardless of home/away order.
        self._canonicalize_teams()
        self._allow_schedule_locked_bracket_progression = allow_progression
        try:
            self.full_clean()
            return super().save(*args, **kwargs)
        finally:
            delattr(self, "_allow_schedule_locked_bracket_progression")

    def delete(self, *args, **kwargs):
        if self.stage_id and self.stage.competition.schedule_locked_at:
            raise ValidationError(
                "This competition's schedule is locked. Bracket relationships cannot be deleted."
            )
        return super().delete(*args, **kwargs)

    def _schedule_locked_structure_changes(self):
        """Return bracket-structure changes affecting a locked competition."""

        if not self.stage_id:
            return []
        target_is_locked = Stage.objects.filter(
            pk=self.stage_id,
            competition__schedule_locked_at__isnull=False,
        ).exists()
        fields = (
            "stage_id",
            "position",
            "team_one_id",
            "team_two_id",
            "is_bye",
            "winner_destination_id",
            "winner_destination_slot",
        )
        if not self.pk:
            return ["__new__"] if target_is_locked else []
        existing = type(self)._base_manager.filter(pk=self.pk).values(*fields).first()
        if not existing:
            return []
        source_is_locked = Stage.objects.filter(
            pk=existing["stage_id"],
            competition__schedule_locked_at__isnull=False,
        ).exists()
        if not source_is_locked and not target_is_locked:
            return []
        return [field for field in fields if existing[field] != getattr(self, field)]

    def _is_valid_schedule_locked_bracket_progression(self, changes):
        """Allow only a completed upstream tie to fill its exact destination."""

        if not self.pk or self.position is None:
            return False
        if not changes or set(changes) - {"team_one_id", "team_two_id"}:
            return False
        slots = {
            slot.slot: slot.team_id
            for slot in self.bracket_slots.select_related("team").all()
        }
        expected_team_ids = {team_id for team_id in slots.values() if team_id}
        if {team_id for team_id in (self.team_one_id, self.team_two_id) if team_id} != expected_team_ids:
            return False
        source_winners = {
            source.winner_destination_slot: source.winner_id
            for source in type(self).objects.filter(
                winner_destination_id=self.pk,
                winner_id__isnull=False,
            )
        }
        return all(
            not team_id or source_winners.get(slot_name) == team_id
            for slot_name, team_id in slots.items()
        )

    def __str__(self):
        return f"{self.stage.name}: {self.team_one or 'TBD'} vs {self.team_two or 'TBD'}"


class FootballKnockoutTieSlot(models.Model):
    """The explicit home/away slots used by a connected bracket tie."""

    class Slot(models.TextChoices):
        HOME = "HOME", "Home slot"
        AWAY = "AWAY", "Away slot"

    tie = models.ForeignKey(
        FootballKnockoutTie,
        on_delete=models.CASCADE,
        related_name="bracket_slots",
    )
    slot = models.CharField(max_length=4, choices=Slot.choices)
    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="football_knockout_bracket_slots",
    )
    is_bye = models.BooleanField(default=False)

    objects = FootballKnockoutTieSlotQuerySet.as_manager()

    class Meta:
        ordering = ["tie__stage__order", "tie__position", "slot"]
        constraints = [
            models.UniqueConstraint(
                fields=["tie", "slot"],
                name="unique_slot_per_football_bracket_tie",
            ),
            models.CheckConstraint(
                condition=Q(is_bye=False) | Q(team__isnull=True),
                name="football_bracket_bye_slot_has_no_team",
            ),
        ]

    def clean(self):
        errors = {}
        if self.tie_id:
            if self.tie.stage.stage_type != "KNOCKOUT":
                errors["tie"] = "Bracket slots require a knockout tie."

            if self.is_bye and self.tie.position is None:
                errors["is_bye"] = "Only bracket-managed ties may contain byes."

            if self.team_id:
                if self.team.competition_id != self.tie.stage.competition_id:
                    errors["team"] = (
                        "A bracket entrant must belong to the stage competition."
                    )
                elif type(self).objects.filter(
                    tie__stage=self.tie.stage,
                    team_id=self.team_id,
                ).exclude(pk=self.pk).exists():
                    errors["team"] = (
                        "A team can only occupy one slot in the same knockout round."
                    )

        if self.is_bye and self.team_id:
            errors["is_bye"] = "A bye slot cannot also contain a team."

        schedule_changes = self._schedule_locked_structure_changes()
        if schedule_changes and not (
            getattr(self, "_allow_schedule_locked_bracket_progression", False)
            and self._is_valid_schedule_locked_bracket_progression(schedule_changes)
        ):
            errors["tie"] = (
                "This competition's schedule is locked. Bracket entrant slots cannot be changed."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        allow_progression = kwargs.pop(
            "_allow_schedule_locked_bracket_progression",
            False,
        )
        self._allow_schedule_locked_bracket_progression = allow_progression
        try:
            self.full_clean()
            return super().save(*args, **kwargs)
        finally:
            delattr(self, "_allow_schedule_locked_bracket_progression")

    def delete(self, *args, **kwargs):
        if self.tie_id and self.tie.stage.competition.schedule_locked_at:
            raise ValidationError(
                "This competition's schedule is locked. Bracket entrant slots cannot be deleted."
            )
        return super().delete(*args, **kwargs)

    def _schedule_locked_structure_changes(self):
        if not self.tie_id:
            return []
        target_is_locked = FootballKnockoutTie.objects.filter(
            pk=self.tie_id,
            stage__competition__schedule_locked_at__isnull=False,
        ).exists()
        fields = ("tie_id", "slot", "team_id", "is_bye")
        if not self.pk:
            return ["__new__"] if target_is_locked else []
        existing = type(self)._base_manager.filter(pk=self.pk).values(*fields).first()
        if not existing:
            return []
        source_is_locked = FootballKnockoutTie.objects.filter(
            pk=existing["tie_id"],
            stage__competition__schedule_locked_at__isnull=False,
        ).exists()
        if not source_is_locked and not target_is_locked:
            return []
        return [field for field in fields if existing[field] != getattr(self, field)]

    def _is_valid_schedule_locked_bracket_progression(self, changes):
        """Permit a resolved source tie to populate exactly its destination slot."""

        if not self.pk or self.tie.position is None:
            return False
        if set(changes) - {"team_id", "is_bye"}:
            return False
        existing = type(self)._base_manager.filter(pk=self.pk).values(
            "team_id",
            "is_bye",
        ).first()
        if not existing or existing["team_id"] or existing["is_bye"]:
            return False
        if not self.team_id or self.is_bye:
            return False
        return FootballKnockoutTie.objects.filter(
            winner_destination_id=self.tie_id,
            winner_destination_slot=self.slot,
            winner_id=self.team_id,
        ).exists()

    def __str__(self):
        return f"{self.tie} - {self.get_slot_display()}"


class MatchQuerySet(models.QuerySet):
    """Guard structural fields on generated fixtures in bulk write paths."""

    GENERATED_LOCKED_FIELDS = {
        "competition",
        "competition_id",
        "stage",
        "stage_id",
        "group",
        "group_id",
        "home_team",
        "home_team_id",
        "away_team",
        "away_team_id",
        "gameweek",
        "leg",
        "knockout_tie",
        "knockout_tie_id",
        "generated_by_system",
    }

    def _assert_generated_fixtures_are_not_rewritten(self, fields, *, object_ids=None):
        generated = self.filter(generated_by_system=True)
        if object_ids is not None:
            generated = generated.filter(pk__in=object_ids)
        if self.GENERATED_LOCKED_FIELDS.intersection(fields) and generated.exists():
            raise ValidationError(
                "Generated fixtures keep their assigned teams, round and bracket position. "
                "Only scheduling details may be changed."
            )

    def _assert_locked_schedules_are_not_rewritten(self, fields, *, object_ids=None):
        """Block bulk structural writes to a published competition schedule."""

        if not SCHEDULE_LOCKED_FIELDS.intersection(fields):
            return
        locked = self.filter(competition__schedule_locked_at__isnull=False)
        if object_ids is not None:
            locked = locked.filter(pk__in=object_ids)
        if locked.exists():
            raise ValidationError(
                "This competition's schedule is locked. Only match date, time and venue "
                "may be changed through ordinary fixture management."
            )

        # Moving an unlocked row into a locked competition is also a structural
        # change to that published schedule. Expression-based target values
        # cannot be safely inspected, so reject them rather than guessing.
        if not hasattr(fields, "get"):
            return
        target = fields.get("competition_id", fields.get("competition"))
        if target is not None:
            target_id = getattr(target, "pk", target)
            if not isinstance(target_id, int) or Competition.objects.filter(
                pk=target_id,
                schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Fixtures cannot be moved into a locked competition schedule."
                )

    def update(self, **kwargs):
        self._assert_generated_fixtures_are_not_rewritten(kwargs)
        self._assert_locked_schedules_are_not_rewritten(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        object_ids = [obj.pk for obj in objs if obj.pk]
        self._assert_generated_fixtures_are_not_rewritten(
            fields,
            object_ids=object_ids,
        )
        self._assert_locked_schedules_are_not_rewritten(
            fields,
            object_ids=object_ids,
        )
        if "competition" in fields or "competition_id" in fields:
            target_competition_ids = {
                obj.competition_id
                for obj in objs
                if obj.competition_id is not None
            }
            if Competition.objects.filter(
                pk__in=target_competition_ids,
                schedule_locked_at__isnull=False,
            ).exists():
                raise ValidationError(
                    "Fixtures cannot be moved into a locked competition schedule."
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        # bulk_create bypasses Match.save(), so it needs the same new-fixture
        # protection explicitly. Trusted bracket progression creates matches
        # one at a time through its narrow private model-save exception.
        competition_ids = {
            obj.competition_id for obj in objs if obj.competition_id is not None
        }
        if Competition.objects.filter(
            pk__in=competition_ids,
            schedule_locked_at__isnull=False,
        ).exists():
            raise ValidationError(
                "Fixtures cannot be created in a locked competition schedule."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class Match(models.Model):

    STATUS_CHOICES = [
        ('UPCOMING', 'Upcoming'),
        ('LIVE', 'Live'),
        ('FINISHED', 'Finished'),
        ('ABANDONED', 'Abandoned'),
    ]

    PHASE_CHOICES = [
        ('NONE', 'None'),
        ('FIRST_HALF', 'First Half'),
        ('BREAK', 'Break'),
        ('SECOND_HALF', 'Second Half'),
        ('REGULATION_ENDED', 'Regulation Ended'),
        ('EXTRA_FIRST_HALF', 'Extra Time First Half'),
        ('EXTRA_BREAK', 'Extra Time Break'),
        ('EXTRA_SECOND_HALF', 'Extra Time Second Half'),
        ('EXTRA_ENDED', 'Extra Time Ended'),
        ('PENALTIES', 'Penalty Shootout'),
    ]

    RESOLUTION_CHOICES = [
        ('', 'Not decided'),
        ('REGULATION', 'Regulation'),
        ('AGGREGATE', 'Aggregate'),
        ('EXTRA_TIME', 'Extra Time'),
        ('PENALTIES', 'Penalties'),
    ]

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name='matches'
    )

    stage = models.ForeignKey(
        Stage,
        on_delete=models.CASCADE,
        related_name='matches'
    )

    group = models.ForeignKey(
        Group,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='matches'
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(home_team=F("away_team")),
                name="prevent_same_team_match"
            ),
            models.CheckConstraint(
                condition=Q(home_score__gte=0),
                name="home_score_positive"
            ),
            models.CheckConstraint(
                condition=Q(away_score__gte=0),
                name="away_score_positive"
            ),
            # A replay deliberately has the same sporting identity as the
            # abandoned fixture it replaces.  Keep the normal fixture unique
            # while allowing that explicitly linked exception.
            models.UniqueConstraint(
                fields=["competition", "stage", "home_team", "away_team", "leg"],
                condition=Q(replay_of__isnull=True),
                name="unique_primary_match_per_leg",
            ),
            models.UniqueConstraint(
                fields=["replay_of"],
                condition=Q(status__in=["UPCOMING", "LIVE"]),
                name="unique_active_replay_per_source",
            ),
            models.UniqueConstraint(
                fields=["knockout_tie", "leg"],
                condition=Q(knockout_tie__isnull=False),
                name="unique_match_leg_per_knockout_tie",
            ),
            models.CheckConstraint(
                condition=Q(regulation_home_score__isnull=True)
                | Q(regulation_home_score__gte=0),
                name="regulation_home_score_positive",
            ),
            models.CheckConstraint(
                condition=Q(regulation_away_score__isnull=True)
                | Q(regulation_away_score__gte=0),
                name="regulation_away_score_positive",
            ),
            models.CheckConstraint(
                condition=Q(penalty_home_score__isnull=True)
                | Q(penalty_home_score__gte=0),
                name="penalty_home_score_positive",
            ),
            models.CheckConstraint(
                condition=Q(penalty_away_score__isnull=True)
                | Q(penalty_away_score__gte=0),
                name="penalty_away_score_positive",
            ),
        ]
        ordering = ["-match_date"]

    leg = models.IntegerField(default=1)
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_matches')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_matches')

    home_score = models.IntegerField(default=0)
    away_score = models.IntegerField(default=0)

    # Normal football goals after 90 minutes.  Extra-time goals stay in
    # home_score/away_score; shootout kicks are always separate.
    regulation_home_score = models.IntegerField(null=True, blank=True)
    regulation_away_score = models.IntegerField(null=True, blank=True)
    penalty_home_score = models.IntegerField(null=True, blank=True)
    penalty_away_score = models.IntegerField(null=True, blank=True)
    resolution_method = models.CharField(
        max_length=20,
        choices=RESOLUTION_CHOICES,
        blank=True,
        default="",
    )

    knockout_tie = models.ForeignKey(
        FootballKnockoutTie,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matches",
    )

    # A replay is a new sporting fixture, never a rewrite of its abandoned
    # source.  PROTECT retains the original audit trail when replacement
    # fixtures are being reviewed or deleted.
    replay_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="replays",
        editable=False,
    )

    # League fixture generation deliberately creates pairings before an
    # administrator has selected a kickoff.  Existing scheduled matches keep
    # their date; null is only used for genuinely unscheduled fixtures.
    match_date = models.DateTimeField(null=True, blank=True)

    # A matchday is optional for legacy/manual fixtures, but is always set by
    # the TABLE league round-robin generator.
    gameweek = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    # Explicit provenance prevents the application from guessing whether an
    # old fixture was generated. Existing rows intentionally remain False on
    # migration; only fixtures created by a system generator are locked.
    generated_by_system = models.BooleanField(default=False, db_index=True, editable=False)

    location = models.CharField(max_length=200, blank=True, null=True)
    abandon_reason = models.TextField(blank=True, null=True)

    class AbandonmentResolution(models.TextChoices):
        NONE = "", "Awaiting official resolution"
        REPLAY = "REPLAY", "Full replay ordered"
        RESUMED = "RESUMED", "Resumption approved"
        AWARDED = "AWARDED", "Official result awarded"
        CONFIRMED = "CONFIRMED", "Score at abandonment confirmed"
        VOID = "VOID", "Match voided"

    # These fields are snapshots, rather than a second score source.  They
    # preserve the score, clock and original kickoff seen on the field when a
    # later official award changes ``home_score`` / ``away_score``.
    abandoned_home_score = models.PositiveIntegerField(null=True, blank=True)
    abandoned_away_score = models.PositiveIntegerField(null=True, blank=True)
    abandoned_minute = models.PositiveIntegerField(null=True, blank=True)
    abandoned_phase = models.CharField(max_length=20, blank=True, default="")
    abandoned_at = models.DateTimeField(null=True, blank=True)
    abandoned_scheduled_for = models.DateTimeField(null=True, blank=True)
    abandonment_resolution = models.CharField(
        max_length=12,
        choices=AbandonmentResolution.choices,
        blank=True,
        default=AbandonmentResolution.NONE,
        db_index=True,
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UPCOMING')
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES, default='NONE')

    phase_start_time = models.DateTimeField(null=True, blank=True)

    minute = models.PositiveIntegerField(default=0)
    table_updated = models.BooleanField(default=False)
    lineups_locked = models.BooleanField(default=False)

    table_home_score = models.IntegerField(null=True, blank=True)
    table_away_score = models.IntegerField(null=True, blank=True)

    objects = MatchQuerySet.as_manager()

    GENERATED_LOCKED_FIELD_LABELS = {
        "competition_id": "competition",
        "stage_id": "stage",
        "group_id": "group",
        "home_team_id": "home team",
        "away_team_id": "away team",
        "gameweek": "gameweek",
        "leg": "leg",
        "knockout_tie_id": "knockout tie",
        "generated_by_system": "generated-fixture lock",
    }

    def _generated_structure_changes(self):
        """Return changed immutable fields when this row was system-generated."""

        if not self.pk:
            return []
        existing = type(self)._base_manager.filter(pk=self.pk).values(
            "competition_id",
            "stage_id",
            "group_id",
            "home_team_id",
            "away_team_id",
            "gameweek",
            "leg",
            "knockout_tie_id",
            "generated_by_system",
        ).first()
        if not existing or not existing["generated_by_system"]:
            return []
        return [
            field
            for field in self.GENERATED_LOCKED_FIELD_LABELS
            if existing[field] != getattr(self, field)
        ]

    def _schedule_locked_structure_changes(self):
        """Return protected changes when either affected schedule is locked."""

        if not self.competition_id:
            return []
        target_is_locked = Competition.objects.filter(
            pk=self.competition_id,
            schedule_locked_at__isnull=False,
        ).exists()
        if not self.pk:
            return ["__new__"] if target_is_locked else []

        existing = type(self)._base_manager.filter(pk=self.pk).values(
            *SCHEDULE_LOCKED_FIELD_LABELS,
            "competition__schedule_locked_at",
        ).first()
        if not existing:
            return []
        changed_fields = [
            field
            for field in SCHEDULE_LOCKED_FIELD_LABELS
            if existing[field] != getattr(self, field)
        ]
        if not changed_fields:
            return []
        if existing["competition__schedule_locked_at"] is not None or target_is_locked:
            return changed_fields
        return []

    def _is_valid_schedule_locked_bracket_creation(self):
        """Allow only bracket progression to create an expected future fixture."""

        if (
            self.pk
            or not self.generated_by_system
            or not self.knockout_tie_id
            or self.status != "UPCOMING"
            or self.phase != "NONE"
            or self.minute
            or self.lineups_locked
            or self.leg not in {1, 2}
        ):
            return False
        tie = FootballKnockoutTie.objects.select_related("stage").filter(
            pk=self.knockout_tie_id,
        ).first()
        if (
            not tie
            or tie.stage_id != self.stage_id
            or tie.stage.competition_id != self.competition_id
            or not tie.team_one_id
            or not tie.team_two_id
        ):
            return False
        expected_home = tie.team_one_id if self.leg == 1 else tie.team_two_id
        expected_away = tie.team_two_id if self.leg == 1 else tie.team_one_id
        return (
            self.home_team_id == expected_home
            and self.away_team_id == expected_away
        )

    def _is_valid_schedule_locked_abandoned_replay(self):
        """Allow the narrow, audited replay exception to a schedule lock."""

        if (
            self.pk
            or not self.replay_of_id
            or self.status != "UPCOMING"
            or self.phase != "NONE"
            or self.minute
            or self.home_score
            or self.away_score
            or self.table_updated
            or self.generated_by_system
            or self.knockout_tie_id
        ):
            return False
        source = type(self).objects.filter(pk=self.replay_of_id).values(
            "competition_id",
            "stage_id",
            "group_id",
            "home_team_id",
            "away_team_id",
            "leg",
            "status",
        ).first()
        return bool(
            source
            and source["status"] == "ABANDONED"
            and source["competition_id"] == self.competition_id
            and source["stage_id"] == self.stage_id
            and source["group_id"] == self.group_id
            and source["home_team_id"] == self.home_team_id
            and source["away_team_id"] == self.away_team_id
            and source["leg"] == self.leg
        )

    def _assert_valid_replay_link(self):
        """Keep programmatic saves from bypassing replay-chain safeguards."""

        if not self.replay_of_id:
            return
        source = type(self).objects.filter(pk=self.replay_of_id).values(
            "id", "competition_id", "stage_id", "group_id", "home_team_id",
            "away_team_id", "leg", "replay_of_id",
        ).first()
        if not source:
            raise ValidationError("The replay source no longer exists.")
        if self.pk and source["id"] == self.pk:
            raise ValidationError("A fixture cannot be a replay of itself.")
        expected = (
            self.competition_id,
            self.stage_id,
            self.group_id,
            self.home_team_id,
            self.away_team_id,
            self.leg,
        )
        actual = (
            source["competition_id"], source["stage_id"], source["group_id"],
            source["home_team_id"], source["away_team_id"], source["leg"],
        )
        if actual != expected:
            raise ValidationError(
                "A replay must keep the abandoned fixture's competition, stage, "
                "group, teams and leg."
            )
        # Multiple successive abandoned replays are allowed, but a relation
        # may never point back to this row through an ancestor.
        seen = {self.pk} if self.pk else set()
        ancestor_id = source["replay_of_id"]
        while ancestor_id:
            if ancestor_id in seen:
                raise ValidationError("A replay relationship cannot be circular.")
            seen.add(ancestor_id)
            ancestor_id = type(self).objects.filter(pk=ancestor_id).values_list(
                "replay_of_id", flat=True
            ).first()

    def _is_valid_bracket_participant_rewrite(self, changed_fields):
        """Allow only the bracket service to refresh an untouched future tie.

        Connected-bracket progression has one deliberate exception to the
        generated-fixture lock: when an upstream winner is corrected, a future
        fixture may need its already-derived participants refreshed.  The
        exception is intentionally narrow and still requires the exact teams
        in the current HOME/AWAY bracket slots.
        """

        if set(changed_fields) - {"home_team_id", "away_team_id"}:
            return False
        if (
            not self.knockout_tie_id
            or self.status != "UPCOMING"
            or self.phase != "NONE"
            or self.minute
            or self.lineups_locked
        ):
            return False
        slots = {
            slot.slot: slot.team_id
            for slot in FootballKnockoutTieSlot.objects.filter(
                tie_id=self.knockout_tie_id,
            )
        }
        expected_home = slots.get(
            FootballKnockoutTieSlot.Slot.HOME
            if self.leg == 1
            else FootballKnockoutTieSlot.Slot.AWAY
        )
        expected_away = slots.get(
            FootballKnockoutTieSlot.Slot.AWAY
            if self.leg == 1
            else FootballKnockoutTieSlot.Slot.HOME
        )
        return (
            expected_home
            and expected_away
            and self.home_team_id == expected_home
            and self.away_team_id == expected_away
        )

    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"

    def is_editable_by(self, user):
        """
        Compatibility method used by existing match views.

        Permission decisions are now handled centrally by
        league_manager.permissions.
        """
        from league_manager.permissions import can_control_match

        return can_control_match(user, self)
    def clean(self):
        errors = {}

        if self.replay_of_id:
            if self.pk and self.replay_of_id == self.pk:
                errors["replay_of"] = "A fixture cannot be a replay of itself."
            elif self.replay_of.competition_id != self.competition_id:
                errors["replay_of"] = "A replay must remain in the same competition."
            elif (
                self.replay_of.stage_id != self.stage_id
                or self.replay_of.group_id != self.group_id
                or self.replay_of.home_team_id != self.home_team_id
                or self.replay_of.away_team_id != self.away_team_id
                or self.replay_of.leg != self.leg
            ):
                errors["replay_of"] = (
                    "A replay must keep the abandoned fixture's stage, group, "
                    "teams and leg."
                )

        generated_changes = self._generated_structure_changes()
        if generated_changes:
            labels = ", ".join(
                self.GENERATED_LOCKED_FIELD_LABELS[field]
                for field in generated_changes
            )
            errors["__all__"] = (
                "Generated fixtures keep their assigned teams and round. "
                f"The following fields are locked: {labels}."
            )

        schedule_changes = self._schedule_locked_structure_changes()
        if schedule_changes:
            if schedule_changes == ["__new__"]:
                errors["__all__"] = (
                    "This competition's schedule is locked. New fixtures cannot be created."
                )
            else:
                labels = ", ".join(
                    SCHEDULE_LOCKED_FIELD_LABELS[field]
                    for field in schedule_changes
                )
                errors["__all__"] = (
                    "This competition's schedule is locked. The following fields cannot "
                    f"be changed: {labels}."
                )

        if self.home_team_id and self.competition_id:
            if self.home_team.competition_id != self.competition_id:
                errors["home_team"] = "Home team does not belong to this competition."

        if self.away_team_id and self.competition_id:
            if self.away_team.competition_id != self.competition_id:
                errors["away_team"] = "Away team does not belong to this competition."

        if self.home_team_id and self.away_team_id:
            if self.home_team_id == self.away_team_id:
                errors["away_team"] = "A team cannot play against itself."

        if self.stage_id and self.competition_id:
            if self.stage.competition_id != self.competition_id:
                errors["stage"] = "Selected stage does not belong to this competition."

        if self.knockout_tie_id:
            if self.stage_id and self.knockout_tie.stage_id != self.stage_id:
                errors["knockout_tie"] = (
                    "The knockout tie must belong to the selected stage."
                )
            if self.home_team_id and self.away_team_id:
                match_team_ids = {self.home_team_id, self.away_team_id}
                tie_team_ids = {
                    self.knockout_tie.team_one_id,
                    self.knockout_tie.team_two_id,
                }
                if match_team_ids != tie_team_ids:
                    errors["knockout_tie"] = (
                        "The match teams must be the participants in the knockout tie."
                    )

        paired_fields = (
            ("regulation_home_score", "regulation_away_score"),
            ("penalty_home_score", "penalty_away_score"),
        )
        for home_field, away_field in paired_fields:
            home_value = getattr(self, home_field)
            away_value = getattr(self, away_field)
            if (home_value is None) != (away_value is None):
                errors[home_field] = "Both team scores must be recorded together."

        if self.penalty_home_score is not None and self.stage_id:
            if self.stage.stage_type != "KNOCKOUT":
                errors["penalty_home_score"] = (
                    "Penalty shootouts are only available for knockout matches."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Save only.
        Do NOT trigger standings or prediction side effects here.
        """
        allow_generated_bracket_participant_rewrite = kwargs.pop(
            "_allow_generated_bracket_participant_rewrite",
            False,
        )
        allow_schedule_locked_bracket_creation = kwargs.pop(
            "_allow_schedule_locked_bracket_creation",
            False,
        )
        allow_abandoned_resume = kwargs.pop("_allow_abandoned_resume", False)
        allow_schedule_locked_replay = kwargs.pop(
            "_allow_schedule_locked_replay",
            False,
        )
        self._assert_valid_replay_link()
        generated_changes = self._generated_structure_changes()
        valid_generated_bracket_rewrite = (
            allow_generated_bracket_participant_rewrite
            and self._is_valid_bracket_participant_rewrite(generated_changes)
        )
        if generated_changes and not valid_generated_bracket_rewrite:
            labels = ", ".join(
                self.GENERATED_LOCKED_FIELD_LABELS[field]
                for field in generated_changes
            )
            raise ValidationError(
                "Generated fixtures keep their assigned teams and round. "
                f"The following fields are locked: {labels}."
            )

        schedule_changes = self._schedule_locked_structure_changes()
        if schedule_changes:
            if schedule_changes == ["__new__"]:
                if not (
                    allow_schedule_locked_bracket_creation
                    and self._is_valid_schedule_locked_bracket_creation()
                ) and not (
                    allow_schedule_locked_replay
                    and self._is_valid_schedule_locked_abandoned_replay()
                ):
                    raise ValidationError(
                        "This competition's schedule is locked. New fixtures cannot be created."
                    )
            elif not valid_generated_bracket_rewrite:
                labels = ", ".join(
                    SCHEDULE_LOCKED_FIELD_LABELS[field]
                    for field in schedule_changes
                )
                raise ValidationError(
                    "This competition's schedule is locked. The following fields cannot "
                    f"be changed: {labels}."
                )

        previous_status = None
        update_fields = kwargs.get("update_fields")
        # Final result saves never transition to LIVE, so they do not need a
        # preliminary status SELECT before their single-row UPDATE. Likewise,
        # a save that omits status cannot start the live lifecycle.
        if (
            self.pk
            and self.status == "LIVE"
            and (update_fields is None or "status" in update_fields)
        ):
            previous_status = (
                type(self)
                .objects
                .filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        if previous_status == "ABANDONED" and self.status == "LIVE" and not allow_abandoned_resume:
            raise ValidationError(
                "An abandoned match can only return to live play through the "
                "authorised abandoned-match resolution workflow."
            )

        status_changed_to_live = (
            self.status == "LIVE"
            and previous_status != "LIVE"
        )
        if status_changed_to_live:
            # A match entering its live lifecycle always locks team lineups.
            self.lineups_locked = True
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {
                    "lineups_locked",
                }

        super().save(*args, **kwargs)

    def unlock_lineups(self):
        """Reopen lineups only before a match begins."""
        if self.status != "UPCOMING":
            raise ValidationError(
                "Lineups can only be unlocked before a match begins."
            )
        if not self.lineups_locked:
            return

        self.lineups_locked = False
        self.save(update_fields=["lineups_locked"])

    # =========================
    # MATCH CONTROL METHODS
    # =========================

    def start_first_half(self, *, current_minute=0):
        """Start the existing server clock from an elapsed first-half minute.

        The adjusted phase reference is persisted, so all users and the
        scheduled minute updater calculate the same running clock.  It does
        not change the fixture's scheduled kickoff.
        """
        if self.status == "FINISHED":
            return

        if isinstance(current_minute, bool) or not isinstance(current_minute, int):
            raise ValidationError("The current match minute must be a whole number.")
        if current_minute < 0:
            raise ValidationError("The current match minute cannot be negative.")

        self.status = "LIVE"
        self.phase = "FIRST_HALF"
        self.minute = current_minute
        self.phase_start_time = timezone.now() - timedelta(minutes=current_minute)
        self.save(update_fields=["status", "phase", "minute", "phase_start_time"])

    def end_first_half(self):
        if self.status == "FINISHED":
            return
        self.minute = self.get_current_minute()
        self.phase = "BREAK"
        self.status = "LIVE"
        self.phase_start_time = None
        self.save(update_fields=["minute", "phase", "status", "phase_start_time"])

    def start_second_half(self):
        if self.status == "FINISHED":
            return
        self.status = "LIVE"
        self.phase = "SECOND_HALF"
        self.phase_start_time = timezone.now()
        self.save(update_fields=["status", "phase", "phase_start_time"])

    def abandon_match(self, reason):
        if self.status == "FINISHED":
            return

        # Freeze the live clock before clearing its server reference.  A
        # resumed match must never include the time that it spent abandoned.
        elapsed_minute = self.get_current_minute()
        now = timezone.now()
        if self.pk:
            AbandonedMatchDecision.objects.filter(
                match_id=self.pk,
                is_current=True,
                decision_type=AbandonedMatchDecision.DecisionType.RESUME,
            ).update(is_current=False, superseded_at=now)

        self.status = "ABANDONED"
        self.abandon_reason = reason
        self.abandoned_home_score = self.home_score
        self.abandoned_away_score = self.away_score
        self.abandoned_minute = elapsed_minute
        self.abandoned_phase = self.phase
        self.abandoned_at = now
        self.abandoned_scheduled_for = self.match_date
        self.abandonment_resolution = self.AbandonmentResolution.NONE
        self.minute = elapsed_minute
        self.phase = "NONE"
        self.phase_start_time = None
        self.save(update_fields=[
            "status",
            "abandon_reason",
            "abandoned_home_score",
            "abandoned_away_score",
            "abandoned_minute",
            "abandoned_phase",
            "abandoned_at",
            "abandoned_scheduled_for",
            "abandonment_resolution",
            "minute",
            "phase",
            "phase_start_time",
        ])

    # =========================
    # LIVE MINUTE CALCULATION
    # =========================

    def get_current_minute(self):
        if self.status != "LIVE" or not self.phase_start_time:
            return self.minute

        elapsed = int((timezone.now() - self.phase_start_time).total_seconds() // 60)

        if self.phase == "FIRST_HALF":
            return elapsed

        if self.phase == "SECOND_HALF":
            return 45 + elapsed

        if self.phase == "EXTRA_FIRST_HALF":
            return 90 + elapsed

        if self.phase == "EXTRA_SECOND_HALF":
            return 105 + elapsed

        return self.minute

    def get_display_minute(self):
        if self.status == "FINISHED":
            return "FT"

        if self.phase == "BREAK":
            return "HT"

        if self.phase == "REGULATION_ENDED":
            return "90'"

        if self.phase == "EXTRA_BREAK":
            return "ET HT"

        if self.phase == "EXTRA_ENDED":
            return "AET"

        if self.phase == "PENALTIES":
            return "PEN"

        minute = self.get_current_minute()

        if self.phase == "FIRST_HALF":
            if minute > 45:
                return f"45+{minute - 45}"
            elif minute == 45:
                return "45"
            else:
                return str(minute)

        if self.phase == "SECOND_HALF":
            if minute > 90:
                return f"90+{minute - 90}"
            elif minute == 90:
                return "90"
            else:
                return str(minute)

        if self.phase == "EXTRA_FIRST_HALF":
            if minute > 105:
                return f"105+{minute - 105}"
            return str(minute)

        if self.phase == "EXTRA_SECOND_HALF":
            if minute > 120:
                return f"120+{minute - 120}"
            return str(minute)

        return str(minute)

    # =========================
    # LEAGUE TABLE UPDATE
    # =========================

    @transaction.atomic
    def update_table(self):
        if self.stage.stage_type not in {"LEAGUE", "GROUP"}:
            return

        if self.table_updated:
            return

        home_standing, _ = Standing.objects.select_for_update().get_or_create(
            competition=self.competition,
            stage=self.stage,
            group=self.group,
            team=self.home_team,
            defaults={
                "played": 0,
                "won": 0,
                "drawn": 0,
                "lost": 0,
                "goals_for": 0,
                "goals_against": 0,
                "points": 0,
            }
        )

        away_standing, _ = Standing.objects.select_for_update().get_or_create(
            competition=self.competition,
            stage=self.stage,
            group=self.group,
            team=self.away_team,
            defaults={
                "played": 0,
                "won": 0,
                "drawn": 0,
                "lost": 0,
                "goals_for": 0,
                "goals_against": 0,
                "points": 0,
            }
        )

        home_standing.played += 1
        away_standing.played += 1

        home_standing.goals_for += self.home_score
        home_standing.goals_against += self.away_score

        away_standing.goals_for += self.away_score
        away_standing.goals_against += self.home_score

        if self.home_score > self.away_score:
            home_standing.won += 1
            home_standing.points += 3
            away_standing.lost += 1

        elif self.home_score < self.away_score:
            away_standing.won += 1
            away_standing.points += 3
            home_standing.lost += 1

        else:
            home_standing.drawn += 1
            away_standing.drawn += 1
            home_standing.points += 1
            away_standing.points += 1

        home_standing.save()
        away_standing.save()

        self.table_updated = True
        self.save(update_fields=["table_updated"])

    def get_stage_label(self):
        if self.stage.stage_type == "GROUP" and self.group:
            return f"{self.stage.name} - {self.group.name}"

        return self.stage.name

    @property
    def prediction_home_score(self):
        if self.regulation_home_score is not None:
            return self.regulation_home_score
        return self.home_score

    @property
    def prediction_away_score(self):
        if self.regulation_away_score is not None:
            return self.regulation_away_score
        return self.away_score

    @property
    def has_penalty_shootout(self):
        return (
            self.penalty_home_score is not None
            and self.penalty_away_score is not None
        )

    @property
    def result_note(self):
        if self.abandonment_resolution == self.AbandonmentResolution.AWARDED:
            return "Official result awarded"
        if self.abandonment_resolution == self.AbandonmentResolution.CONFIRMED:
            return "Score confirmed after abandonment"
        if self.abandonment_resolution == self.AbandonmentResolution.VOID:
            return "Match voided — no points awarded"
        if self.abandonment_resolution == self.AbandonmentResolution.REPLAY:
            return "Full replay ordered"
        if self.abandonment_resolution == self.AbandonmentResolution.RESUMED:
            return "Resumed after abandonment"
        if self.has_penalty_shootout:
            return (
                f"{self.penalty_home_score}-{self.penalty_away_score} "
                "on penalties"
            )
        if self.resolution_method == "EXTRA_TIME":
            return "After extra time"
        if self.resolution_method == "AGGREGATE":
            return "Decided on aggregate"
        return ""


class AbandonedMatchDecision(models.Model):
    """Append-only record of an authorised abandonment decision.

    ``is_current`` is intentionally separate from the historical row.  A
    correction revokes the current decision rather than overwriting who made
    it, when, or why.  The conditional database constraint also makes a
    repeated form POST and concurrent administrators safe.
    """

    class DecisionType(models.TextChoices):
        REPLAY = "REPLAY", "Schedule full replay"
        RESUME = "RESUME", "Resume match"
        AWARD = "AWARD", "Award official result"
        CONFIRM = "CONFIRM", "Confirm score at abandonment"
        VOID = "VOID", "Void match"

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="abandonment_decisions",
    )
    decision_type = models.CharField(max_length=12, choices=DecisionType.choices)
    # This is the official resolution description (an administrator's note or
    # a server-generated default), not the reason the match was abandoned.
    reason = models.TextField()
    # Historical records retain their references. New decisions do not require
    # a separate reference entry from administrators.
    decision_reference = models.CharField(max_length=200, blank=True, default="")
    official_home_score = models.PositiveIntegerField(null=True, blank=True)
    official_away_score = models.PositiveIntegerField(null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    venue = models.CharField(max_length=200, blank=True)
    resume_minute = models.PositiveIntegerField(null=True, blank=True)
    replay_match = models.ForeignKey(
        Match,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_by_abandonment_decisions",
    )
    decided_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="abandoned_match_decisions",
    )
    decided_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(default=True, db_index=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supersedes",
    )

    class Meta:
        ordering = ["-decided_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["match"],
                condition=Q(is_current=True),
                name="one_current_abandonment_decision_per_match",
            ),
            models.CheckConstraint(
                condition=(
                    Q(official_home_score__isnull=True, official_away_score__isnull=True)
                    | Q(official_home_score__isnull=False, official_away_score__isnull=False)
                ),
                name="abandonment_decision_scores_paired",
            ),
        ]

    def clean(self):
        errors = {}
        scores_provided = (
            self.official_home_score is not None
            or self.official_away_score is not None
        )
        if self.decision_type == self.DecisionType.AWARD and not scores_provided:
            errors["official_home_score"] = "An awarded result needs both official scores."
        if self.decision_type != self.DecisionType.AWARD and scores_provided:
            errors["official_home_score"] = "Only an awarded result stores an official score."
        if self.decision_type == self.DecisionType.REPLAY and not self.scheduled_for:
            errors["scheduled_for"] = "Set the kickoff for the replay fixture."
        if self.decision_type == self.DecisionType.RESUME and self.resume_minute is None:
            errors["resume_minute"] = "Set the minute from which the match will resume."
        if self.replay_match_id and self.decision_type != self.DecisionType.REPLAY:
            errors["replay_match"] = "Only a full replay decision may create a fixture."
        if self.replay_match_id and self.replay_match.replay_of_id != self.match_id:
            errors["replay_match"] = "The replay fixture must link back to this match."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.get_decision_type_display()} — {self.match}"


class Player(models.Model):
    name = models.CharField(max_length=200)
    photo = models.ImageField(
        upload_to="players/",
        storage=SupabaseStorage(folder="players", image_profile="photo"),
        blank=True,
        null=True,
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='players')
    jersey_number = models.IntegerField(null=True, blank=True)
    starting_goals = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.team.name})"

    @property
    def recorded_goals(self):
        if hasattr(self, "recorded_goals_count"):
            return self.recorded_goals_count
    
        return self.goal_set.filter(
            canceled=False,
        ).exclude(
            match__status="ABANDONED",
        ).filter(
            match__abandonment_resolution__in=[
                Match.AbandonmentResolution.NONE,
                Match.AbandonmentResolution.RESUMED,
                Match.AbandonmentResolution.CONFIRMED,
            ],
            goal_type__in=[
                Goal.GoalType.NORMAL,
                Goal.GoalType.PENALTY,
            ],
        ).count()
    
    @property
    def total_goals(self):
        return self.starting_goals + self.recorded_goals


class MatchLineup(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='lineups')
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    # Retained for backwards compatibility with previously saved lineups.
    # New submissions are stored as MatchLineupEntry records below.
    players = models.ManyToManyField(Player)
    submitted_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_match_lineups",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_match_lineups",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["match", "team"],
                name="unique_match_lineup_per_team",
            ),
        ]

    def __str__(self):
        return f"Lineup - {self.team.name} ({self.match})"

    def clean(self):
        if self.match_id and self.team_id and self.team_id not in {
            self.match.home_team_id,
            self.match.away_team_id,
        }:
            raise ValidationError(
                {"team": "The lineup team must be playing in this match."}
            )


class MatchLineupEntry(models.Model):
    """A selected player in a team lineup.

    The role and display order keep the schema ready for formations, positions,
    captains, appearances, substitutions and player statistics without changing
    the lineup relationship later.
    """

    class Role(models.TextChoices):
        STARTER = "STARTER", "Starting XI"
        SUBSTITUTE = "SUBSTITUTE", "Substitute"

    lineup = models.ForeignKey(
        MatchLineup,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.PROTECT,
        related_name="lineup_entries",
    )
    role = models.CharField(max_length=12, choices=Role.choices)
    sort_order = models.PositiveSmallIntegerField(default=0)
    position = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["role", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["lineup", "player"],
                name="unique_player_per_match_lineup",
            ),
        ]

    def __str__(self):
        return f"{self.player} - {self.get_role_display()}"


class Goal(models.Model):
    class GoalType(models.TextChoices):
        NORMAL = "NORMAL", "Normal Goal"
        PENALTY = "PENALTY", "Penalty Goal"
        OWN_GOAL = "OWN_GOAL", "Own Goal"

    class Period(models.TextChoices):
        FIRST_HALF = "FIRST_HALF", "First Half"
        SECOND_HALF = "SECOND_HALF", "Second Half"
        EXTRA_FIRST_HALF = "EXTRA_FIRST_HALF", "Extra Time First Half"
        EXTRA_SECOND_HALF = "EXTRA_SECOND_HALF", "Extra Time Second Half"

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='goals')
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    player = models.ForeignKey(Player, null=True, blank=True, on_delete=models.SET_NULL)
    player_name = models.CharField(max_length=200, blank=True)
    goal_type = models.CharField(
        max_length=10,
        choices=GoalType.choices,
        default=GoalType.NORMAL,
    )

    minute = models.PositiveIntegerField(null=True, blank=True)
    period = models.CharField(
        max_length=24,
        choices=Period.choices,
        default=Period.SECOND_HALF,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    canceled = models.BooleanField(default=False)
    # A browser retry must represent the same logical event, not another goal.
    # Nullable keeps all historic rows valid while new control forms require it.
    idempotency_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    def clean(self):
        """Protect goal attribution for the normal match-goal lifecycle."""
        errors = {}
        match = self.match if self.match_id else None

        if match and self.team_id:
            match_team_ids = {match.home_team_id, match.away_team_id}
            if self.team_id not in match_team_ids:
                errors["team"] = "The benefiting team must be playing in this match."
            elif self.player_id:
                expected_team_id = self.team_id
                if self.goal_type == self.GoalType.OWN_GOAL:
                    expected_team_id = (
                        match.away_team_id
                        if self.team_id == match.home_team_id
                        else match.home_team_id
                    )
                if self.player.team_id != expected_team_id:
                    if self.goal_type == self.GoalType.OWN_GOAL:
                        errors["player"] = (
                            "An own-goal player must belong to the opposing team."
                        )
                    else:
                        errors["player"] = (
                            "The scorer must belong to the benefiting team."
                        )
            # A post-match result may confirm an own goal without identifying
            # the player.  Keep the scorer relation empty in that case rather
            # than inventing a player record.

        # Existing goals may be corrected after the match.  Only a new goal is
        # limited to a currently active regulation or extra-time period.
        if self._state.adding and match and match.status != "FINISHED":
            valid_phases = {
                "FIRST_HALF",
                "SECOND_HALF",
                "EXTRA_FIRST_HALF",
                "EXTRA_SECOND_HALF",
            }
            if match.status != "LIVE" or match.phase not in valid_phases:
                errors["match"] = (
                    "Goals can only be recorded while a regulation or extra-time "
                    "period is running."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding:
            phase_periods = {
                "FIRST_HALF": self.Period.FIRST_HALF,
                "SECOND_HALF": self.Period.SECOND_HALF,
                "EXTRA_FIRST_HALF": self.Period.EXTRA_FIRST_HALF,
                "EXTRA_SECOND_HALF": self.Period.EXTRA_SECOND_HALF,
            }
            if (
                self.match.status != "LIVE"
                or self.match.phase not in phase_periods
            ) and self.match.status != "FINISHED":
                raise ValidationError(
                    "Goals can only be recorded while a regulation or extra-time "
                    "period is running."
                )
            # A goal detail may be supplied after a Quick Result has already
            # finished an otherwise untracked match.  Its live period is not
            # known in that case; retain the model's longstanding neutral
            # default rather than indexing the terminal ``NONE`` phase.
            self.period = phase_periods.get(
                self.match.phase,
                self.Period.SECOND_HALF,
            )
        if self.minute is None and self.match.status == "LIVE":
            self.minute = self.match.get_current_minute()
        if self.player and not self.player_name:
            self.player_name = self.player.name
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        status = " (Canceled)" if self.canceled else ""
        return f"{self.player_name or 'Unknown'} - {self.match}{status}"


class YellowCard(models.Model):
    """A disciplinary event.

    The legacy name is retained deliberately so existing yellow-card records and
    reverse relations remain valid.  ``card_type`` expands the event into the
    full disciplinary vocabulary without reinterpreting historic rows.
    """

    class CardType(models.TextChoices):
        YELLOW = "YELLOW", "Yellow"
        SECOND_YELLOW_RED = "SECOND_YELLOW_RED", "Second yellow / red"
        DIRECT_RED = "DIRECT_RED", "Direct red"

    match = models.ForeignKey('Match', on_delete=models.CASCADE, related_name='yellow_cards')
    team = models.ForeignKey('teams.Team', on_delete=models.CASCADE)
    player = models.ForeignKey('Player', on_delete=models.CASCADE)
    minute = models.IntegerField(blank=True, null=True)
    card_type = models.CharField(
        max_length=20,
        choices=CardType.choices,
        default=CardType.YELLOW,
    )
    canceled = models.BooleanField(default=False)
    idempotency_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class Meta:
        ordering = ["minute", "id"]

    def clean(self):
        errors = {}
        if self.match_id:
            if self.team_id not in {self.match.home_team_id, self.match.away_team_id}:
                errors["team"] = "The card team must be playing in this match."
            if self.player_id and self.player.team_id != self.team_id:
                errors["player"] = "The player must be registered for the selected team."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.player.name} - {self.get_card_type_display()}"


class MatchAdminAssignment(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='assigned_admins')
    admin = models.ForeignKey(User, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.admin.username} → {self.match}"


class MatchResultProcessingOutbox(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    match = models.OneToOneField(
        Match, on_delete=models.CASCADE, related_name="result_processing",
    )
    requested_revision = models.PositiveBigIntegerField(default=1)
    completed_revision = models.PositiveBigIntegerField(default=0)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    pending_since = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    dispatch_token = models.UUIDField(null=True, blank=True)
    dispatch_attempts = models.PositiveIntegerField(default=0)
    processing_failures = models.PositiveIntegerField(default=0)
    last_dispatch_at = models.DateTimeField(null=True, blank=True)
    last_processing_at = models.DateTimeField(null=True, blank=True)
    last_completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state", "next_attempt_at", "id"], name="match_result_outbox_due_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(completed_revision__lte=models.F("requested_revision")),
                name="match_result_outbox_revision_order",
            ),
        ]
