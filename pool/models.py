"""Database models for EsportsLive's 8-ball pool competitions.

These models intentionally remain separate from the football apps.  Pool
fixtures are built around competition entries, so a future team fixture can
gain player-level races without changing the top-level fixture relationship.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Count, Q
from django.utils import timezone

from teams.storage_backends import SupabaseStorage


# These are intentionally shared by independent manual-round configuration
# and persistent bracket topology.  A configured round is metadata for a
# knockout stage; it is not itself a generated bracket node.
SUPPORTED_KNOCKOUT_ROUND_SIZES = (2, 4, 8, 16, 32, 64, 128, 256)


def canonical_knockout_round_name(round_size):
    """Return the one public label for a supported knockout round size."""

    labels = {
        2: "Final",
        4: "Semi-final",
        8: "Quarter-final",
    }
    if round_size in labels:
        return labels[round_size]
    if round_size in SUPPORTED_KNOCKOUT_ROUND_SIZES:
        return f"Round of {round_size}"
    return ""


class PoolCompetitionQuerySet(models.QuerySet):
    """Query helpers for public and manager-aware competition lists."""

    _STANDINGS_POLICY_FIELDS = frozenset(
        {
            "points_mode",
            "points_for_win",
            "points_for_loss",
            "default_race_target",
        }
    )

    def update(self, **kwargs):
        """Do not let a raw update activate or weaken a table-points policy."""

        if self._STANDINGS_POLICY_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Bulk updates to pool standings policy are not supported; use "
                "the validated standings or competition settings workflow."
            )
        return super().update(**kwargs)

    def published(self):
        """Return the competitions currently published to the public site."""
        return self.filter(visibility=PoolCompetition.Visibility.PUBLISHED)

    def visible_to(self, user):
        """Return public competitions plus drafts a manager may preview."""
        public = self.published()
        if not getattr(user, "is_authenticated", False) or not getattr(
            user, "is_active", False
        ):
            return public
        if getattr(user, "is_superuser", False):
            return self
        return self.filter(
            Q(pk__in=public.values("pk"))
            | Q(owner=user)
            | Q(
                memberships__user=user,
                memberships__is_active=True,
                memberships__role__in=[
                    PoolCompetitionMembership.Role.OWNER,
                    PoolCompetitionMembership.Role.ADMIN,
                ],
            )
        ).distinct()


class PoolCompetitionManager(models.Manager.from_queryset(PoolCompetitionQuerySet)):
    pass


def competition_has_generated_group_qualification_bracket(competition_id):
    """Return whether a competition has a generated group-qualifier bracket.

    Standings settings are deliberately frozen while qualification slots exist:
    changing their policy could otherwise change a participant that has already
    been placed into a knockout topology.  ``None`` is common while a new
    competition is being validated, so it is intentionally treated as unlocked.
    """
    if not competition_id:
        return False
    return PoolKnockoutTieSlot.objects.filter(
        tie__bracket__competition_id=competition_id,
        source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
    ).exists()


class PoolPlayer(models.Model):
    """An individual pool player, optionally linked to a site account."""

    class Gender(models.TextChoices):
        FEMALE = "FEMALE", "Female"
        MALE = "MALE", "Male"
        NON_BINARY = "NON_BINARY", "Non-binary"
        PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY", "Prefer not to say"

    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )
    full_name = models.CharField(max_length=200)
    nickname = models.CharField(max_length=100, blank=True)
    profile_picture = models.ImageField(
        upload_to="pool/players/",
        storage=SupabaseStorage(folder="pool/players", image_profile="photo"),
        blank=True,
        null=True,
    )
    region = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    gender = models.CharField(
        max_length=20,
        choices=Gender.choices,
        blank=True,
    )
    # Contact fields are deliberately private.  Public views must only use
    # the non-contact profile fields above.
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pool_player_profile",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name", "id"]

    def __str__(self):
        return self.nickname or self.full_name

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolTeam(models.Model):
    """A pool team, independent of any football team or club/room."""

    name = models.CharField(max_length=200)
    logo = models.ImageField(
        upload_to="pool/teams/",
        storage=SupabaseStorage(folder="pool/teams", image_profile="logo"),
        blank=True,
        null=True,
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolCompetitionSeries(models.Model):
    """The durable identity shared by successive pool league seasons."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class PoolCompetition(models.Model):
    """A pool competition and its public configuration."""

    class ParticipantMode(models.TextChoices):
        SINGLES = "SINGLES", "Singles"
        TEAMS = "TEAMS", "Teams"

    class FormatType(models.TextChoices):
        LEAGUE = "LEAGUE", "League"
        LEAGUE_KNOCKOUT = "LEAGUE_KNOCKOUT", "League/Group + Knockout"
        KNOCKOUT = "KNOCKOUT", "Knockout"
        MONEY_MATCH = "MONEY_MATCH", "Money Match"

    class Visibility(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"

    class OperationalState(models.TextChoices):
        UPCOMING = "UPCOMING", "Upcoming"
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"

    class PointsMode(models.TextChoices):
        FIXED = "FIXED", "Fixed points"
        MARGIN_BASED = "MARGIN_BASED", "Margin-based points"

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    series = models.ForeignKey(
        PoolCompetitionSeries,
        on_delete=models.PROTECT,
        related_name="competitions",
    )
    season = models.CharField(max_length=20, blank=True)
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="archived_pool_competitions",
        null=True,
        blank=True,
    )
    description = models.TextField(blank=True)
    logo = models.ImageField(
        upload_to="pool/competitions/logos/",
        storage=SupabaseStorage(folder="pool/competitions/logos", image_profile="logo"),
        blank=True,
        null=True,
    )
    cover_image = models.ImageField(
        upload_to="pool/competitions/covers/",
        storage=SupabaseStorage(folder="pool/competitions/covers", image_profile="general"),
        blank=True,
        null=True,
    )
    participant_mode = models.CharField(
        max_length=10,
        choices=ParticipantMode.choices,
        default=ParticipantMode.SINGLES,
    )
    format_type = models.CharField(
        max_length=20,
        choices=FormatType.choices,
        default=FormatType.LEAGUE,
    )
    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.DRAFT,
    )
    operational_state = models.CharField(
        max_length=10,
        choices=OperationalState.choices,
        default=OperationalState.UPCOMING,
    )
    default_race_target = models.PositiveSmallIntegerField(default=7)
    money_match_prize_pot = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional total prize money for a Money Match.",
    )
    points_for_win = models.PositiveSmallIntegerField(
        default=2,
        help_text="Table points awarded for an eligible pool match win.",
    )
    points_for_loss = models.PositiveSmallIntegerField(
        default=0,
        help_text="Table points awarded for an eligible pool match loss.",
    )
    points_mode = models.CharField(
        max_length=16,
        choices=PointsMode.choices,
        default=PointsMode.FIXED,
        help_text=(
            "Use fixed points for every eligible result, or margin-based "
            "points for played matches."
        ),
    )
    third_place_match_enabled = models.BooleanField(
        default=False,
        help_text="Create a third-place match when a knockout bracket is generated.",
    )
    match_time_limit_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Optional per-match countdown for knockout-format fixtures. The "
            "current value is copied to a fixture only when that fixture starts."
        ),
    )
    allow_two_lives = models.BooleanField(
        default=False,
        help_text=(
            "Allow one or two tournament lives per singles knockout entry. "
            "This is unavailable for leagues, groups, and team competitions."
        ),
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    venue = models.CharField(max_length=255, blank=True)
    rules = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="owned_pool_competitions",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PoolCompetitionManager()

    class Meta:
        ordering = ["-start_date", "name", "id"]
        indexes = [
            models.Index(
                fields=["visibility", "start_date"],
                name="pool_comp_visibility_date_idx",
            ),
            models.Index(fields=["participant_mode", "format_type"], name="pool_comp_mode_format_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "season"],
                name="pool_unique_season_per_competition_series",
            ),
            models.CheckConstraint(
                condition=Q(default_race_target__gte=1),
                name="pool_competition_race_target_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(money_match_prize_pot__isnull=True)
                    | Q(money_match_prize_pot__gte=0)
                ),
                name="pool_competition_money_match_prize_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(points_for_win__gte=0),
                name="pool_competition_win_points_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(points_for_loss__gte=0),
                name="pool_competition_loss_points_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(start_date__isnull=True)
                    | Q(end_date__isnull=True)
                    | Q(end_date__gte=models.F("start_date"))
                ),
                name="pool_competition_dates_in_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(match_time_limit_minutes__isnull=True)
                    | Q(match_time_limit_minutes__gte=1)
                ),
                name="pool_competition_match_time_limit_positive",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_money_match(self):
        """Whether this competition is the single-fixture Money Match format."""

        return self.format_type == self.FormatType.MONEY_MATCH

    @property
    def has_league_or_group_stage(self):
        if not self.pk:
            return self.format_type in {
                self.FormatType.LEAGUE,
                self.FormatType.LEAGUE_KNOCKOUT,
            }
        return self.stages.filter(
            stage_type__in=[
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            ]
        ).exists()

    @property
    def has_knockout_stage(self):
        if not self.pk:
            return self.format_type in {
                self.FormatType.LEAGUE_KNOCKOUT,
                self.FormatType.KNOCKOUT,
            }
        return self.stages.filter(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        ).exists()

    def clean(self):
        errors = {}
        original = (
            type(self).objects.filter(pk=self.pk)
            .values(
                "format_type",
                "points_for_win",
                "points_for_loss",
                "points_mode",
                "default_race_target",
                "match_time_limit_minutes",
                "allow_two_lives",
            )
            .first()
            if self.pk
            else None
        )
        race_target_changed = bool(
            original
            and original["default_race_target"] != self.default_race_target
        )
        points_configuration_changed = bool(
            original
            and any(
                original[field] != getattr(self, field)
                for field in ("points_for_win", "points_for_loss", "points_mode")
            )
        )
        margin_policy_configuration_changed = bool(
            points_configuration_changed or race_target_changed
        )
        if (
            self.start_date
            and self.end_date
            and self.end_date < self.start_date
        ):
            errors["end_date"] = "The end date cannot be before the start date."

        if (
            self.is_money_match
            and self.participant_mode != self.ParticipantMode.SINGLES
        ):
            errors["participant_mode"] = "Money Matches are available for singles players only."

        if self.default_race_target is not None and self.default_race_target < 1:
            errors["default_race_target"] = "Race target must be at least 1."
        if self.money_match_prize_pot is not None and self.money_match_prize_pot < 0:
            errors["money_match_prize_pot"] = "Prize pot cannot be negative."
        if self.points_for_win is not None and self.points_for_win < 0:
            errors["points_for_win"] = "Win points cannot be negative."
        if self.points_for_loss is not None and self.points_for_loss < 0:
            errors["points_for_loss"] = "Loss points cannot be negative."
        if (
            self.match_time_limit_minutes is not None
            and self.match_time_limit_minutes < 1
        ):
            errors["match_time_limit_minutes"] = (
                "Match time limit must be at least one minute."
            )
        if (
            self.format_type
            in {self.FormatType.LEAGUE, self.FormatType.MONEY_MATCH}
            and self.match_time_limit_minutes is not None
        ):
            errors["match_time_limit_minutes"] = (
                "A competition-wide match timer is available only for knockout "
                "and league/knockout tournaments."
            )

        two_lives_allowed_for_format = (
            self.participant_mode == self.ParticipantMode.SINGLES
            and self.format_type == self.FormatType.KNOCKOUT
        )
        if self.allow_two_lives and not two_lives_allowed_for_format:
            errors["allow_two_lives"] = (
                "Two lives are available only for singles knockout competitions."
            )

        # An active margin policy must always be complete.  The transactional
        # standings service installs its complete rules before saving this
        # field; this guard closes the direct ORM/admin path as well.
        if not self.pk and self.points_mode == self.PointsMode.MARGIN_BASED:
            errors["points_mode"] = (
                "Create the competition with fixed points, then save a complete "
                "margin-points policy through the standings settings."
            )
        elif (
            self.pk
            and self.points_mode == self.PointsMode.MARGIN_BASED
            and not getattr(self, "_allow_pending_margin_policy", False)
        ):
            try:
                from .standings import validate_pool_margin_point_rules

                validate_pool_margin_point_rules(
                    self,
                    self.margin_point_rules.all(),
                    require_complete=True,
                    # A historic open-ended terminal rule is read-compatible
                    # only while this competition's policy is untouched.  Any
                    # activation, point/race-target change, or repair write
                    # must first canonicalise it to a real finite maximum.
                    allow_legacy_open_ended=not margin_policy_configuration_changed,
                )
            except ValidationError as error:
                # The settings page does not edit ``points_mode`` directly.
                # Attach a target-change failure to the editable field and
                # leave the existing policy untouched; organisers can use the
                # documented fixed -> target -> margin workflow to configure
                # a new bounded final range explicitly.
                errors[
                    "default_race_target" if race_target_changed else "points_mode"
                ] = error.messages

        # Changing a configured competition between singles and teams must not
        # invalidate entries that already form part of its match history.
        if self.pk:
            entries = self.entries.all()
            original_format = original["format_type"] if original else None
            if original_format and original_format != self.format_type:
                errors["format_type"] = (
                    "Competition format cannot be changed after creation because "
                    "its explicit stages are already provisioned."
                )
            if (
                points_configuration_changed
                and competition_has_generated_group_qualification_bracket(self.pk)
            ):
                message = (
                    "Reset the generated qualification bracket before changing "
                    "the standings points configuration."
                )
                if original["points_mode"] != self.points_mode:
                    errors["points_mode"] = message
                if original["points_for_win"] != self.points_for_win:
                    errors["points_for_win"] = message
                if original["points_for_loss"] != self.points_for_loss:
                    errors["points_for_loss"] = message
            if (
                self.participant_mode == self.ParticipantMode.SINGLES
                and entries.filter(team__isnull=False).exists()
            ):
                errors["participant_mode"] = (
                    "A competition with team entries cannot be changed to singles."
                )
            if (
                self.participant_mode == self.ParticipantMode.TEAMS
                and entries.filter(player__isnull=False).exists()
            ):
                errors["participant_mode"] = (
                    "A competition with player entries cannot be changed to teams."
                )

            if original and original["allow_two_lives"] != self.allow_two_lives:
                has_life_dependency = (
                    PoolBracketDrawPreview.objects.filter(competition_id=self.pk).exists()
                    or PoolKnockoutBracket.objects.filter(competition_id=self.pk).exists()
                )
                if has_life_dependency:
                    errors["allow_two_lives"] = (
                        "Reset the existing draw preview or bracket before changing "
                        "the tournament-life setting."
                    )
                elif (
                    not self.allow_two_lives
                    and PoolCompetitionEntryLife.objects.filter(
                        competition_entry__competition_id=self.pk,
                        life_number=2,
                    ).exists()
                ):
                    errors["allow_two_lives"] = (
                        "Remove every second life before disabling two-life entries."
                    )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Serialise a default race-target/policy edit with fixture scope edits.
        # The same competition row is locked by PoolMatch before it validates
        # and persists an override, so two concurrent valid-looking changes
        # cannot jointly leave a bounded margin policy outside its real range.
        with transaction.atomic():
            if self._state.adding and not self.series_id:
                base_slug = f"{self.slug}-series" if self.slug else "pool-series"
                series_slug = base_slug[:255]
                if PoolCompetitionSeries.objects.filter(slug=series_slug).exists():
                    series_slug = f"{base_slug[:220]}-{uuid.uuid4().hex}"[:255]
                self.series = PoolCompetitionSeries.objects.create(
                    name=self.name,
                    slug=series_slug,
                )
            if self.pk:
                PoolCompetition.objects.select_for_update().filter(pk=self.pk).exists()
            self.full_clean()
            return super().save(*args, **kwargs)


class PoolCompetitionTieBreaker(models.Model):
    """One organiser-configured criterion after table points.

    Table points are deliberately not stored as a row: they are always the
    first criterion for every pool table.  The four optional rows are replaced
    atomically by the standings service, so administrators never need to swap
    unique ``order`` values in place.
    """

    class Criterion(models.TextChoices):
        MATCHES_WON = "MATCHES_WON", "Matches won"
        RACK_DIFFERENCE = "RACK_DIFFERENCE", "Rack difference"
        RACKS_WON = "RACKS_WON", "Racks won"
        HEAD_TO_HEAD = "HEAD_TO_HEAD", "Head-to-head"

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="tie_breakers",
    )
    criterion = models.CharField(max_length=24, choices=Criterion.choices)
    order = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "criterion"],
                name="pool_unique_tiebreaker_criterion",
            ),
            models.UniqueConstraint(
                fields=["competition", "order"],
                name="pool_unique_tiebreaker_order",
            ),
            models.CheckConstraint(
                condition=Q(order__gte=1),
                name="pool_tiebreaker_order_positive",
            ),
        ]

    def __str__(self):
        return f"{self.competition.name}: {self.get_criterion_display()}"

    def clean(self):
        if self.competition_id and competition_has_generated_group_qualification_bracket(
            self.competition_id
        ):
            raise ValidationError(
                "Reset the generated qualification bracket before changing tie-breakers."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolCompetitionMarginPointRuleQuerySet(models.QuerySet):
    """Protect an active margin policy from direct bulk-rule deletion."""

    _POLICY_FIELDS = frozenset(
        {
            "competition",
            "competition_id",
            "minimum_margin",
            "maximum_margin",
            "winner_points",
            "loser_points",
        }
    )

    def update(self, **kwargs):
        """Keep policy mutations inside model saves or atomic replacement.

        A queryset update has no complete before/after collection for the
        row-level model validator to inspect.  No application path needs it,
        so fail closed instead of allowing a raw update to bypass contiguous
        bounded-range validation.
        """

        if self._POLICY_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Bulk updates to margin-point rules are not supported; use "
                "the standings settings service."
            )
        return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            rule_ids = list(self.values_list("pk", flat=True))
            PoolCompetitionMarginPointRule._validate_active_margin_policy_after_removal(
                rule_ids
            )
            return super().delete()

    def _delete_for_atomic_replacement(self):
        """Delete already-validated rows inside the standings replacement service."""

        return super().delete()


class PoolCompetitionMarginPointRuleManager(
    models.Manager.from_queryset(PoolCompetitionMarginPointRuleQuerySet)
):
    pass


class PoolCompetitionMarginPointRule(models.Model):
    """Played-match table points for an inclusive winning-rack-margin band.

    The standings configuration service validates the complete set of bands
    atomically.  This model enforces row-local invariants and the
    qualification-bracket safety boundary; direct writes may only build an
    incomplete collection while the competition remains in fixed-points mode.
    """

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="margin_point_rules",
    )
    minimum_margin = models.PositiveSmallIntegerField()
    maximum_margin = models.PositiveSmallIntegerField(null=True, blank=True)
    winner_points = models.PositiveSmallIntegerField()
    loser_points = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PoolCompetitionMarginPointRuleManager()

    class Meta:
        ordering = ["minimum_margin", "id"]
        indexes = [
            models.Index(
                fields=["competition", "minimum_margin"],
                name="pool_margin_comp_min_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "minimum_margin"],
                name="pool_margin_rule_unique_min",
            ),
            models.UniqueConstraint(
                fields=["competition"],
                condition=Q(maximum_margin__isnull=True),
                name="pool_margin_rule_open_unique",
            ),
            models.CheckConstraint(
                condition=Q(minimum_margin__gte=1),
                name="pool_margin_min_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(maximum_margin__isnull=True)
                    | Q(maximum_margin__gte=models.F("minimum_margin"))
                ),
                name="pool_margin_max_after_min",
            ),
            models.CheckConstraint(
                condition=Q(winner_points__gte=0),
                name="pool_margin_winner_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(loser_points__gte=0),
                name="pool_margin_loser_nonnegative",
            ),
        ]

    def __str__(self):
        maximum = f"-{self.maximum_margin}" if self.maximum_margin else "+"
        return (
            f"{self.competition.name}: {self.minimum_margin}{maximum} "
            f"=> {self.winner_points}/{self.loser_points}"
        )

    def clean(self):
        errors = {}
        if self.minimum_margin is not None and self.minimum_margin < 1:
            errors["minimum_margin"] = "Minimum winning margin must be at least 1."
        if (
            self.minimum_margin is not None
            and self.maximum_margin is not None
            and self.maximum_margin < self.minimum_margin
        ):
            errors["maximum_margin"] = (
                "Maximum winning margin cannot be lower than the minimum margin."
            )
        if self.winner_points is not None and self.winner_points < 0:
            errors["winner_points"] = "Winner points cannot be negative."
        if self.loser_points is not None and self.loser_points < 0:
            errors["loser_points"] = "Loser points cannot be negative."

        original = (
            type(self).objects.filter(pk=self.pk)
            .values(
                "competition_id",
                "minimum_margin",
                "maximum_margin",
                "winner_points",
                "loser_points",
            )
            .first()
            if self.pk
            else None
        )
        has_changed = not original or any(
            original[field] != getattr(self, field)
            for field in (
                "competition_id",
                "minimum_margin",
                "maximum_margin",
                "winner_points",
                "loser_points",
            )
        )
        if original and original["competition_id"] != self.competition_id:
            # A policy belongs to exactly one competition.  The scoped manager
            # service replaces rows atomically and never needs to move one;
            # permitting a re-parent would be able to leave the former active
            # margin policy incomplete outside that service boundary.
            errors["competition"] = (
                "A margin-based points rule cannot be moved to another competition."
            )
        locked_competition_ids = {
            competition_id
            for competition_id in (
                self.competition_id,
                original["competition_id"] if original else None,
            )
            if competition_id
        }
        if has_changed and any(
            competition_has_generated_group_qualification_bracket(competition_id)
            for competition_id in locked_competition_ids
        ):
            errors["competition"] = (
                "Reset the generated qualification bracket before changing "
                "margin-based points rules."
            )

        if errors:
            raise ValidationError(errors)

    def _validate_active_margin_configuration(self, rules):
        """Reject direct edits that would leave an active policy incomplete.

        The standings service intentionally replaces complete collections with
        queryset/bulk operations inside one transaction.  This guard covers
        ordinary model/admin instance writes without getting in that atomic
        service path.
        """
        if not self.competition_id:
            return
        competition = PoolCompetition.objects.get(pk=self.competition_id)
        if competition.points_mode != PoolCompetition.PointsMode.MARGIN_BASED:
            return

        from .standings import validate_pool_margin_point_rules

        validate_pool_margin_point_rules(
            competition,
            rules,
            require_complete=True,
        )

    @classmethod
    def _validate_active_margin_policy_after_removal(cls, rule_ids):
        """Validate all policy rows remaining after one direct/bulk deletion.

        The atomic standings service validates a complete replacement before
        it removes anything, then uses its explicit private queryset bypass.
        Every ordinary model or admin delete comes through here instead, so it
        cannot leave a published active margin policy with a gap or no final
        winning-margin allocation.
        """

        rule_ids = {
            rule_id
            for rule_id in (rule_ids or ())
            if isinstance(rule_id, int) and not isinstance(rule_id, bool)
        }
        if not rule_ids:
            return

        rules_by_competition = {}
        for competition_id, rule_id in cls.objects.filter(
            pk__in=rule_ids
        ).values_list("competition_id", "pk"):
            rules_by_competition.setdefault(competition_id, set()).add(rule_id)

        for competition_id, removed_rule_ids in rules_by_competition.items():
            competition = PoolCompetition.objects.select_for_update().get(
                pk=competition_id
            )
            if competition_has_generated_group_qualification_bracket(competition_id):
                raise ValidationError(
                    "Reset the generated qualification bracket before deleting "
                    "a margin-based points rule."
                )
            if competition.points_mode != PoolCompetition.PointsMode.MARGIN_BASED:
                continue

            remaining_rules = list(
                cls.objects.select_for_update()
                .filter(competition_id=competition_id)
                .exclude(pk__in=removed_rule_ids)
            )
            from .standings import validate_pool_margin_point_rules

            validate_pool_margin_point_rules(
                competition,
                remaining_rules,
                require_complete=True,
            )

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.competition_id:
                PoolCompetition.objects.select_for_update().filter(
                    pk=self.competition_id
                ).exists()
            self.full_clean()
            existing_rules = list(
                type(self).objects.filter(competition_id=self.competition_id).exclude(
                    pk=self.pk
                )
            )
            self._validate_active_margin_configuration([*existing_rules, self])
            return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        with transaction.atomic():
            if self.pk:
                type(self)._validate_active_margin_policy_after_removal([self.pk])
            return super().delete(using=using, keep_parents=keep_parents)


class PoolCompetitionMembership(models.Model):
    """Competition-scoped ownership and administration permissions."""

    class Role(models.TextChoices):
        OWNER = "OWNER", "Competition Owner"
        ADMIN = "ADMIN", "Competition Admin"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pool_competition_memberships",
    )
    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["competition__name", "role", "user__username", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "competition"],
                name="pool_unique_user_competition_member",
            ),
        ]

    def __str__(self):
        return (
            f"{self.user} - {self.competition.name} - "
            f"{self.get_role_display()}"
        )

    def clean(self):
        if (
            self.role == self.Role.OWNER
            and self.competition_id
            and self.competition.owner_id != self.user_id
        ):
            raise ValidationError(
                {"role": "Only the competition's assigned owner may have owner role."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


def _lock_competition_roots_for_draw_dependency_check(competition_ids, *, using=None):
    """Lock competition roots before inspecting mutable draw dependencies.

    Bracket preview creation takes this same root lock before reading entries
    and tournament lives.  Keeping destructive entry/life operations behind
    it makes the JSON draw order safe under concurrent manager requests: one
    transaction either sees and blocks the preview, or removes the identity
    before a preview can read it.
    """

    competition_ids = sorted(
        {competition_id for competition_id in competition_ids if competition_id}
    )
    if not competition_ids:
        return []
    return list(
        PoolCompetition._base_manager.using(using)
        .select_for_update()
        .filter(pk__in=competition_ids)
        .order_by("pk")
    )


# A generated direct-knockout bracket normally freezes entrant identity.  The
# bracket service has one deliberately tiny exception: before round one has
# finished, it may create a *new* singles entry (and its lives) while it
# atomically consumes an untouched first-round BYE.  Keep that capability
# private to this module and context-local so ordinary manager, admin, import,
# and malicious POST paths cannot opt into it with a model keyword argument.
_late_knockout_entry_creation_competitions = ContextVar(
    "pool_late_knockout_entry_creation_competitions",
    default=frozenset(),
)


def _late_knockout_entry_creation_allowed(competition_id):
    return bool(
        competition_id
        and competition_id in _late_knockout_entry_creation_competitions.get()
    )


@contextmanager
def _late_knockout_entry_creation_scope(competition_id):
    """Permit one private bracket transaction to create a late BYE entrant.

    This is intentionally not part of a public model API.  The caller still
    has to hold the competition/bracket topology locks and use the bracket
    service's open-BYE validation; this scope merely prevents the generic
    entry/life draw-freeze guard from rejecting that already-authorised write.
    """

    current = _late_knockout_entry_creation_competitions.get()
    token = _late_knockout_entry_creation_competitions.set(
        frozenset((*current, competition_id))
    )
    try:
        yield
    finally:
        _late_knockout_entry_creation_competitions.reset(token)


def _assert_competition_entries_can_be_deleted(entry_ids, *, using=None):
    """Keep persisted draw identity from becoming dangling JSON.

    Draw previews deliberately retain their slot order as JSON so an organiser
    can inspect and confirm exactly what was previewed. That means a database
    FK cannot protect a preview from a direct entry deletion (and a cascading
    deletion of its lives would otherwise leave ``life_draw_order`` orphaned).
    A generated bracket is likewise immutable until it is reset.
    """

    entry_ids = {entry_id for entry_id in entry_ids if entry_id}
    if not entry_ids:
        return
    competition_ids = set(
        PoolCompetitionEntry._base_manager.using(using)
        .filter(pk__in=entry_ids)
        .values_list("competition_id", flat=True)
    )
    if not competition_ids:
        return
    _lock_competition_roots_for_draw_dependency_check(
        competition_ids,
        using=using,
    )
    # Recheck only after the root lock is held.  A preview service cannot
    # now persist a life/entry JSON reference between this test and deletion.
    if (
        PoolBracketDrawPreview.objects.using(using)
        .filter(competition_id__in=competition_ids)
        .exists()
        or PoolKnockoutBracket.objects.using(using)
        .filter(competition_id__in=competition_ids)
        .exists()
    ):
        raise ValidationError(
            "Reset the draw preview or bracket before deleting a competition entry."
        )


class PoolCompetitionEntryQuerySet(models.QuerySet):
    """Prevent bulk deletion from bypassing entry draw-dependency checks."""

    def update(self, **kwargs):
        raise ValidationError(
            "Update competition entries through their validated manager form or model save."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError(
            "Bulk competition-entry updates bypass tournament identity validation."
        )

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        """Create the mandatory Life 1 rows even for a validated bulk import.

        ``bulk_create`` deliberately bypasses ``PoolCompetitionEntry.save``.
        Retaining it for import callers is useful, but only if the entry and
        its stable first life are written in one competition-root transaction.
        """

        if ignore_conflicts or update_conflicts:
            raise ValidationError(
                "Conflict-tolerant bulk entry imports cannot guarantee a stable "
                "Life 1 identity; import validated rows without conflict options."
            )
        objects = list(objs)
        if not objects:
            return super().bulk_create(
                objects,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )
        database = self.db
        competition_ids = {
            obj.competition_id
            for obj in objects
            if getattr(obj, "competition_id", None)
        }
        with transaction.atomic(using=database):
            competitions = _lock_competition_roots_for_draw_dependency_check(
                competition_ids,
                using=database,
            )
            singles_competition_ids = {
                competition.pk
                for competition in competitions
                if competition.participant_mode
                == PoolCompetition.ParticipantMode.SINGLES
            }
            if competition_ids and (
                PoolBracketDrawPreview.objects.using(database)
                .filter(competition_id__in=competition_ids)
                .exists()
                or PoolKnockoutBracket.objects.using(database)
                .filter(competition_id__in=competition_ids)
                .exists()
            ):
                raise ValidationError(
                    "Reset the draw preview or bracket before adding a competition entry."
                )
            created = super().bulk_create(
                objects,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )
            entry_ids = [
                obj.pk
                for obj in created
                if obj.pk
                and obj.player_id
                and obj.competition_id in singles_competition_ids
            ]
            if entry_ids:
                existing_life_entry_ids = set(
                    PoolCompetitionEntryLife._base_manager.using(database)
                    .filter(
                        competition_entry_id__in=entry_ids,
                        life_number=1,
                    )
                    .values_list("competition_entry_id", flat=True)
                )
                PoolCompetitionEntryLife._base_manager.using(database).bulk_create(
                    [
                        PoolCompetitionEntryLife(
                            competition_entry_id=entry_id,
                            life_number=1,
                        )
                        for entry_id in entry_ids
                        if entry_id not in existing_life_entry_ids
                    ],
                    batch_size=batch_size,
                )
            return created

    def delete(self):
        with transaction.atomic(using=self.db):
            _assert_competition_entries_can_be_deleted(
                self.values_list("pk", flat=True),
                using=self.db,
            )
            return super().delete()


class PoolCompetitionEntryManager(
    models.Manager.from_queryset(PoolCompetitionEntryQuerySet)
):
    pass


class PoolCompetitionEntry(models.Model):
    """One player or one team participating in a pool competition."""

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="competition_entries",
        null=True,
        blank=True,
    )
    team = models.ForeignKey(
        PoolTeam,
        on_delete=models.PROTECT,
        related_name="competition_entries",
        null=True,
        blank=True,
    )
    seed = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PoolCompetitionEntryManager()

    class Meta:
        ordering = ["seed", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(player__isnull=False, team__isnull=True)
                    | Q(player__isnull=True, team__isnull=False)
                ),
                name="pool_entry_exactly_one_participant",
            ),
            models.UniqueConstraint(
                fields=["competition", "player"],
                condition=Q(player__isnull=False),
                name="pool_unique_player_entry_per_comp",
            ),
            models.UniqueConstraint(
                fields=["competition", "team"],
                condition=Q(team__isnull=False),
                name="pool_unique_team_entry_per_comp",
            ),
        ]

    def __str__(self):
        return f"{self.competition.name}: {self.display_name}"

    @property
    def participant(self):
        return self.player if self.player_id else self.team

    @property
    def display_name(self):
        participant = self.participant
        if not participant:
            return ""
        return participant.name if self.team_id else participant.full_name

    @property
    def display_image(self):
        participant = self.participant
        if not participant:
            return None
        return participant.logo if self.team_id else participant.profile_picture

    @property
    def is_player_entry(self):
        return bool(self.player_id)

    @property
    def is_team_entry(self):
        return bool(self.team_id)

    @property
    def tournament_life_count(self):
        """Return the configured number of bracket lives for this entry.

        A life is deliberately bracket metadata rather than a second player or
        competition entry. Every singles entry retains Life 1 as stable audit
        identity, while ordinary brackets simply leave their life references
        null and continue to operate entry-first.
        """

        if not self.player_id or not self.competition_id:
            return 1
        competition = self.competition
        if not competition.allow_two_lives:
            return 1
        return max(self.lives.count(), 1)

    def clean(self):
        errors = {}
        has_player = bool(self.player_id)
        has_team = bool(self.team_id)
        if has_player == has_team:
            errors["player"] = "Set exactly one participant: a player or a team."

        if self.competition_id:
            if (
                has_player
                and self.competition.participant_mode
                != PoolCompetition.ParticipantMode.SINGLES
            ):
                errors["player"] = "Team competitions accept team entries only."
            if (
                has_team
                and self.competition.participant_mode
                != PoolCompetition.ParticipantMode.TEAMS
            ):
                errors["team"] = "Singles competitions accept player entries only."
            if (
                self._state.adding
                and self.competition.is_money_match
                and self.competition.entries.exclude(pk=self.pk).count() >= 2
            ):
                errors["competition"] = "A Money Match has exactly two participants."

        if self.pk:
            original = type(self)._base_manager.filter(pk=self.pk).values(
                "seed",
                "player_id",
                "team_id",
                "competition_id",
            ).first()
            if original and original["competition_id"] != self.competition_id:
                errors["competition"] = (
                    "A competition entry cannot be moved between competitions. "
                    "Create a new entry in the destination competition instead."
                )
            if original and (
                original["player_id"] != self.player_id
                or original["team_id"] != self.team_id
            ):
                errors["player"] = (
                    "A competition entry's participant identity is permanent. "
                    "Create a separate entry instead of replacing its player or team."
                )
            if original and original["seed"] != self.seed and PoolKnockoutTieSlot.objects.filter(
                Q(source_entry_id=self.pk)
                | Q(source_group__memberships__entry_id=self.pk)
            ).exists():
                errors["seed"] = (
                    "Reset the generated bracket before changing the seed of an entry it uses."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        database = kwargs.get("using") or self._state.db
        with transaction.atomic(using=database):
            original_competition_id = (
                type(self)._base_manager.using(database)
                .filter(pk=self.pk)
                .values_list("competition_id", flat=True)
                .first()
                if self.pk
                else None
            )
            affected_competition_ids = {
                competition_id
                for competition_id in (
                    self.competition_id,
                    original_competition_id,
                )
                if competition_id
            }
            _lock_competition_roots_for_draw_dependency_check(
                affected_competition_ids,
                using=database,
            )
            is_private_late_bye_creation = bool(
                not self.pk
                and original_competition_id is None
                and _late_knockout_entry_creation_allowed(self.competition_id)
            )
            if (
                (not self.pk or original_competition_id != self.competition_id)
                and not is_private_late_bye_creation
                and affected_competition_ids
                and (
                    PoolBracketDrawPreview.objects.using(database)
                    .filter(competition_id__in=affected_competition_ids)
                    .exists()
                    or PoolKnockoutBracket.objects.using(database)
                    .filter(competition_id__in=affected_competition_ids)
                    .exists()
                )
            ):
                raise ValidationError(
                    "Reset the draw preview or bracket before changing competition entries."
                )
            self.full_clean()
            result = super().save(*args, **kwargs)
            if (
                self.player_id
                and self.competition.participant_mode
                == PoolCompetition.ParticipantMode.SINGLES
            ):
                PoolCompetitionEntryLife.objects.get_or_create(
                    competition_entry_id=self.pk,
                    life_number=1,
                )
            return result

    def delete(self, using=None, keep_parents=False):
        database = using or self._state.db
        with transaction.atomic(using=database):
            _assert_competition_entries_can_be_deleted(
                [self.pk],
                using=database,
            )
            return super().delete(using=using, keep_parents=keep_parents)


class PoolCompetitionEntryLifeQuerySet(models.QuerySet):
    """Keep bulk life deletion behind the same identity/draw safeguards."""

    def update(self, **kwargs):
        raise ValidationError(
            "Tournament-life changes must use the locked bracket lifecycle service."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError(
            "Bulk tournament-life updates bypass identity and elimination validation."
        )

    def bulk_create(self, objs, *args, **kwargs):
        raise ValidationError(
            "Create tournament lives through the competition entry workflow."
        )

    def delete(self):
        with transaction.atomic(using=self.db):
            if self.filter(life_number=1).exists():
                raise ValidationError(
                    "Life 1 is the permanent entry identity; delete the competition "
                    "entry instead."
                )
            competition_ids = set(
                self.values_list("competition_entry__competition_id", flat=True)
            )
            _lock_competition_roots_for_draw_dependency_check(
                competition_ids,
                using=self.db,
            )
            if competition_ids and (
                PoolBracketDrawPreview.objects.using(self.db).filter(
                    competition_id__in=competition_ids
                ).exists()
                or PoolKnockoutBracket.objects.using(self.db).filter(
                    competition_id__in=competition_ids
                ).exists()
            ):
                raise ValidationError(
                    "Reset the draw preview or bracket before changing tournament lives."
                )
            return super().delete()


class PoolCompetitionEntryLifeManager(
    models.Manager.from_queryset(PoolCompetitionEntryLifeQuerySet)
):
    pass


class PoolCompetitionEntryLife(models.Model):
    """One bracket life belonging to one real singles competition entry.

    The model intentionally never replaces ``PoolCompetitionEntry``.  Match
    and rack statistics remain anchored to that real entry/player while this
    row tracks only its independent knockout path.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ELIMINATED = "ELIMINATED", "Eliminated"
        CHAMPION = "CHAMPION", "Champion"

    competition_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.CASCADE,
        related_name="lives",
    )
    life_number = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    eliminated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PoolCompetitionEntryLifeManager()

    class Meta:
        ordering = ["competition_entry", "life_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition_entry", "life_number"],
                name="pool_unique_entry_life_number",
            ),
            models.CheckConstraint(
                condition=Q(life_number__in=[1, 2]),
                name="pool_entry_life_number_one_or_two",
            ),
        ]

    def __str__(self):
        return f"{self.competition_entry.display_name} - Life {self.life_number}"

    @property
    def display_name(self):
        return f"{self.competition_entry.display_name} - Life {self.life_number}"

    def clean(self):
        errors = {}
        if self.life_number not in {1, 2}:
            errors["life_number"] = "A tournament life must be numbered 1 or 2."
        if self.competition_entry_id:
            entry = self.competition_entry
            competition = entry.competition
            if not entry.player_id:
                errors["competition_entry"] = (
                    "Tournament lives are available only for singles player entries."
                )
            elif competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
                errors["competition_entry"] = (
                    "Tournament lives are available only for singles player entries."
                )
            elif self.life_number == 2 and (
                competition.format_type != PoolCompetition.FormatType.KNOCKOUT
                or not competition.allow_two_lives
            ):
                errors["life_number"] = (
                    "A second tournament life is available only in an enabled "
                    "singles knockout competition."
                )
            elif self.life_number == 2 and not type(self).objects.filter(
                competition_entry_id=self.competition_entry_id,
                life_number=1,
            ).exclude(pk=self.pk).exists():
                errors["life_number"] = (
                    "Create and retain Life 1 before adding Life 2."
                )
            if self.pk:
                original = type(self)._base_manager.filter(pk=self.pk).values(
                    "competition_entry_id",
                    "life_number",
                ).first()
                if (
                    original
                    and original["competition_entry_id"]
                    != self.competition_entry_id
                ):
                    errors["competition_entry"] = (
                        "A tournament life remains attached to its original "
                        "competition entry."
                    )
                if original and original["life_number"] != self.life_number:
                    errors["life_number"] = (
                        "A tournament life number is permanent; retain Life 1 and "
                        "create or remove only Life 2 through the entry workflow."
                    )
            elif (
                not _late_knockout_entry_creation_allowed(competition.pk)
                and (
                    PoolBracketDrawPreview.objects.filter(
                        competition_id=competition.pk
                    ).exists()
                    or PoolKnockoutBracket.objects.filter(
                        competition_id=competition.pk
                    ).exists()
                )
            ):
                errors["life_number"] = (
                    "Reset the draw preview or bracket before adding a tournament life."
                )
        if self.status == self.Status.ELIMINATED and self.eliminated_at is None:
            # The bracket service usually supplies the timestamp; retaining a
            # model-level fallback keeps direct/admin writes internally sound.
            self.eliminated_at = timezone.now()
        elif self.status != self.Status.ELIMINATED and self.eliminated_at is not None:
            errors["eliminated_at"] = (
                "Only an eliminated tournament life may have an elimination time."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        database = kwargs.get("using") or self._state.db
        with transaction.atomic(using=database):
            if self.competition_entry_id:
                competition_id = (
                    PoolCompetitionEntry._base_manager.using(database)
                    .filter(pk=self.competition_entry_id)
                    .values_list("competition_id", flat=True)
                    .first()
                )
                _lock_competition_roots_for_draw_dependency_check(
                    [competition_id],
                    using=database,
                )
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        if self.life_number == 1:
            raise ValidationError(
                "Life 1 is the permanent entry identity; delete the competition "
                "entry instead."
            )
        database = using or self._state.db
        with transaction.atomic(using=database):
            if self.competition_entry_id:
                competition_id = (
                    PoolCompetitionEntry._base_manager.using(database)
                    .filter(pk=self.competition_entry_id)
                    .values_list("competition_id", flat=True)
                    .first()
                )
                _lock_competition_roots_for_draw_dependency_check(
                    [competition_id],
                    using=database,
                )
                if (
                    PoolBracketDrawPreview.objects.using(database)
                    .filter(competition_id=competition_id)
                    .exists()
                    or PoolKnockoutBracket.objects.using(database)
                    .filter(competition_id=competition_id)
                    .exists()
                ):
                    raise ValidationError(
                        "Reset the draw preview or bracket before changing tournament lives."
                    )
            return super().delete(using=using, keep_parents=keep_parents)


class PoolTeamRosterQuerySet(models.QuerySet):
    """Keep roster identity changes on the validated model-save path.

    ``competition_entry`` carries the competition through a join, so a normal
    database unique constraint cannot express the rule that a player may only
    represent one team in that competition.  Creation and identity changes
    therefore need the row locks in ``PoolTeamRoster.save``.  Safe operational
    fields remain available to existing maintenance code without allowing a
    bulk import or admin action to move a player between teams behind that
    validation.
    """

    _SAFE_BULK_FIELDS = frozenset({"is_active", "sort_order", "updated_at"})

    def update(self, **kwargs):
        if not set(kwargs).issubset(self._SAFE_BULK_FIELDS):
            raise ValidationError(
                "Update team roster membership through the validated roster workflow."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if not set(fields).issubset(self._SAFE_BULK_FIELDS):
            raise ValidationError(
                "Bulk roster membership changes cannot change a player or team entry."
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError(
            "Bulk roster imports must use the validated team roster workflow."
        )


class PoolTeamRosterManager(models.Manager.from_queryset(PoolTeamRosterQuerySet)):
    pass


class PoolTeamRoster(models.Model):
    """A player's membership of a team's entry in one competition.

    Rosters belong to the team *entry*, not a global team roster.  That keeps
    a player list historically accurate and makes the future lineup engine
    competition-specific by design.
    """

    competition_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.CASCADE,
        related_name="roster_members",
    )
    player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="team_roster_memberships",
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PoolTeamRosterManager()

    class Meta:
        ordering = ["sort_order", "player__full_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition_entry", "player"],
                name="pool_unique_player_per_team_entry",
            ),
        ]

    def __str__(self):
        return f"{self.player.full_name} - {self.team.name}"

    @property
    def team(self):
        return self.competition_entry.team

    @property
    def competition(self):
        return self.competition_entry.competition

    def clean(self):
        errors = {}
        if self.competition_entry_id:
            entry = self.competition_entry
            if not entry.team_id:
                errors["competition_entry"] = (
                    "A team roster membership must belong to a team entry."
                )
            elif entry.competition.participant_mode != PoolCompetition.ParticipantMode.TEAMS:
                errors["competition_entry"] = (
                    "Roster membership is only available in team competitions."
                )
            elif self.player_id:
                other_membership = (
                    PoolTeamRoster._base_manager.filter(
                        competition_entry__competition_id=entry.competition_id,
                        player_id=self.player_id,
                    )
                    .exclude(pk=self.pk)
                    .exclude(competition_entry_id=entry.pk)
                    .select_related("competition_entry__team")
                    .order_by("pk")
                    .first()
                )
                if other_membership:
                    errors["player"] = (
                        f"{self.player.full_name} is already registered to "
                        f"{other_membership.competition_entry.team.name} in this competition."
                    )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        database = kwargs.get("using") or self._state.db
        with transaction.atomic(using=database):
            if self.pk:
                original = (
                    PoolTeamRoster._base_manager.using(database)
                    .select_for_update()
                    .filter(pk=self.pk)
                    .values("competition_entry_id", "player_id")
                    .first()
                )
                if original and (
                    original["competition_entry_id"] != self.competition_entry_id
                    or original["player_id"] != self.player_id
                ):
                    raise ValidationError(
                        "A roster membership's player and team entry cannot be changed. "
                        "Remove it and add the player through the validated roster workflow."
                    )

            if self.competition_entry_id and self.player_id:
                entry = PoolCompetitionEntry._base_manager.using(database).get(
                    pk=self.competition_entry_id
                )
                competition = (
                    PoolCompetition._base_manager.using(database)
                    .select_for_update()
                    .get(pk=entry.competition_id)
                )
                entry = (
                    PoolCompetitionEntry._base_manager.using(database)
                    .select_for_update()
                    .get(pk=entry.pk)
                )
                player = (
                    PoolPlayer._base_manager.using(database)
                    .select_for_update()
                    .get(pk=self.player_id)
                )

                # Keep validation on the locked, authoritative objects.  The
                # competition lock serialises two concurrent attempts to put
                # the same player on different teams, and the player lock
                # gives that invariant a stable per-player boundary too.
                entry._state.fields_cache["competition"] = competition
                self.competition_entry = entry
                self.player = player

            self.full_clean()
            return super().save(*args, **kwargs)


class PoolCompetitionStage(models.Model):
    """An ordered competition stage, kept explicit for future tables/brackets."""

    class StageType(models.TextChoices):
        LEAGUE = "LEAGUE", "League"
        GROUP = "GROUP", "Group"
        KNOCKOUT = "KNOCKOUT", "Knockout"
        MONEY = "MONEY", "Money Match"

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    name = models.CharField(max_length=100)
    stage_type = models.CharField(max_length=10, choices=StageType.choices)
    order = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "order"],
                name="pool_unique_stage_order_per_comp",
            ),
            models.UniqueConstraint(
                fields=["competition", "name"],
                name="pool_unique_stage_name_per_comp",
            ),
        ]

    def __str__(self):
        return f"{self.competition.name}: {self.name}"

    def clean(self):
        """Keep a persisted bracket's stage identity and ordering stable.

        The bracket graph records sources by explicit stage and round order.
        Letting an administrator turn that stage into a league/group stage, or
        move it before its qualifying stage, would make safe correction and
        qualification guarantees impossible.  A bracket must be reset through
        its owner-only service before such a structural edit.
        """

        errors = {}
        if self.competition_id:
            if self.competition.is_money_match and self.stage_type != self.StageType.MONEY:
                errors["stage_type"] = (
                    "A Money Match can only use its single Money Match stage."
                )
            elif (
                self.competition.is_money_match
                and self._state.adding
                and self.competition.stages.exclude(pk=self.pk).exists()
            ):
                errors["competition"] = "A Money Match has only one stage."
            elif (
                not self.competition.is_money_match
                and self.stage_type == self.StageType.MONEY
            ):
                errors["stage_type"] = (
                    "A Money Match stage belongs only to a Money Match competition."
                )
        if errors:
            raise ValidationError(errors)
        if not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            "stage_type", "order"
        ).first()
        if not original:
            return
        structure_changed = (
            original["stage_type"] != self.stage_type
            or original["order"] != self.order
        )
        has_existing_stage_structure = (
            self.matches.exists()
            or self.groups.exists()
            or self.group_memberships.exists()
            or self.knockout_round_definitions.exists()
            or PoolGroupQualificationRule.objects.filter(
                Q(source_stage_id=self.pk) | Q(knockout_stage_id=self.pk)
            ).exists()
        )
        is_generated_knockout = PoolKnockoutBracket.objects.filter(
            stage_id=self.pk
        ).exists()
        feeds_generated_bracket = PoolGroupQualificationRule.objects.filter(
            source_stage_id=self.pk,
            knockout_stage__knockout_bracket__isnull=False,
        ).exists()
        if structure_changed and (
            has_existing_stage_structure
            or is_generated_knockout
            or feeds_generated_bracket
        ):
            raise ValidationError(
                "A stage with fixtures, groups, qualification rules, or a generated bracket cannot change type or order."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        if self.pk and (
            PoolKnockoutBracket.objects.filter(stage_id=self.pk).exists()
            or PoolGroupQualificationRule.objects.filter(
                source_stage_id=self.pk,
                knockout_stage__knockout_bracket__isnull=False,
            ).exists()
        ):
            raise ValidationError(
                "Reset the generated bracket before deleting a stage used by it."
            )
        return super().delete(using=using, keep_parents=keep_parents)


class PoolKnockoutRoundDefinition(models.Model):
    """One canonical, manager-configured manual round in a knockout stage.

    These rows deliberately describe only round structure.  They neither
    create fixtures nor imply a persistent bracket topology, so a tournament
    can use fresh manual draws round by round and later still use the normal
    bracket workflow where that is safe.
    """

    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="knockout_round_definitions",
    )
    order = models.PositiveSmallIntegerField(
        help_text="Initial round first; the final is the last configured round.",
    )
    round_size = models.PositiveSmallIntegerField(
        help_text="Structural field size represented by this round.",
    )
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stage__order", "order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["stage", "order"],
                name="pool_unique_knockout_round_order",
            ),
            models.UniqueConstraint(
                fields=["stage", "round_size"],
                name="pool_unique_knockout_round_size",
            ),
            models.CheckConstraint(
                condition=Q(order__gte=1),
                name="pool_knockout_round_order_positive",
            ),
            models.CheckConstraint(
                condition=Q(round_size__in=SUPPORTED_KNOCKOUT_ROUND_SIZES),
                name="pool_knockout_round_supported_size",
            ),
        ]

    def __str__(self):
        return f"{self.stage.competition.name}: {self.name}"

    @property
    def competition(self):
        return self.stage.competition

    def clean(self):
        errors = {}
        if self.stage_id and self.stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
            errors["stage"] = "Knockout round definitions require a knockout stage."
        expected_name = canonical_knockout_round_name(self.round_size)
        if not expected_name:
            errors["round_size"] = (
                "Round size must be 2, 4, 8, 16, 32, 64, 128, or 256."
            )
        elif self.name != expected_name:
            errors["name"] = "Round names are generated from their structural size."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolGroup(models.Model):
    """An explicit group within one pool group stage.

    Entries join a group through :class:`PoolGroupEntry`; a group is therefore
    competition scoped through its stage without introducing a second type of
    participant.
    """

    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="groups",
    )
    name = models.CharField(max_length=100)
    order = models.PositiveSmallIntegerField(default=1)
    qualifiers = models.PositiveSmallIntegerField(
        default=2,
        help_text="Number of entries eligible to qualify from this group.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stage__order", "order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["stage", "name"],
                name="pool_unique_group_name_per_stage",
            ),
            models.UniqueConstraint(
                fields=["stage", "order"],
                name="pool_unique_group_order_per_stage",
            ),
            models.CheckConstraint(
                condition=Q(order__gte=1),
                name="pool_group_order_positive",
            ),
            models.CheckConstraint(
                condition=Q(qualifiers__gte=1),
                name="pool_group_qualifiers_positive",
            ),
        ]

    def __str__(self):
        return f"{self.stage.competition.name}: {self.name}"

    @property
    def competition(self):
        return self.stage.competition

    def clean(self):
        errors = {}
        if self.stage_id and self.stage.stage_type != PoolCompetitionStage.StageType.GROUP:
            errors["stage"] = "Groups can only belong to a group stage."
        if self.order is not None and self.order < 1:
            errors["order"] = "Group order must be at least 1."
        if self.qualifiers is not None and self.qualifiers < 1:
            errors["qualifiers"] = "At least one qualifier is required."
        if self.pk:
            highest_rule_position = (
                self.qualification_rules.order_by("-position")
                .values_list("position", flat=True)
                .first()
            )
            if highest_rule_position and self.qualifiers < highest_rule_position:
                errors["qualifiers"] = (
                    "This cannot be lower than an existing qualification rule position."
                )
            original = type(self).objects.filter(pk=self.pk).values(
                "stage_id", "name", "order", "qualifiers"
            ).first()
            if original and any(
                original[field] != getattr(self, field)
                for field in ("stage_id", "name", "order", "qualifiers")
            ) and PoolKnockoutTieSlot.objects.filter(source_group_id=self.pk).exists():
                errors["name"] = (
                    "Reset the generated bracket before changing a group used by qualification."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolGroupEntry(models.Model):
    """An entry assigned once to a particular group-stage table."""

    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    group = models.ForeignKey(
        PoolGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    entry = models.ForeignKey(
        PoolCompetitionEntry,
        # A direct entry deletion remains restricted while its group
        # membership exists, but a whole competition cascade can collect both
        # owned objects together.
        on_delete=models.RESTRICT,
        related_name="group_memberships",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "entry__seed", "entry__id"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "entry"],
                name="pool_unique_entry_per_group",
            ),
            models.UniqueConstraint(
                fields=["stage", "entry"],
                name="pool_unique_entry_per_group_stage",
            ),
        ]

    def __str__(self):
        return f"{self.group.name}: {self.entry.display_name}"

    def clean(self):
        errors = {}
        if self.group_id and self.stage_id and self.group.stage_id != self.stage_id:
            errors["group"] = "The group must belong to the selected stage."
        if self.stage_id:
            if self.stage.stage_type != PoolCompetitionStage.StageType.GROUP:
                errors["stage"] = "Group membership requires a group stage."
            if self.entry_id and self.entry.competition_id != self.stage.competition_id:
                errors["entry"] = "The entry must belong to this stage's competition."
        if self.group_id and PoolKnockoutTieSlot.objects.filter(
            source_group_id=self.group_id
        ).exists():
            original = (
                type(self).objects.filter(pk=self.pk)
                .values("stage_id", "group_id", "entry_id", "sort_order")
                .first()
                if self.pk
                else None
            )
            if original is None or any(
                original[field] != getattr(self, field)
                for field in ("stage_id", "group_id", "entry_id", "sort_order")
            ):
                errors["group"] = (
                    "Reset the generated bracket before changing a qualifying group assignment."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.group_id and not self.stage_id:
            self.stage_id = self.group.stage_id
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        if self.group_id and PoolKnockoutTieSlot.objects.filter(
            source_group_id=self.group_id
        ).exists():
            raise ValidationError(
                "Reset the generated bracket before removing a qualifying group assignment."
            )
        return super().delete(using=using, keep_parents=keep_parents)


class PoolStandingBaseline(models.Model):
    """Imported pre-eSportsLive totals for one exact pool table scope.

    Pool standings are normally reconstructed from finalised match records.
    A baseline is the deliberately small exception: it represents the part of
    a league or group table that happened before those records existed here.
    It is scoped to an entry *and* its table, never to the underlying player
    or team, so the same entrant can carry different imported totals in other
    competitions and stages.
    """

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="standing_baselines",
    )
    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="standing_baselines",
    )
    group = models.ForeignKey(
        PoolGroup,
        on_delete=models.CASCADE,
        related_name="standing_baselines",
        null=True,
        blank=True,
    )
    entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="standing_baselines",
    )
    played = models.PositiveSmallIntegerField(default=0)
    won = models.PositiveSmallIntegerField(default=0)
    lost = models.PositiveSmallIntegerField(default=0)
    racks_for = models.PositiveSmallIntegerField(default=0)
    racks_against = models.PositiveSmallIntegerField(default=0)
    points = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stage__order", "group__order", "entry__seed", "entry__id"]
        constraints = [
            # PostgreSQL treats NULL values as distinct in ordinary unique
            # constraints.  Keep league tables (which intentionally have no
            # group) safe with their own partial uniqueness rule.
            models.UniqueConstraint(
                fields=["stage", "entry"],
                condition=Q(group__isnull=True),
                name="pool_unique_league_baseline_entry",
            ),
            models.UniqueConstraint(
                fields=["stage", "group", "entry"],
                condition=Q(group__isnull=False),
                name="pool_unique_group_baseline_entry",
            ),
            models.CheckConstraint(
                condition=Q(played__gte=models.F("won") + models.F("lost")),
                name="pool_baseline_results_within_played",
            ),
        ]
        indexes = [
            models.Index(
                fields=["competition", "stage", "group"],
                name="pool_baseline_scope_idx",
            ),
        ]

    def __str__(self):
        scope = self.group.name if self.group_id else self.stage.name
        return f"{self.competition.name}: {scope} — {self.entry.display_name}"

    def clean(self):
        errors = {}
        if self.stage_id:
            if self.stage.stage_type not in {
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            }:
                errors["stage"] = "Baseline standings require a league or group stage."
            if self.competition_id and self.stage.competition_id != self.competition_id:
                errors["stage"] = "The baseline stage must belong to this competition."
        if self.entry_id and self.competition_id and (
            self.entry.competition_id != self.competition_id
        ):
            errors["entry"] = "The baseline entrant must belong to this competition."
        if self.group_id:
            if not self.stage_id or self.group.stage_id != self.stage_id:
                errors["group"] = "The baseline group must belong to the selected stage."
            elif self.stage.stage_type != PoolCompetitionStage.StageType.GROUP:
                errors["group"] = "Only group-stage baselines may select a group."
            elif self.entry_id and not PoolGroupEntry.objects.filter(
                stage_id=self.stage_id,
                group_id=self.group_id,
                entry_id=self.entry_id,
            ).exists():
                errors["entry"] = "The baseline entrant is not assigned to this group."
        elif self.stage_id and self.stage.stage_type == PoolCompetitionStage.StageType.GROUP:
            errors["group"] = "Select the group for a group-stage baseline."
        if self.played is not None and self.won is not None and self.lost is not None:
            if self.won + self.lost > self.played:
                errors["played"] = "Wins plus losses cannot exceed played."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolLeagueAuditEvent(models.Model):
    """Small immutable audit trail for imported tables and league schedules."""

    class Action(models.TextChoices):
        BASELINES_UPDATED = "BASELINES_UPDATED", "Baseline standings updated"
        FIRST_ROUND_GENERATED = "FIRST_ROUND_GENERATED", "First round fixtures generated"
        SECOND_ROUND_GENERATED = "SECOND_ROUND_GENERATED", "Second round fixtures generated"

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="league_audit_events",
    )
    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.SET_NULL,
        related_name="league_audit_events",
        null=True,
        blank=True,
    )
    group = models.ForeignKey(
        PoolGroup,
        on_delete=models.SET_NULL,
        related_name="league_audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pool_league_audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["competition", "stage", "group", "-created_at"],
                name="pool_league_audit_scope_idx",
            ),
        ]

    def __str__(self):
        return f"{self.competition.name}: {self.get_action_display()}"

    def clean(self):
        errors = {}
        if self.stage_id and self.stage.competition_id != self.competition_id:
            errors["stage"] = "The audit stage must belong to its competition."
        if self.group_id:
            if not self.stage_id or self.group.stage_id != self.stage_id:
                errors["group"] = "The audit group must belong to its stage."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolMatchQuerySet(models.QuerySet):
    """Keep bulk fixture deletion inside the same policy boundary as forms."""

    _MARGIN_SCOPE_FIELDS = frozenset(
        {
            "competition",
            "competition_id",
            "stage",
            "stage_id",
            "race_target",
            "table_count",
        }
    )

    def update(self, **kwargs):
        """Reject raw updates that would bypass prospective target validation."""

        if self._MARGIN_SCOPE_FIELDS.intersection(kwargs):
            raise ValidationError(
                "Bulk updates to a pool match's competition, stage, or race "
                "target/table count are not supported; save each fixture through its "
                "validated form or model boundary."
            )
        return super().update(**kwargs)

    def delete(self):
        # Django admin's bulk action calls QuerySet.delete() directly, which
        # would otherwise bypass PoolMatch.delete().  Validate the entire
        # prospective removal set before any row is deleted, so two high-target
        # fixtures cannot each appear safe while together reducing the policy
        # maximum below a saved finite band.
        with transaction.atomic():
            match_ids = list(self.values_list("pk", flat=True))
            handoff_sources = list(
                self.filter(knockout_round__isnull=False).values_list(
                    "knockout_round__stage_id", "knockout_round__order"
                )
            )
            for stage_id, round_order in handoff_sources:
                if PoolKnockoutBracket.objects.filter(
                    handoff_from_round__stage_id=stage_id,
                    handoff_from_round__order__gte=round_order,
                ).exists():
                    raise ValidationError(
                        "Reset the untouched handoff bracket before deleting its manual knockout source fixtures."
                    )
            PoolMatch._validate_margin_policy_after_table_matches_removed(match_ids)
            return super().delete()


class PoolMatchManager(models.Manager.from_queryset(PoolMatchQuerySet)):
    def _create_locked_money_match_fixture(
        self,
        *,
        competition,
        stage,
        home_entry,
        away_entry,
        race_target,
        scheduled_at=None,
        venue="",
    ):
        """Insert the one prevalidated Money Match fixture without generic save.

        This private lifecycle boundary is deliberately narrower than ordinary
        fixture creation.  Its caller must hold the Money Match competition,
        entry, and fixture locks and have established that the fixture scope
        is empty.  Re-running ``PoolMatch.save()`` at that point repeats a
        large full-clean graph, including foreign-key existence queries, while
        this method can validate the fixed singles-only shape directly.
        """

        if not isinstance(competition, PoolCompetition) or not competition.pk:
            raise ValidationError("A saved Money Match competition is required.")
        database = competition._state.db or "default"
        if not transaction.get_connection(using=database).in_atomic_block:
            raise ValidationError(
                "Money Match fixtures can be created only inside their lifecycle transaction."
            )
        if not competition.is_money_match:
            raise ValidationError("Only a Money Match can create this fixture.")
        if competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
            raise ValidationError("Money Matches are available for singles players only.")
        if not isinstance(stage, PoolCompetitionStage) or stage.competition_id != competition.pk:
            raise ValidationError("The Money Match stage must belong to this competition.")
        if stage.stage_type != PoolCompetitionStage.StageType.MONEY:
            raise ValidationError("A Money Match fixture requires the Money Match stage.")

        entries = (home_entry, away_entry)
        if any(
            not isinstance(entry, PoolCompetitionEntry)
            or not entry.pk
            or entry.competition_id != competition.pk
            or not entry.player_id
            or entry.team_id
            for entry in entries
        ):
            raise ValidationError("Money Matches require two singles player entries.")
        if home_entry.pk == away_entry.pk:
            raise ValidationError("A competitor cannot play itself.")
        if (
            not isinstance(race_target, int)
            or isinstance(race_target, bool)
            or race_target < 1
        ):
            raise ValidationError("Money Match race target must be a positive integer.")
        if self.filter(competition_id=competition.pk).exists():
            raise ValidationError(
                "A Money Match competition cannot contain more than one fixture."
            )

        fixture = self.model(
            competition=competition,
            stage=stage,
            is_money_match_fixture=True,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=scheduled_at,
            race_target=race_target,
            venue=venue,
        )
        # Call the base implementation directly.  The lifecycle validation
        # above and the model's database constraints protect this single,
        # locked INSERT without invoking the generic full-clean boundary.
        models.Model.save(fixture, force_insert=True, using=database)
        return fixture

    def _update_locked_bracket_race_targets(self, *, bracket_id, targets_by_match_id):
        """Persist bracket-owned target changes after the lifecycle preflight.

        This deliberately bypasses :meth:`PoolMatchQuerySet.update`, whose
        public guard prevents callers from changing a fixture target without
        the normal model validation boundary.  It is private to the generated
        bracket lifecycle: its caller must already hold the bracket, tie, and
        fixture locks, have checked competitive activity, and supply only
        matches linked to the locked bracket.
        """

        if not transaction.get_connection().in_atomic_block:
            raise ValidationError(
                "Bracket race targets can be updated only inside the bracket lifecycle transaction."
            )
        if not isinstance(bracket_id, int) or isinstance(bracket_id, bool):
            raise ValidationError("A saved bracket is required for a target update.")
        if not isinstance(targets_by_match_id, dict):
            raise ValidationError("Bracket fixture targets must be supplied by match.")

        normalised_targets = {}
        for match_id, target in targets_by_match_id.items():
            if (
                not isinstance(match_id, int)
                or isinstance(match_id, bool)
                or not isinstance(target, int)
                or isinstance(target, bool)
                or target < 1
            ):
                raise ValidationError("Bracket fixture race targets must be positive integers.")
            normalised_targets[match_id] = target
        if not normalised_targets:
            return 0

        match_ids = sorted(normalised_targets)
        linked_match_ids = set(
            PoolKnockoutTie.objects.filter(
                bracket_id=bracket_id,
                match_id__in=match_ids,
            ).values_list("match_id", flat=True)
        )
        if linked_match_ids != set(match_ids):
            raise ValidationError(
                "Bracket fixture targets may be changed only for matches linked to that bracket."
            )

        updated_count = 0
        now = timezone.now()
        target_groups = {}
        for match_id, target in normalised_targets.items():
            target_groups.setdefault(target, []).append(match_id)
        for target, target_match_ids in target_groups.items():
            # Call the base implementation directly.  The public queryset's
            # override remains in force for every ordinary ``.update()``.
            updated_count += models.QuerySet.update(
                self.get_queryset().filter(pk__in=target_match_ids),
                race_target=target,
                updated_at=now,
            )
        if updated_count != len(match_ids):
            raise ValidationError("A locked bracket fixture no longer exists.")
        return updated_count


class PoolMatch(models.Model):
    """A read-only-public pool fixture between two competition entries."""

    class Status(models.TextChoices):
        UPCOMING = "UPCOMING", "Upcoming"
        LIVE = "LIVE", "Live"
        FINISHED = "FINISHED", "Finished"
        POSTPONED = "POSTPONED", "Postponed"
        CANCELLED = "CANCELLED", "Cancelled"
        ABANDONED = "ABANDONED", "Abandoned"

    class ResultMethod(models.TextChoices):
        PLAYED = "PLAYED", "Played"
        WALKOVER = "WALKOVER", "Walkover"
        WITHDRAWAL = "WITHDRAWAL", "Withdrawal"
        DISQUALIFICATION = "DISQUALIFICATION", "Disqualification"
        BYE = "BYE", "Bye"

    class ResultEntryMethod(models.TextChoices):
        LIVE_RACK_SCORING = "LIVE_RACK_SCORING", "Live rack scoring"
        MANUAL_FINAL_RESULT = "MANUAL_FINAL_RESULT", "Manual final result"

    class CompletionReason(models.TextChoices):
        RACE_TARGET = "RACE_TARGET", "Race target reached"
        TIME_LIMIT = "TIME_LIMIT", "Time limit"

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.RESTRICT,
        related_name="matches",
    )
    is_money_match_fixture = models.BooleanField(
        default=False,
        editable=False,
        help_text="Internal marker for the one permitted Money Match fixture.",
    )
    group = models.ForeignKey(
        PoolGroup,
        on_delete=models.RESTRICT,
        related_name="matches",
        null=True,
        blank=True,
        help_text="Set only for fixtures that belong to an explicit group table.",
    )
    knockout_round = models.ForeignKey(
        PoolKnockoutRoundDefinition,
        on_delete=models.RESTRICT,
        related_name="matches",
        null=True,
        blank=True,
        help_text=(
            "Configured manual knockout round. Generated bracket fixtures retain "
            "their bracket round label instead."
        ),
    )
    home_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="home_pool_matches",
    )
    away_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="away_pool_matches",
    )
    # Tournament-life fields are intentionally nullable for all historic and
    # ordinary brackets. The real entry fields above remain the statistical
    # source of truth and keep the existing distinct-competitor constraint.
    home_entry_life = models.ForeignKey(
        PoolCompetitionEntryLife,
        on_delete=models.RESTRICT,
        related_name="home_pool_matches",
        null=True,
        blank=True,
    )
    away_entry_life = models.ForeignKey(
        PoolCompetitionEntryLife,
        on_delete=models.RESTRICT,
        related_name="away_pool_matches",
        null=True,
        blank=True,
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.UPCOMING,
    )
    race_target = models.PositiveSmallIntegerField(null=True, blank=True)
    table_count = models.PositiveSmallIntegerField(
        default=1,
        help_text=(
            "Number of physical tables available for this fixture. Team-match "
            "operations may run one eligible scheduled rack per table."
        ),
    )
    home_score = models.PositiveSmallIntegerField(default=0)
    away_score = models.PositiveSmallIntegerField(default=0)
    winner = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="won_pool_matches",
        null=True,
        blank=True,
    )
    winner_entry_life = models.ForeignKey(
        PoolCompetitionEntryLife,
        on_delete=models.RESTRICT,
        related_name="won_pool_matches",
        null=True,
        blank=True,
    )
    round_label = models.CharField(max_length=100, blank=True)
    gameweek = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="League gameweek when this fixture is part of a scheduled league leg.",
    )
    league_leg = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="1 for the generated first round, 2 for reverse fixtures.",
    )
    is_generated_league_fixture = models.BooleanField(
        default=False,
        help_text="Whether the validated league scheduler created this fixture.",
    )
    venue = models.CharField(max_length=255, blank=True)
    stream_url = models.URLField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    applied_time_limit_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "The competition timer copied when this fixture entered live play."
        ),
    )
    timer_snapshot_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this fixture first entered live play and its timer policy "
            "was frozen, including an intentionally unlimited policy."
        ),
    )
    timer_started_at = models.DateTimeField(null=True, blank=True)
    timer_deadline = models.DateTimeField(null=True, blank=True)
    time_expired_tied = models.BooleanField(
        default=False,
        help_text=(
            "The countdown expired level; the next valid rack is the one "
            "deciding rack."
        ),
    )
    completion_reason = models.CharField(
        max_length=16,
        choices=CompletionReason.choices,
        blank=True,
        default="",
        help_text="Why a played fixture was completed, when applicable.",
    )
    is_in_correction_mode = models.BooleanField(
        default=False,
        help_text=(
            "Allows an authorised manager to correct a completed result through "
            "the pool result service."
        ),
    )
    result_method = models.CharField(
        max_length=20,
        choices=ResultMethod.choices,
        default=ResultMethod.PLAYED,
    )
    result_entry_method = models.CharField(
        max_length=24,
        choices=ResultEntryMethod.choices,
        default=ResultEntryMethod.LIVE_RACK_SCORING,
        help_text=(
            "Whether a played result came from live rack scoring or an "
            "authorised aggregate final-result entry."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PoolMatchManager()

    class Meta:
        ordering = ["scheduled_at", "id"]
        indexes = [
            models.Index(fields=["competition", "status", "scheduled_at"], name="pool_match_comp_status_idx"),
            models.Index(fields=["stage", "scheduled_at"], name="pool_match_stage_date_idx"),
            models.Index(
                fields=["stage", "group", "league_leg", "gameweek"],
                name="pool_match_league_schedule_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(home_entry=models.F("away_entry")),
                name="pool_match_distinct_competitors",
            ),
            models.CheckConstraint(
                condition=Q(home_score__gte=0) & Q(away_score__gte=0),
                name="pool_match_scores_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(race_target__isnull=True) | Q(race_target__gte=1),
                name="pool_match_race_target_positive",
            ),
            models.CheckConstraint(
                condition=Q(table_count__gte=1) & Q(table_count__lte=5),
                name="pool_match_table_count_between_one_and_five",
            ),
            models.CheckConstraint(
                condition=Q(league_leg__isnull=True) | Q(league_leg__in=[1, 2]),
                name="pool_match_league_leg_valid",
            ),
            models.CheckConstraint(
                condition=Q(gameweek__isnull=True) | Q(gameweek__gte=1),
                name="pool_match_gameweek_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(applied_time_limit_minutes__isnull=True)
                    | Q(applied_time_limit_minutes__gte=1)
                ),
                name="pool_match_applied_time_limit_positive",
            ),
            models.UniqueConstraint(
                fields=["competition"],
                condition=Q(is_money_match_fixture=True),
                name="pool_unique_money_match_fixture",
            ),
        ]

    def __str__(self):
        return f"{self.home_entry.display_name} vs {self.away_entry.display_name}"

    @property
    def home_participant(self):
        return self.home_entry.participant

    @property
    def away_participant(self):
        return self.away_entry.participant

    def _participant_display_name(self, entry, entry_life):
        """Return one public-safe name for a fixture side.

        Tournament lives are match identity, not a second competition entry.
        Showing the life only when it is persisted on this exact fixture keeps
        ordinary singles, team fixtures, and historic fixtures unchanged.
        """

        if not entry:
            return ""
        if entry.player_id and entry_life:
            return f"{entry.display_name} \u2014 Life {entry_life.life_number}"
        return entry.display_name

    @property
    def home_participant_display_name(self):
        return self._participant_display_name(self.home_entry, self.home_entry_life)

    @property
    def away_participant_display_name(self):
        return self._participant_display_name(self.away_entry, self.away_entry_life)

    @property
    def winner_participant_display_name(self):
        return self._participant_display_name(self.winner, self.winner_entry_life)

    @property
    def has_stream(self):
        """Whether this public fixture has a configured stream destination."""

        return bool((self.stream_url or "").strip())

    @property
    def fixture_filter_status(self):
        """Map persisted lifecycle states to the manager fixture filters."""

        if self.status in (self.Status.UPCOMING, self.Status.POSTPONED):
            return "upcoming"
        if self.status == self.Status.LIVE:
            return "live"
        return "completed"

    @staticmethod
    def fixture_round_term_for(competition, stage):
        """Return the public name for persisted table-round metadata."""

        if (
            competition
            and stage
            and competition.format_type
            == PoolCompetition.FormatType.LEAGUE_KNOCKOUT
            and stage.stage_type == PoolCompetitionStage.StageType.GROUP
        ):
            return "Game"
        return "Gameweek"

    @property
    def fixture_round_term(self):
        return self.fixture_round_term_for(self.competition, self.stage)

    @property
    def fixture_round_display(self):
        if self.gameweek is None:
            return ""
        if self.fixture_round_term == "Game":
            return f"Game {self.gameweek}"
        return f"GW {self.gameweek}"

    @property
    def display_round_name(self):
        """Return one safe, public-facing label for a knockout fixture.

        A configured manual round takes precedence.  Historic and generated
        bracket fixtures continue to use their persisted ``round_label``;
        a legacy knockout row with neither safely falls back to its stage.
        Non-knockout fixtures deliberately return an empty string so their
        existing league/group context remains unchanged.
        """

        if self.knockout_round_id:
            return self.knockout_round.name
        if self.round_label:
            return self.round_label
        if (
            self.stage_id
            and self.stage.stage_type == PoolCompetitionStage.StageType.KNOCKOUT
        ):
            return self.stage.name or self.stage.get_stage_type_display()
        return ""

    @property
    def stage_context_label(self):
        """Return the single template-facing stage/group/round context label."""

        if self.group_id:
            return f"Group stage · {self.group.name}"
        round_name = self.display_round_name
        if round_name:
            return round_name
        if self.stage_id:
            return self.stage.get_stage_type_display()
        return ""

    @property
    def is_completed(self):
        return self.status == self.Status.FINISHED

    @property
    def is_live_scoring_available(self):
        """Whether this fixture may still be opened in the live scorer.

        A completed result, regardless of whether it came from rack-by-rack
        scoring or an authorised manual final-result entry, is immutable from
        the live-scoring workflow.  Result correction remains a separate,
        manager-only operation.
        """
        return self.status in {
            self.Status.UPCOMING,
            self.Status.LIVE,
            self.Status.POSTPONED,
        }

    @property
    def result_is_locked(self):
        """Whether a completed result is read-only until correction is opened."""
        return self.is_completed and not self.is_in_correction_mode

    @property
    def result_correction_open(self):
        """Template-friendly name for an explicitly opened result correction."""
        return self.is_in_correction_mode

    @property
    def has_match_timer(self):
        return bool(
            self.applied_time_limit_minutes
            and self.timer_started_at
            and self.timer_deadline
        )

    @property
    def timer_is_active(self):
        return self.status == self.Status.LIVE and self.has_match_timer

    @property
    def timer_has_expired(self):
        return bool(
            self.timer_is_active
            and self.timer_deadline
            and self.timer_deadline <= timezone.now()
        )

    @property
    def timer_remaining_seconds(self):
        if not self.timer_is_active or not self.timer_deadline:
            return None
        return max(int((self.timer_deadline - timezone.now()).total_seconds()), 0)

    @property
    def team_scorer_assignments(self):
        """Backward-compatible accessor for the pre-generalisation relation."""

        return self.scorer_assignments

    def rack_score_totals(self):
        """Return the authoritative score implied by persisted rack results."""
        if not self.pk or not self.home_entry_id or not self.away_entry_id:
            return 0, 0

        totals = self.racks.aggregate(
            home=Count("pk", filter=Q(winner_id=self.home_entry_id)),
            away=Count("pk", filter=Q(winner_id=self.away_entry_id)),
        )
        return totals["home"] or 0, totals["away"] or 0

    @classmethod
    def _lock_margin_policy_competitions(cls, competition_ids):
        """Lock affected competitions in a stable order when a write is atomic.

        A competition row is the policy root shared by all league/group
        fixtures.  Serialising scope-changing writes through that row prevents
        two independently valid target reductions from jointly invalidating a
        finite policy.  ``full_clean()`` remains usable outside a transaction,
        where this intentionally performs an ordinary fresh read only.
        """

        competition_ids = sorted(
            {
                competition_id
                for competition_id in (competition_ids or ())
                if isinstance(competition_id, int)
                and not isinstance(competition_id, bool)
            }
        )
        if not competition_ids:
            return {}
        queryset = PoolCompetition.objects.filter(pk__in=competition_ids).order_by(
            "pk"
        )
        if transaction.get_connection().in_atomic_block:
            queryset = queryset.select_for_update()
        return {competition.pk: competition for competition in queryset}

    @classmethod
    def _stage_is_table_stage(cls, stage_id):
        return bool(
            stage_id
            and PoolCompetitionStage.objects.filter(
                pk=stage_id,
                stage_type__in=(
                    PoolCompetitionStage.StageType.LEAGUE,
                    PoolCompetitionStage.StageType.GROUP,
                ),
            ).exists()
        )

    @classmethod
    def _validate_active_margin_policy_for_prospective_scopes(
        cls,
        scopes,
        *,
        locked_competitions=None,
    ):
        """Fail closed when an edit/removal changes a table-target maximum.

        ``scopes`` maps a competition id to the persisted table fixtures that
        are about to disappear and the new table targets that are about to be
        introduced.  It intentionally runs from model/queryset boundaries so
        an admin bulk delete, a direct ORM edit, and a manager form all share
        the exact central bounded-margin policy.
        """

        from .standings import (
            maximum_meaningful_pool_margin,
            validate_pool_margin_point_rules,
        )

        competitions = (
            locked_competitions
            if locked_competitions is not None
            else cls._lock_margin_policy_competitions(scopes)
        )
        for competition_id in sorted(scopes):
            scope = scopes[competition_id]
            competition = competitions.get(competition_id)
            if competition is None:
                raise PoolCompetition.DoesNotExist(
                    f"Pool competition {competition_id} does not exist."
                )
            if competition.points_mode != PoolCompetition.PointsMode.MARGIN_BASED:
                continue
            maximum_margin = maximum_meaningful_pool_margin(
                competition,
                exclude_match_ids=scope["exclude_match_ids"],
                pending_race_targets=scope["pending_race_targets"],
            )
            validate_pool_margin_point_rules(
                competition,
                competition.margin_point_rules.all(),
                require_complete=True,
                maximum_margin=maximum_margin,
            )

    @classmethod
    def _validate_margin_policy_after_table_matches_removed(cls, match_ids):
        """Validate the post-delete maximum for one direct/bulk deletion."""

        match_ids = {
            match_id
            for match_id in (match_ids or ())
            if isinstance(match_id, int) and not isinstance(match_id, bool)
        }
        if not match_ids:
            return
        scopes = {}
        for competition_id, match_id in cls.objects.filter(
            pk__in=match_ids,
            stage__stage_type__in=(
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            ),
        ).values_list("competition_id", "pk"):
            scope = scopes.setdefault(
                competition_id,
                {"exclude_match_ids": set(), "pending_race_targets": []},
            )
            scope["exclude_match_ids"].add(match_id)
        if scopes:
            cls._validate_active_margin_policy_for_prospective_scopes(scopes)

    def _validate_margin_policy_for_scope_change(self):
        """Validate this fixture as if its new stage/target were already saved."""

        original = (
            type(self).objects.filter(pk=self.pk)
            .values("competition_id", "stage_id")
            .first()
            if self.pk
            else None
        )
        current_is_table = bool(
            self.competition_id
            and self.stage_id
            and self.stage.competition_id == self.competition_id
            and self._stage_is_table_stage(self.stage_id)
        )
        original_is_table = bool(
            original and self._stage_is_table_stage(original["stage_id"])
        )
        scopes = {}

        def scope_for(competition_id):
            return scopes.setdefault(
                competition_id,
                {"exclude_match_ids": set(), "pending_race_targets": []},
            )

        if original_is_table:
            scope_for(original["competition_id"])["exclude_match_ids"].add(self.pk)
        if current_is_table:
            scope_for(self.competition_id)["pending_race_targets"].append(
                self.race_target
            )
        if scopes:
            self._validate_active_margin_policy_for_prospective_scopes(scopes)
        return current_is_table, original_is_table

    def clean(self):
        errors = {}

        if self.competition_id and self.stage_id:
            if self.stage.competition_id != self.competition_id:
                errors["stage"] = "The selected stage does not belong to this competition."
            elif self.competition.is_money_match:
                if self.stage.stage_type != PoolCompetitionStage.StageType.MONEY:
                    errors["stage"] = (
                        "A Money Match fixture must use the Money Match stage."
                    )
                if not self.is_money_match_fixture:
                    errors["is_money_match_fixture"] = (
                        "A Money Match competition can contain only its single configured fixture."
                    )
                elif (
                    type(self).objects.filter(
                        competition_id=self.competition_id,
                        is_money_match_fixture=True,
                    )
                    .exclude(pk=self.pk)
                    .exists()
                ):
                    errors["competition"] = (
                        "A Money Match competition cannot contain more than one fixture."
                    )
            elif self.stage.stage_type == PoolCompetitionStage.StageType.MONEY:
                errors["stage"] = "A Money Match stage requires a Money Match competition."
            elif self.is_money_match_fixture:
                errors["is_money_match_fixture"] = (
                    "Only a Money Match competition can use a Money Match fixture."
                )

        if self.group_id:
            if not self.stage_id or self.group.stage_id != self.stage_id:
                errors["group"] = "The group must belong to this match's stage."
            elif self.stage.stage_type != PoolCompetitionStage.StageType.GROUP:
                errors["group"] = "Only group-stage matches may have a group."
            elif self.group.stage.competition_id != self.competition_id:
                errors["group"] = "The group must belong to this competition."
        elif (
            self.stage_id
            and self.stage.stage_type == PoolCompetitionStage.StageType.GROUP
        ):
            errors["group"] = "A group-stage match must belong to a group."

        if self.competition_id and self.competition.is_money_match:
            if self.group_id:
                errors["group"] = "A Money Match fixture cannot belong to a group."
            if self.knockout_round_id:
                errors["knockout_round"] = (
                    "A Money Match fixture cannot belong to a knockout round."
                )
            if self.gameweek is not None or self.league_leg is not None:
                errors["gameweek"] = (
                    "A Money Match fixture cannot use league scheduling metadata."
                )
            if self.is_generated_league_fixture:
                errors["is_generated_league_fixture"] = (
                    "A Money Match fixture cannot be generated by the league scheduler."
                )

        if self.is_generated_league_fixture:
            if not self.stage_id or self.stage.stage_type not in {
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            }:
                errors["is_generated_league_fixture"] = (
                    "Only league or group fixtures can be generated by the league scheduler."
                )
            if self.league_leg not in {1, 2}:
                errors["league_leg"] = "Generated league fixtures require leg 1 or 2."
            if not self.gameweek:
                errors["gameweek"] = "Generated league fixtures require a gameweek."
        elif self.league_leg is not None:
            errors["league_leg"] = (
                "League-leg metadata is reserved for fixtures generated by the league scheduler."
            )

        if self.gameweek is not None and (
            not self.stage_id
            or self.stage.stage_type
            not in {
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            }
        ):
            errors["gameweek"] = (
                "Gameweeks are available only for league or group-stage fixtures."
            )

        if self.knockout_round_id:
            knockout_round = self.knockout_round
            if not self.stage_id or (
                self.stage.stage_type
                != PoolCompetitionStage.StageType.KNOCKOUT
            ):
                errors["knockout_round"] = (
                    "A configured knockout round can be used only by a knockout fixture."
                )
            elif knockout_round.stage_id != self.stage_id:
                errors["knockout_round"] = (
                    "The selected knockout round must belong to this fixture's stage."
                )
            elif knockout_round.stage.competition_id != self.competition_id:
                errors["knockout_round"] = (
                    "The selected knockout round must belong to this competition."
                )
            elif PoolKnockoutBracket.objects.filter(
                handoff_from_round__stage_id=knockout_round.stage_id,
                handoff_from_round__order__gte=knockout_round.order,
            ).exists():
                errors["knockout_round"] = (
                    "This manual knockout round is locked by a generated handoff bracket. "
                    "Reset that untouched bracket before changing its source fixtures."
                )

        if self.pk:
            original_round_values = type(self).objects.filter(pk=self.pk).values(
                "knockout_round_id",
                "status",
            ).first()
            if (
                original_round_values
                and original_round_values["knockout_round_id"]
                != self.knockout_round_id
            ):
                has_meaningful_activity = (
                    original_round_values["status"] != self.Status.UPCOMING
                    or self.status != self.Status.UPCOMING
                    or self.racks.exists()
                )
                if has_meaningful_activity:
                    errors["knockout_round"] = (
                        "A fixture with competitive activity cannot move between knockout rounds."
                    )
                elif PoolKnockoutTie.objects.filter(match_id=self.pk).exists():
                    errors["knockout_round"] = (
                        "A generated bracket fixture retains the round owned by its bracket tie."
                    )

        entry_fields = ("home_entry", "away_entry")
        for field_name in entry_fields:
            entry_id = getattr(self, f"{field_name}_id")
            if entry_id and self.competition_id:
                entry = getattr(self, field_name)
                if entry.competition_id != self.competition_id:
                    errors[field_name] = (
                        "Both competitors must be entries in this competition."
                    )

        if self.group_id:
            group_entry_ids = set(
                PoolGroupEntry.objects.filter(group_id=self.group_id).values_list(
                    "entry_id", flat=True
                )
            )
            for field_name in entry_fields:
                entry_id = getattr(self, f"{field_name}_id")
                if entry_id and entry_id not in group_entry_ids:
                    errors[field_name] = "Both competitors must be members of this group."

        if self.home_entry_id and self.away_entry_id:
            if self.home_entry_id == self.away_entry_id:
                errors["away_entry"] = "A competitor cannot play itself."

        for side, entry_id, life_id in (
            ("home", self.home_entry_id, self.home_entry_life_id),
            ("away", self.away_entry_id, self.away_entry_life_id),
        ):
            if life_id:
                life = getattr(self, f"{side}_entry_life")
                if life.competition_entry_id != entry_id:
                    errors[f"{side}_entry_life"] = (
                        f"The {side} tournament life must belong to the {side} entry."
                    )
                elif not life.competition_entry.competition.allow_two_lives:
                    errors[f"{side}_entry_life"] = (
                        "Tournament-life match metadata requires an enabled two-life "
                        "competition."
                    )
        if self.home_entry_life_id and self.away_entry_life_id and (
            self.home_entry_life_id == self.away_entry_life_id
        ):
            errors["away_entry_life"] = (
                "The same tournament life cannot occupy both sides of a match."
            )

        if self.race_target is None and self.competition_id:
            self.race_target = self.competition.default_race_target
        elif self.race_target is not None and self.race_target < 1:
            errors["race_target"] = "Race target must be at least 1."

        if self.table_count is None or not 1 <= self.table_count <= 5:
            errors["table_count"] = "Table count must be between 1 and 5."

        # A generated team score sheet is a persistent operational contract:
        # its live table assignments and clinching behaviour are based on the
        # parent target and physical-table capacity.  Do not let an ordinary
        # model/form save silently rewrite either setting after that topology
        # exists.  Legacy team fixtures have no schedule and remain editable.
        if (
            self.pk
            and self.competition_id
            and self.competition.participant_mode
            == PoolCompetition.ParticipantMode.TEAMS
            and PoolTeamMatchScheduledRack.objects.filter(match_id=self.pk).exists()
        ):
            original_operation_values = type(self).objects.filter(pk=self.pk).values(
                "race_target",
                "table_count",
            ).first()
            if original_operation_values:
                if self.race_target != original_operation_values["race_target"]:
                    errors["race_target"] = (
                        "The race target cannot change after the team score sheet has been generated."
                    )
                if self.table_count != original_operation_values["table_count"]:
                    errors["table_count"] = (
                        "The table count cannot change after the team score sheet has been generated."
                    )

        if (
            self.competition_id
            and self.competition.participant_mode
            == PoolCompetition.ParticipantMode.TEAMS
            and self.race_target is not None
            and self.race_target > 13
        ):
            errors["race_target"] = (
                "A team match race target cannot exceed 13: a 25-rack, "
                "no-draw schedule must be able to produce a winner."
            )

        # Margin points are one competition-wide policy for every league and
        # group table.  Validate the prospective scope, not merely the saved
        # row: lowering a target or moving a fixture out of table play can
        # otherwise leave a finite policy extending beyond every remaining
        # meaningful margin.  Knockout round targets intentionally remain
        # outside standings configuration.
        if (
            self.competition_id
            and self.stage_id
            and self.race_target is not None
            and not getattr(self, "_skip_margin_policy_validation", False)
        ):
            try:
                self._validate_margin_policy_for_scope_change()
            except ValidationError as error:
                errors["race_target"] = error.messages

        if self.home_score is not None and self.home_score < 0:
            errors["home_score"] = "Scores cannot be negative."
        if self.away_score is not None and self.away_score < 0:
            errors["away_score"] = "Scores cannot be negative."

        participant_ids = {self.home_entry_id, self.away_entry_id}
        participant_ids.discard(None)
        if self.winner_id and self.winner_id not in participant_ids:
            errors["winner"] = "The winner must be a participant in this match."
        elif self.winner_id and self.competition_id:
            if self.winner.competition_id != self.competition_id:
                errors["winner"] = "The winner must belong to this competition."
        if self.winner_entry_life_id:
            winner_life = self.winner_entry_life
            if not self.winner_id:
                errors["winner_entry_life"] = (
                    "A winning tournament life requires a declared match winner."
                )
            elif winner_life.competition_entry_id != self.winner_id:
                errors["winner_entry_life"] = (
                    "The winning tournament life must belong to the declared winner."
                )
            elif winner_life.pk not in {
                self.home_entry_life_id,
                self.away_entry_life_id,
            }:
                errors["winner_entry_life"] = (
                    "The winning tournament life must be playing in this match."
                )

        timer_fields_present = any(
            value is not None
            for value in (
                self.applied_time_limit_minutes,
                self.timer_started_at,
                self.timer_deadline,
            )
        )
        if timer_fields_present and not all(
            value is not None
            for value in (
                self.applied_time_limit_minutes,
                self.timer_started_at,
                self.timer_deadline,
            )
        ):
            errors["applied_time_limit_minutes"] = (
                "A timed match requires a start time, deadline, and applied duration."
            )
        if timer_fields_present and self.timer_snapshot_at is None:
            errors["timer_snapshot_at"] = (
                "A timed match must retain the timestamp of its first timer snapshot."
            )
        if (
            self.applied_time_limit_minutes is not None
            and self.applied_time_limit_minutes < 1
        ):
            errors["applied_time_limit_minutes"] = (
                "An applied match time limit must be at least one minute."
            )
        if (
            self.timer_started_at
            and self.timer_deadline
            and self.timer_deadline <= self.timer_started_at
        ):
            errors["timer_deadline"] = "The timer deadline must be after the start time."
        if self.time_expired_tied:
            if self.status != self.Status.LIVE:
                errors["time_expired_tied"] = (
                    "Only a live fixture can wait for a time-limit deciding rack."
                )
            elif not self.has_match_timer:
                errors["time_expired_tied"] = (
                    "A deciding-rack state requires an applied match timer."
                )
            elif self.home_score != self.away_score:
                errors["time_expired_tied"] = (
                    "A deciding-rack state is valid only while the score is tied."
                )

        if self.result_entry_method == self.ResultEntryMethod.MANUAL_FINAL_RESULT:
            if (
                self.status != self.Status.FINISHED
                or self.result_method != self.ResultMethod.PLAYED
            ):
                errors["result_entry_method"] = (
                    "A manual final-result entry must be a finished played match."
                )
            elif self.pk and self.racks.exists():
                errors["result_entry_method"] = (
                    "A manual final result cannot contain rack-by-rack history."
                )
        elif self.pk:
            rack_home_score, rack_away_score = self.rack_score_totals()
            if self.home_score != rack_home_score:
                errors["home_score"] = (
                    "The home score must be synchronised from the recorded racks."
                )
            if self.away_score != rack_away_score:
                errors["away_score"] = (
                    "The away score must be synchronised from the recorded racks."
                )

        # A bye is structural bracket state.  It never represents a normal
        # persisted fixture/result because that would create an apparent race
        # with no rack history and could leak into derived statistics.
        if self.result_method == self.ResultMethod.BYE:
            errors["result_method"] = (
                "A bye is bracket topology, not a persisted pool match result."
            )

        # A walkover awards a result but deliberately has no invented rack
        # score.  Enforce this at the model boundary as well as in the result
        # service so imports/admin code cannot pollute authoritative totals.
        if self.result_method == self.ResultMethod.WALKOVER:
            if self.home_score or self.away_score:
                errors["home_score"] = (
                    "A walkover must retain a 0-0 score and no invented racks."
                )
            elif self.pk and self.racks.exists():
                errors["result_method"] = (
                    "A walkover cannot contain recorded rack history."
                )

        if self.status == self.Status.FINISHED:
            if not self.winner_id:
                errors["winner"] = "A finished match must have a winner."
            if self.result_method == self.ResultMethod.PLAYED:
                if self.home_score == self.away_score:
                    errors["winner"] = "A played, finished match cannot have a tied score."
                elif self.completion_reason == self.CompletionReason.TIME_LIMIT:
                    if not self.has_match_timer:
                        errors["completion_reason"] = (
                            "A time-limit completion requires an applied match timer."
                        )
                    elif self.winner_id != (
                        self.home_entry_id
                        if self.home_score > self.away_score
                        else self.away_entry_id
                    ):
                        errors["winner"] = (
                            "The winner must be the player or team leading when the "
                            "time limit completed the match."
                        )
                elif self.home_score == self.race_target:
                    if self.winner_id != self.home_entry_id:
                        errors["winner"] = (
                            "The winner must be the competitor who reached the race target."
                        )
                elif self.away_score == self.race_target:
                    if self.winner_id != self.away_entry_id:
                        errors["winner"] = (
                            "The winner must be the competitor who reached the race target."
                        )
                else:
                    errors["home_score"] = (
                        "A played, finished match must have one competitor at the "
                        "race target unless it was completed by the time limit."
                    )

                if (
                    self.race_target is not None
                    and (
                        self.home_score > self.race_target
                        or self.away_score > self.race_target
                    )
                ):
                    errors["home_score"] = (
                        "A played match score cannot exceed its race target."
                    )
            elif self.completion_reason:
                errors["completion_reason"] = (
                    "Only a played match can have a race-target or time-limit "
                    "completion reason."
                )
        else:
            if self.winner_id:
                errors["winner"] = (
                    "Only a finished match may have a declared winner."
                )
            if self.winner_entry_life_id:
                errors["winner_entry_life"] = (
                    "Only a finished match may have a winning tournament life."
                )
            if self.completion_reason:
                errors["completion_reason"] = (
                    "A completion reason is available only for a finished match."
                )
            if self.is_in_correction_mode:
                errors["is_in_correction_mode"] = (
                    "Correction mode is only available for completed results."
                )
            if self.result_method != self.ResultMethod.PLAYED:
                errors["result_method"] = (
                    "A non-played result must be recorded as a finished match."
                )
            if self.status in {self.Status.UPCOMING, self.Status.CANCELLED}:
                if self.home_score or self.away_score:
                    errors["home_score"] = (
                        "An upcoming or cancelled match cannot retain a score."
                    )
            if (
                self.status in {
                    self.Status.LIVE,
                    self.Status.POSTPONED,
                    self.Status.ABANDONED,
                }
                and self.race_target is not None
                and (
                    self.home_score >= self.race_target
                    or self.away_score >= self.race_target
                )
            ):
                errors["home_score"] = (
                    "A competitor who reaches the race target must finish the match."
                )

        if self.status != self.Status.FINISHED and self.completed_at:
            errors["completed_at"] = (
                "Only a finished match may have a completion time."
            )

        if errors:
            raise ValidationError(errors)

    def save(
        self,
        *args,
        _locked_competitions=None,
        _skip_margin_policy_validation=False,
        _skip_database_constraint_validation=False,
        **kwargs,
    ):
        # Fixture-generator confirmation owns one outer transaction. Its
        # private batch path has already validated the schedule, so avoid an
        # otherwise redundant savepoint for every generated INSERT. Ordinary
        # callers retain the original nested-savepoint behaviour.
        with transaction.atomic(savepoint=not _skip_database_constraint_validation):
            # Keep the legacy text column useful to older templates, exports,
            # and integrations while the foreign key remains the authoritative
            # manual-round identity.  Bracket-generated rows have no configured
            # round relation and retain their existing tie-derived label.
            if self.knockout_round_id:
                self.round_label = self.knockout_round.name
            original_competition_id = (
                type(self).objects.filter(pk=self.pk)
                .values_list("competition_id", flat=True)
                .first()
                if self.pk
                else None
            )
            competitions = (
                _locked_competitions
                if _locked_competitions is not None
                else self._lock_margin_policy_competitions(
                    (self.competition_id, original_competition_id)
                )
            )
            if self.race_target is None and self.competition_id:
                competition = competitions.get(self.competition_id)
                if competition is None:
                    competition = PoolCompetition.objects.get(pk=self.competition_id)
                self.race_target = competition.default_race_target
            if self.status == self.Status.FINISHED and self.completed_at is None:
                self.completed_at = timezone.now()
            previous_skip = getattr(self, "_skip_margin_policy_validation", None)
            self._skip_margin_policy_validation = _skip_margin_policy_validation
            try:
                self.full_clean(
                    validate_constraints=not _skip_database_constraint_validation,
                )
            finally:
                if previous_skip is None:
                    delattr(self, "_skip_margin_policy_validation")
                else:
                    self._skip_margin_policy_validation = previous_skip
            return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        """Reject direct fixture deletion that would invalidate table bands.

        Django admin's individual-object deletion calls this method rather than
        ``QuerySet.delete()``.  Keep it on the same prospective validation path
        as bulk deletion so a finite margin policy cannot silently become
        invalid when its only high-target table fixture is removed.
        """

        with transaction.atomic():
            if self.pk:
                if self.knockout_round_id and PoolKnockoutBracket.objects.filter(
                    handoff_from_round__stage_id=self.knockout_round.stage_id,
                    handoff_from_round__order__gte=self.knockout_round.order,
                ).exists():
                    raise ValidationError(
                        "Reset the untouched handoff bracket before deleting this manual knockout source fixture."
                    )
                type(self)._validate_margin_policy_after_table_matches_removed([self.pk])
            return super().delete(using=using, keep_parents=keep_parents)


class PoolManualResultRackSummary(models.Model):
    """Aggregate outcome totals for one side of an offline final result.

    This deliberately has no rack sequence or player reference.  It records
    only the optional classified wins supplied by a manager; normal wins are
    always derived from the authoritative parent-match score.
    """

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="manual_rack_summaries",
    )
    entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="manual_result_rack_summaries",
    )
    green_apples = models.PositiveSmallIntegerField(default=0)
    red_apples = models.PositiveSmallIntegerField(default=0)
    one_visits = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["match", "entry"],
                name="pool_unique_manual_result_summary_side",
            ),
        ]

    @property
    def special_wins(self):
        return self.green_apples + self.red_apples + self.one_visits

    @property
    def score(self):
        if self.entry_id == self.match.home_entry_id:
            return self.match.home_score
        if self.entry_id == self.match.away_entry_id:
            return self.match.away_score
        return 0

    @property
    def normal_wins(self):
        return self.score - self.special_wins

    def clean(self):
        errors = {}
        if self.match_id and self.entry_id and self.entry_id not in {
            self.match.home_entry_id,
            self.match.away_entry_id,
        }:
            errors["entry"] = "The summary entry must be a fixture participant."
        if (
            self.match_id
            and self.match.result_entry_method
            != PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
        ):
            errors["match"] = "Outcome summaries are available only for manual final results."
        if self.special_wins > self.score:
            errors["green_apples"] = (
                "Green apples, red apples, and one visits cannot exceed this side's final score."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolRack(models.Model):
    """An authoritative rack result from which special outcomes are derived.

    ``winner`` remains the competition entry that receives the rack in the
    fixture score.  ``winning_player`` records the individual responsible for
    that rack when a team is competing.  It is deliberately nullable so the
    migration neither invents attribution for historical results nor prevents
    those results from remaining readable.  New team racks are validated by
    the result service and require an active, competition-entry-scoped roster
    membership.
    """

    class OutcomeType(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        GREEN_APPLE = "GREEN_APPLE", "Green apple"
        RED_APPLE = "RED_APPLE", "Red apple"
        ONE_VISIT = "ONE_VISIT", "One visit"
        LINEUP_VACANCY = "LINEUP_VACANCY", "Lineup vacancy"

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="racks",
    )
    rack_number = models.PositiveSmallIntegerField()
    winner = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="won_pool_racks",
    )
    winning_player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="won_pool_racks",
        null=True,
        blank=True,
        help_text=(
            "The player who won this rack. Required for newly recorded team "
            "racks; nullable for unattributed historical results."
        ),
    )
    outcome_type = models.CharField(
        max_length=20,
        choices=OutcomeType.choices,
        default=OutcomeType.NORMAL,
    )
    breaker = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="break_off_pool_racks",
        null=True,
        blank=True,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="recorded_pool_racks",
        null=True,
        blank=True,
    )
    request_token = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "Client action identifier used to make scorer retries idempotent."
        ),
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rack_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["match", "rack_number"],
                name="pool_unique_rack_number_per_match",
            ),
            models.UniqueConstraint(
                fields=["match", "request_token"],
                condition=Q(request_token__isnull=False),
                name="pool_unique_rack_request_token",
            ),
            models.CheckConstraint(
                condition=Q(rack_number__gte=1),
                name="pool_rack_number_positive",
            ),
        ]

    def __str__(self):
        return f"{self.match} - Rack {self.rack_number}"

    def clean(self):
        errors = {}
        participant_ids = set()
        pool_match = None
        if self.match_id:
            pool_match = self.match
            participant_ids = {
                pool_match.home_entry_id,
                pool_match.away_entry_id,
            }
        if self.winner_id and self.winner_id not in participant_ids:
            errors["winner"] = "The rack winner must be playing in this match."
        if self.breaker_id and self.breaker_id not in participant_ids:
            errors["breaker"] = "The breaker must be playing in this match."
        if self.rack_number is not None and self.rack_number < 1:
            errors["rack_number"] = "Rack number must be at least 1."

        if pool_match:
            if (
                pool_match.competition.participant_mode
                == PoolCompetition.ParticipantMode.SINGLES
                and self.winning_player_id
                and self.winner_id
                and self.winner.player_id != self.winning_player_id
            ):
                errors["winning_player"] = (
                    "The winning player must be the player represented by the "
                    "singles entry that won the rack."
                )

            # Do not retrospectively invalidate a historic rack merely
            # because its old roster membership was later removed.  A newly
            # written attribution (or a correction that changes attribution)
            # must, however, still belong to the winning team entry in this
            # competition.  The result service additionally requires that
            # membership to be active at the time of a new/corrected write.
            original_attribution = (
                type(self).objects.filter(pk=self.pk)
                .values("winner_id", "winning_player_id")
                .first()
                if self.pk
                else None
            )
            attribution_changed = not original_attribution or any(
                original_attribution[field] != getattr(self, field)
                for field in ("winner_id", "winning_player_id")
            )
            if (
                pool_match.competition.participant_mode
                == PoolCompetition.ParticipantMode.TEAMS
            ):
                is_lineup_vacancy = (
                    self.outcome_type == self.OutcomeType.LINEUP_VACANCY
                )
                if is_lineup_vacancy and not getattr(
                    self,
                    "_from_team_vacancy_service",
                    False,
                ):
                    errors["outcome_type"] = (
                        "Lineup-vacancy outcomes are recorded only by the "
                        "team score-sheet service."
                    )
                elif is_lineup_vacancy and self.winning_player_id:
                    errors["winning_player"] = (
                        "A lineup-vacancy point is not attributed to an individual player."
                    )
                elif (
                    self._state.adding
                    and not self.winning_player_id
                    and not is_lineup_vacancy
                ):
                    errors["winning_player"] = (
                        "Select the registered player who won this team rack."
                    )
                elif (
                    self.winning_player_id
                    and self.winner_id
                    and attribution_changed
                    and not PoolTeamRoster.objects.filter(
                        competition_entry_id=self.winner_id,
                        player_id=self.winning_player_id,
                        is_active=True,
                    ).exists()
                    and not PoolTeamMatchScheduledRack.objects.filter(
                        match_id=pool_match.pk,
                    ).filter(
                        Q(
                            home_player_id=self.winning_player_id,
                            match__home_entry_id=self.winner_id,
                        )
                        | Q(
                            away_player_id=self.winning_player_id,
                            match__away_entry_id=self.winner_id,
                        )
                    ).exists()
                ):
                    errors["winning_player"] = (
                        "The winning player must belong to the winning team's "
                        "competition roster or be an already snapshotted team-match participant."
                    )

            if pool_match.result_method != PoolMatch.ResultMethod.PLAYED:
                errors["match"] = (
                    "Rack records are only available for a played match result."
                )
            elif pool_match.status == PoolMatch.Status.FINISHED:
                if not pool_match.is_in_correction_mode:
                    errors["match"] = (
                        "Open result correction before changing a completed match."
                    )
            elif pool_match.status != PoolMatch.Status.LIVE:
                errors["match"] = "Racks can only be recorded while a match is live."

            if self._state.adding and self.rack_number is not None:
                previous_number = (
                    pool_match.racks.order_by("-rack_number")
                    .values_list("rack_number", flat=True)
                    .first()
                )
                expected_number = (previous_number or 0) + 1
                if self.rack_number != expected_number:
                    errors["rack_number"] = (
                        "Rack numbers must be sequential for this match."
                    )
        if errors:
            raise ValidationError(errors)

    def save(
        self,
        *args,
        _from_result_service=False,
        _from_team_vacancy_service=False,
        **kwargs,
    ):
        """Persist rack history only from the synchronising result service.

        Scores, winners, and completion state are derived immediately after a
        rack mutation. Allowing ordinary model saves here would make it too
        easy for an importer, admin customisation, or future view to leave
        those values out of sync. The service passes the private flag after it
        has locked and validated the parent match.
        """
        if not _from_result_service:
            raise ValidationError(
                "Use the pool result service to create or change rack history."
            )
        if (
            _from_team_vacancy_service
            and self.outcome_type != self.OutcomeType.LINEUP_VACANCY
        ):
            raise ValidationError(
                "The team-vacancy service may create only lineup-vacancy rack results."
            )
        self._from_team_vacancy_service = _from_team_vacancy_service
        try:
            self.full_clean()
            return super().save(*args, **kwargs)
        finally:
            self.__dict__.pop("_from_team_vacancy_service", None)

    def delete(self, using=None, keep_parents=False, *, _from_result_service=False):
        """Keep rack deletion on the same synchronising service boundary."""
        if not _from_result_service:
            raise ValidationError(
                "Use the pool result service to remove rack history."
            )
        return super().delete(using=using, keep_parents=keep_parents)


class PoolCompetitionTeamStaffAssignment(models.Model):
    """An authenticated coach or captain scoped to one team entry only.

    This relationship deliberately grants no general competition-management or
    result-scoring capability.  The permission layer may use it exclusively
    for lineup submission and correction for this exact competition entry.
    """

    class Role(models.TextChoices):
        COACH = "COACH", "Coach"
        CAPTAIN = "CAPTAIN", "Captain"

    competition_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.CASCADE,
        related_name="team_staff_assignments",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pool_team_staff_assignments",
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [
            "competition_entry__competition__name",
            "competition_entry__team__name",
            "user__username",
            "id",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["competition_entry", "user"],
                name="pool_unique_team_staff_entry_user",
            ),
        ]

    def __str__(self):
        return (
            f"{self.user} - {self.competition_entry.display_name} - "
            f"{self.get_role_display()}"
        )

    def clean(self):
        if not self.competition_entry_id:
            return
        entry = self.competition_entry
        if not entry.team_id or (
            entry.competition.participant_mode
            != PoolCompetition.ParticipantMode.TEAMS
        ):
            raise ValidationError(
                {
                    "competition_entry": (
                        "Coach and captain assignments are only available for "
                        "team competition entries."
                    )
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

class PoolMatchScorerAssignment(models.Model):
    """A scoring operator assigned to one specific pool fixture only.

    This role grants no competition-management, standings, bracket, roster,
    or lineup-management permissions. It exists only to operate live scoring
    for this exact match; team score-sheet capabilities remain delegated to
    the existing team-operation services.
    """

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="scorer_assignments",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pool_match_scorer_assignments",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="assigned_pool_match_scorers",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match", "-is_active", "user__username", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["match", "user"],
                name="pool_tm_scorer_user_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["match", "is_active"],
                name="pool_tm_scorer_active_idx",
            ),
        ]

    def __str__(self):
        return f"{self.user} - scorer - {self.match}"

    def clean(self):
        errors = {}

        if self.is_active and self.user_id and not self.user.is_active:
            errors["user"] = "The assigned scorer must have an active account."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


# Import compatibility for integrations and old manager URLs.  This is an
# alias, not a proxy model: the migration below renames the real model/table
# and preserves the existing scorer-assignment rows.
PoolTeamMatchScorerAssignment = PoolMatchScorerAssignment

    
class PoolTeamMatchLineup(models.Model):
    """The submitted squad and five current positions for one team fixture."""

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="team_lineups",
    )
    team_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.CASCADE,
        related_name="team_match_lineups",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="submitted_pool_team_lineups",
        null=True,
        blank=True,
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match", "team_entry", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["match", "team_entry"],
                name="pool_unique_team_lineup_per_match_entry",
            ),
        ]

    def __str__(self):
        return f"{self.match}: {self.team_entry.display_name} lineup"

    @property
    def is_complete(self):
        """Whether the five structural score-sheet positions have been saved.

        A position may intentionally be vacant.  Keeping the five stable rows
        lets the schedule retain its 1--5 topology while the team operation
        service records an explicit vacancy point (or a void) only when that
        position is actually due.
        """

        positions = list(self.positions.values_list("position", "player_id"))
        return (
            len(positions) == 5
            and {position for position, _player_id in positions} == {1, 2, 3, 4, 5}
        )

    @property
    def filled_position_count(self):
        return self.positions.exclude(player__isnull=True).count()

    def clean(self):
        errors = {}
        if self.match_id:
            match = self.match
            if (
                match.competition.participant_mode
                != PoolCompetition.ParticipantMode.TEAMS
            ):
                errors["match"] = "Team lineups are only available for team matches."
            elif self.team_entry_id and self.team_entry_id not in {
                match.home_entry_id,
                match.away_entry_id,
            }:
                errors["team_entry"] = (
                    "A lineup must belong to one of this fixture's team entries."
                )

        if self.team_entry_id:
            entry = self.team_entry
            if not entry.team_id:
                errors["team_entry"] = "A team lineup must reference a team entry."
            elif self.match_id and entry.competition_id != self.match.competition_id:
                errors["team_entry"] = (
                    "The lineup team entry must belong to the match competition."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolTeamMatchLineupMember(models.Model):
    """One submitted player in a lineup squad, including substitutes."""

    lineup = models.ForeignKey(
        PoolTeamMatchLineup,
        on_delete=models.CASCADE,
        related_name="members",
    )
    player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="team_match_lineup_memberships",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["lineup", "player__full_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["lineup", "player"],
                name="pool_unique_team_lineup_member",
            ),
        ]

    def __str__(self):
        return f"{self.player.full_name} - {self.lineup}"

    def clean(self):
        if not self.lineup_id or not self.player_id:
            return
        if not PoolTeamRoster.objects.filter(
            competition_entry_id=self.lineup.team_entry_id,
            player_id=self.player_id,
            is_active=True,
        ).exists():
            raise ValidationError(
                {
                    "player": (
                        "A submitted lineup player must be active in this team's "
                        "competition roster."
                    )
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolTeamMatchLineupPosition(models.Model):
    """The current player assigned to one of the five team-match positions."""

    lineup = models.ForeignKey(
        PoolTeamMatchLineup,
        on_delete=models.CASCADE,
        related_name="positions",
    )
    position = models.PositiveSmallIntegerField()
    player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="team_match_lineup_positions",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["lineup", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["lineup", "position"],
                name="pool_unique_team_lineup_position",
            ),
            models.UniqueConstraint(
                fields=["lineup", "player"],
                name="pool_unique_team_lineup_position_player",
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1) & Q(position__lte=5),
                name="pool_team_lineup_position_between_one_and_five",
            ),
        ]

    def __str__(self):
        return f"{self.lineup}: position {self.position}"

    def clean(self):
        errors = {}
        if self.position is None or not 1 <= self.position <= 5:
            errors["position"] = "Lineup positions must be between 1 and 5."
        if self.lineup_id and self.player_id and not PoolTeamMatchLineupMember.objects.filter(
            lineup_id=self.lineup_id,
            player_id=self.player_id,
        ).exists():
            errors["player"] = (
                "A current position must be occupied by a player in the submitted squad."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolTeamMatchScheduledRack(models.Model):
    """A persistent, position-based team-rack schedule row.

    ``sequence_number`` and ``session_number`` are schedule topology, while
    the linked :class:`PoolRack` retains chronological completion numbering.
    Player snapshots are intentionally taken only when a scheduled rack starts
    so later substitutions cannot rewrite live or completed pairings.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        LIVE = "LIVE", "Live"
        COMPLETED = "COMPLETED", "Completed"
        VOID = "VOID", "Void — both positions vacant"
        STOPPED = "STOPPED", "Stopped at parent completion"
        NOT_REQUIRED = "NOT_REQUIRED", "Not required"

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="team_scheduled_racks",
    )
    sequence_number = models.PositiveSmallIntegerField()
    session_number = models.PositiveSmallIntegerField()
    home_position = models.PositiveSmallIntegerField()
    away_position = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    table_number = models.PositiveSmallIntegerField(null=True, blank=True)
    home_player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="scheduled_home_pool_team_racks",
        null=True,
        blank=True,
    )
    away_player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="scheduled_away_pool_team_racks",
        null=True,
        blank=True,
    )
    result = models.OneToOneField(
        PoolRack,
        on_delete=models.SET_NULL,
        related_name="team_scheduled_rack",
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match", "sequence_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["match", "sequence_number"],
                name="pool_unique_team_scheduled_rack_sequence",
            ),
            models.UniqueConstraint(
                fields=["match", "table_number"],
                condition=Q(status="LIVE", table_number__isnull=False),
                name="pool_unique_live_team_rack_per_table",
            ),
            models.CheckConstraint(
                condition=Q(sequence_number__gte=1) & Q(sequence_number__lte=25),
                name="pool_team_scheduled_rack_sequence_range",
            ),
            models.CheckConstraint(
                condition=Q(session_number__gte=1) & Q(session_number__lte=5),
                name="pool_team_scheduled_rack_session_range",
            ),
            models.CheckConstraint(
                condition=Q(home_position__gte=1) & Q(home_position__lte=5),
                name="pool_team_scheduled_rack_home_position_range",
            ),
            models.CheckConstraint(
                condition=Q(away_position__gte=1) & Q(away_position__lte=5),
                name="pool_team_scheduled_rack_away_position_range",
            ),
            models.CheckConstraint(
                condition=Q(table_number__isnull=True) | Q(table_number__gte=1),
                name="pool_team_scheduled_rack_table_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["match", "status", "sequence_number"],
                name="pool_team_rack_match_stat_ix",
            ),
        ]

    def __str__(self):
        return f"{self.match} - scheduled rack {self.sequence_number}"

    @property
    def is_started(self):
        return self.status in {
            self.Status.LIVE,
            self.Status.COMPLETED,
            self.Status.VOID,
            self.Status.STOPPED,
        }

    def clean(self):
        errors = {}
        if self.sequence_number is None or not 1 <= self.sequence_number <= 25:
            errors["sequence_number"] = "Scheduled rack sequence must be between 1 and 25."
        if self.session_number is None or not 1 <= self.session_number <= 5:
            errors["session_number"] = "Session number must be between 1 and 5."
        for field_name in ("home_position", "away_position"):
            value = getattr(self, field_name)
            if value is None or not 1 <= value <= 5:
                errors[field_name] = "Scheduled rack positions must be between 1 and 5."

        match = self.match if self.match_id else None
        if match:
            if match.competition.participant_mode != PoolCompetition.ParticipantMode.TEAMS:
                errors["match"] = "A team rack schedule is only available for team matches."
            elif not match.home_entry.team_id or not match.away_entry.team_id:
                errors["match"] = "A team rack schedule requires two team entries."

            if self.table_number and self.table_number > match.table_count:
                errors["table_number"] = (
                    "The scheduled table must be within this match's table count."
                )

        if self.table_number is not None and self.table_number < 1:
            errors["table_number"] = "Table number must be at least 1."
        if self.home_player_id and self.away_player_id == self.home_player_id:
            errors["away_player"] = "The same player cannot occupy both sides of a rack."

        is_vacancy_result = bool(
            self.result_id
            and self.result.outcome_type == PoolRack.OutcomeType.LINEUP_VACANCY
        )
        requires_snapshots = self.status in {
            self.Status.LIVE,
            self.Status.STOPPED,
        } or (
            self.status == self.Status.COMPLETED and not is_vacancy_result
        )
        has_both_snapshots = bool(self.home_player_id and self.away_player_id)
        has_any_snapshot = bool(self.home_player_id or self.away_player_id)
        if self.status == self.Status.VOID:
            if has_any_snapshot:
                errors["home_player"] = (
                    "A void scheduled rack cannot retain player snapshots."
                )
        elif is_vacancy_result:
            if self.status != self.Status.COMPLETED:
                errors["status"] = (
                    "A lineup-vacancy result must be recorded as completed."
                )
            elif has_both_snapshots or not has_any_snapshot:
                errors["home_player"] = (
                    "A lineup-vacancy result requires exactly one available player snapshot."
                )
        elif requires_snapshots and not has_both_snapshots:
            errors["home_player"] = (
                "Live and completed scheduled racks require both player snapshots."
            )
        elif not requires_snapshots and has_any_snapshot:
            errors["home_player"] = (
                "Only live or completed scheduled racks may retain player snapshots."
            )

        if self.status == self.Status.LIVE and not self.table_number:
            errors["table_number"] = (
                "A live scheduled rack requires a table number."
            )
        elif self.status in {
            self.Status.PENDING,
            self.Status.NOT_REQUIRED,
            self.Status.VOID,
        } and self.table_number:
            errors["table_number"] = (
                "Pending, void, and not-required scheduled racks cannot occupy a table."
            )
        if is_vacancy_result and self.table_number:
            errors["table_number"] = (
                "A lineup-vacancy result cannot occupy a physical table."
            )

        if self.status == self.Status.STOPPED:
            if not match or match.status not in {
                match.Status.FINISHED,
                match.Status.CANCELLED,
                match.Status.ABANDONED,
            }:
                errors["status"] = (
                    "A scheduled rack can be stopped only after its parent fixture "
                    "has reached a terminal state."
                )
            if self.result_id:
                errors["result"] = (
                    "A stopped scheduled rack cannot retain a fabricated result."
                )

        if self.status == self.Status.COMPLETED and not self.result_id:
            errors["result"] = "A completed scheduled rack requires its rack result."
        elif self.status == self.Status.VOID and self.result_id:
            errors["result"] = "A void scheduled rack cannot retain a rack result."
        elif self.status != self.Status.COMPLETED and self.result_id:
            errors["result"] = "Only a completed scheduled rack may have a rack result."

        if self.result_id and match:
            result = self.result
            if result.match_id != match.pk:
                errors["result"] = "A scheduled rack result must belong to this match."
            elif is_vacancy_result:
                if result.winning_player_id:
                    errors["result"] = (
                        "A lineup-vacancy point cannot be attributed to an individual player."
                    )
                elif result.winner_id == match.home_entry_id:
                    if not self.home_player_id or self.away_player_id:
                        errors["result"] = (
                            "A home vacancy point requires only the available home player snapshot."
                        )
                elif result.winner_id == match.away_entry_id:
                    if not self.away_player_id or self.home_player_id:
                        errors["result"] = (
                            "An away vacancy point requires only the available away player snapshot."
                        )
                else:
                    errors["result"] = (
                        "A lineup-vacancy result winner must play in this match."
                    )
            elif result.winner_id == match.home_entry_id:
                if result.winning_player_id != self.home_player_id:
                    errors["result"] = (
                        "A home rack result must be attributed to the home player snapshot."
                    )
            elif result.winner_id == match.away_entry_id:
                if result.winning_player_id != self.away_player_id:
                    errors["result"] = (
                        "An away rack result must be attributed to the away player snapshot."
                    )
            else:
                errors["result"] = "A scheduled rack result winner must play in this match."

        # Historical schedule rows remain readable if a roster member becomes
        # inactive later.  Only a newly captured or changed snapshot must be
        # active in the corresponding competition-entry roster.
        if match and has_any_snapshot:
            previous = (
                type(self).objects.filter(pk=self.pk)
                .values("home_player_id", "away_player_id")
                .first()
                if self.pk
                else None
            )
            snapshot_changed = not previous or any(
                previous[field_name] != getattr(self, field_name)
                for field_name in ("home_player_id", "away_player_id")
            )
            if snapshot_changed:
                for field_name, entry_id in (
                    ("home_player", match.home_entry_id),
                    ("away_player", match.away_entry_id),
                ):
                    player_id = getattr(self, f"{field_name}_id")
                    if player_id is None:
                        continue
                    if not PoolTeamRoster.objects.filter(
                        competition_entry_id=entry_id,
                        player_id=player_id,
                        is_active=True,
                    ).exists():
                        errors[field_name] = (
                            "The snapshot player must be active in the matching "
                            "team competition roster."
                        )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolTeamMatchSubstitution(models.Model):
    """An immutable record of a future-position team-match substitution."""

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="team_substitutions",
    )
    team_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.CASCADE,
        related_name="team_match_substitutions",
    )
    position = models.PositiveSmallIntegerField()
    outgoing_player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="outgoing_team_match_substitutions",
    )
    incoming_player = models.ForeignKey(
        PoolPlayer,
        on_delete=models.PROTECT,
        related_name="incoming_team_match_substitutions",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="performed_pool_team_substitutions",
        null=True,
        blank=True,
    )
    reason = models.TextField(blank=True)
    performed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["match", "performed_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(position__gte=1) & Q(position__lte=5),
                name="pool_team_substitution_position_range",
            ),
            models.CheckConstraint(
                condition=~Q(outgoing_player=models.F("incoming_player")),
                name="pool_team_substitution_distinct_players",
            ),
        ]
        indexes = [
            models.Index(
                fields=["match", "team_entry", "performed_at"],
                name="pool_team_sub_match_entry_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.match}: {self.outgoing_player.full_name} → "
            f"{self.incoming_player.full_name}"
        )

    def clean(self):
        errors = {}
        if self.position is None or not 1 <= self.position <= 5:
            errors["position"] = "Substitution positions must be between 1 and 5."
        if (
            self.outgoing_player_id
            and self.incoming_player_id
            and self.outgoing_player_id == self.incoming_player_id
        ):
            errors["incoming_player"] = "A substitute must replace a different player."

        if self.match_id:
            match = self.match
            if (
                match.competition.participant_mode
                != PoolCompetition.ParticipantMode.TEAMS
            ):
                errors["match"] = "Substitutions are only available for team matches."
            elif self.team_entry_id and self.team_entry_id not in {
                match.home_entry_id,
                match.away_entry_id,
            }:
                errors["team_entry"] = (
                    "A substitution must belong to one of this fixture's team entries."
                )

        if self.team_entry_id and not self.team_entry.team_id:
            errors["team_entry"] = "A substitution must reference a team entry."

        if self.match_id and self.team_entry_id:
            lineup = PoolTeamMatchLineup.objects.filter(
                match_id=self.match_id,
                team_entry_id=self.team_entry_id,
            ).first()
            if not lineup:
                errors["team_entry"] = "The team must have a submitted match lineup."
            else:
                member_ids = set(lineup.members.values_list("player_id", flat=True))
                for field_name in ("outgoing_player", "incoming_player"):
                    player_id = getattr(self, f"{field_name}_id")
                    if player_id and player_id not in member_ids:
                        errors[field_name] = (
                            "Substitution players must belong to the submitted squad."
                        )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolTeamMatchAuditEvent(models.Model):
    """Immutable audit trail for lineup, substitution, and schedule operations."""

    class Action(models.TextChoices):
        LINEUP_SAVED = "LINEUP_SAVED", "Lineup saved"
        LINEUP_CORRECTED = "LINEUP_CORRECTED", "Lineup corrected"
        SCHEDULE_GENERATED = "SCHEDULE_GENERATED", "Rack schedule generated"
        RACK_STARTED = "RACK_STARTED", "Scheduled rack started"
        RACK_COMPLETED = "RACK_COMPLETED", "Scheduled rack completed"
        SUBSTITUTION = "SUBSTITUTION", "Substitution made"

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="team_match_audit_events",
    )
    team_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.CASCADE,
        related_name="team_match_audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pool_team_match_audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=24, choices=Action.choices)
    message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["match", "-created_at"],
                name="pool_team_audit_match_date_idx",
            ),
            models.Index(
                fields=["team_entry", "-created_at"],
                name="pool_team_audit_entry_date_idx",
            ),
        ]

    def __str__(self):
        return f"{self.match}: {self.get_action_display()}"

    def clean(self):
        if not self.match_id:
            return
        match = self.match
        if (
            match.competition.participant_mode
            != PoolCompetition.ParticipantMode.TEAMS
        ):
            raise ValidationError(
                {"match": "A team-match audit entry requires a team fixture."}
            )
        if not self.team_entry_id:
            return
        if self.team_entry_id not in {match.home_entry_id, match.away_entry_id}:
            raise ValidationError(
                {
                    "team_entry": (
                        "A team-match audit entry must reference a fixture participant."
                    )
                }
            )
        if not self.team_entry.team_id:
            raise ValidationError(
                {"team_entry": "A team-match audit entry must reference a team entry."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolMatchResultRevision(models.Model):
    """An immutable audit record for a pool match result lifecycle action."""

    class Action(models.TextChoices):
        RESULT_CORRECTED = "RESULT_CORRECTED", "Result corrected"
        MATCH_STARTED = "MATCH_STARTED", "Match started"
        RACK_RECORDED = "RACK_RECORDED", "Rack recorded"
        RACK_CORRECTED = "RACK_CORRECTED", "Rack corrected"
        RACK_UNDONE = "RACK_UNDONE", "Rack undone"
        CORRECTION_OPENED = "CORRECTION_OPENED", "Correction opened"
        MATCH_COMPLETED = "MATCH_COMPLETED", "Played match completed"
        MANUAL_RESULT_ENTERED = "MANUAL_RESULT_ENTERED", "Manual final result entered"
        MANUAL_RESULT_CORRECTED = "MANUAL_RESULT_CORRECTED", "Manual final result corrected"
        NON_PLAYED_RESULT = "NON_PLAYED_RESULT", "Non-played result recorded"
        MATCH_POSTPONED = "MATCH_POSTPONED", "Match postponed"
        MATCH_CANCELLED = "MATCH_CANCELLED", "Match cancelled"
        MATCH_ABANDONED = "MATCH_ABANDONED", "Match abandoned"

    match = models.ForeignKey(
        PoolMatch,
        on_delete=models.CASCADE,
        related_name="result_revisions",
    )
    revised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pool_match_result_revisions",
        null=True,
        blank=True,
    )
    action = models.CharField(
        max_length=24,
        choices=Action.choices,
        default=Action.RESULT_CORRECTED,
    )
    reason = models.TextField(blank=True)
    previous_result = models.JSONField(default=dict, blank=True)
    revised_result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["match", "-created_at"], name="pool_revision_match_date_idx"),
        ]

    def __str__(self):
        return f"Result revision for {self.match} at {self.created_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolGroupQualificationRule(models.Model):
    """Map one calculated group finishing position to a bracket entry slot.

    ``destination_slot`` is the stable, one-based slot number in the first
    knockout round.  It allows managers to configure Group A #1 v Group B #2
    before a bracket has been generated, while the bracket service converts it
    into an explicit persistent slot at confirmation time.
    """

    source_stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="qualification_rules",
    )
    group = models.ForeignKey(
        PoolGroup,
        on_delete=models.CASCADE,
        related_name="qualification_rules",
    )
    knockout_stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="incoming_qualification_rules",
    )
    position = models.PositiveSmallIntegerField()
    destination_slot = models.PositiveSmallIntegerField()
    keep_groups_apart = models.BooleanField(
        default=False,
        help_text="Prefer a first-round layout that separates the same group where possible.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_stage__order", "group__order", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_stage", "group", "position"],
                name="pool_unique_group_qualification_source",
            ),
            models.UniqueConstraint(
                fields=["knockout_stage", "destination_slot"],
                name="pool_unique_group_qualification_destination",
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1),
                name="pool_group_qualification_position_positive",
            ),
            models.CheckConstraint(
                condition=Q(destination_slot__gte=1),
                name="pool_group_qualification_slot_positive",
            ),
        ]

    def __str__(self):
        return (
            f"{self.group.name} #{self.position} → "
            f"slot {self.destination_slot}"
        )

    @property
    def competition(self):
        return self.source_stage.competition

    def clean(self):
        errors = {}
        if self.source_stage_id:
            if self.source_stage.stage_type != PoolCompetitionStage.StageType.GROUP:
                errors["source_stage"] = "Qualification sources must be a group stage."
            if self.group_id and self.group.stage_id != self.source_stage_id:
                errors["group"] = "The group must belong to the source stage."
        if self.knockout_stage_id:
            if self.knockout_stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
                errors["knockout_stage"] = "The destination must be a knockout stage."
            if (
                self.source_stage_id
                and self.knockout_stage.competition_id
                != self.source_stage.competition_id
            ):
                errors["knockout_stage"] = (
                    "Qualification destination must stay in the same competition."
                )
            elif (
                self.source_stage_id
                and self.knockout_stage.order <= self.source_stage.order
            ):
                errors["knockout_stage"] = (
                    "Qualification destination must follow its source stage."
                )
        if self.position is not None and self.position < 1:
            errors["position"] = "Qualification position must be at least 1."
        if self.destination_slot is not None and self.destination_slot < 1:
            errors["destination_slot"] = "Destination slot must be at least 1."
        if self.group_id and self.position and self.position > self.group.qualifiers:
            errors["position"] = (
                "This finishing position exceeds the group's configured qualifiers."
            )
        if self.pk and self.knockout_stage_id:
            original = type(self).objects.filter(pk=self.pk).values(
                "source_stage_id", "group_id", "knockout_stage_id", "position", "destination_slot", "keep_groups_apart"
            ).first()
            changed = bool(original and any(
                original[field] != getattr(self, field)
                for field in (
                    "source_stage_id",
                    "group_id",
                    "knockout_stage_id",
                    "position",
                    "destination_slot",
                    "keep_groups_apart",
                )
            ))
            if changed and PoolKnockoutBracket.objects.filter(
                Q(stage_id=original["knockout_stage_id"])
                | Q(stage_id=self.knockout_stage_id)
            ).exists():
                errors["knockout_stage"] = (
                    "Reset the generated bracket before changing a qualification mapping."
                )
        if (not self.pk) and self.knockout_stage_id and PoolKnockoutBracket.objects.filter(
            stage_id=self.knockout_stage_id
        ).exists():
            errors["knockout_stage"] = (
                "Reset the generated bracket before adding a qualification mapping."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        if (
            self.pk
            and self.knockout_stage_id
            and PoolKnockoutBracket.objects.filter(
                stage_id=self.knockout_stage_id
            ).exists()
        ):
            raise ValidationError(
                "Reset the generated bracket before deleting a qualification mapping."
            )
        return super().delete(using=using, keep_parents=keep_parents)


class PoolBracketDrawPreviewQuerySet(models.QuerySet):
    """Keep a confirmed draw's audit source from being deleted out of band."""

    def delete(self):
        with transaction.atomic(using=self.db):
            # Confirmation locks the preview before it creates/locks a
            # bracket.  Take the same first lock here, then recheck the
            # relation so a direct admin/ORM delete cannot race confirmation
            # and null out the generated bracket's audit source.
            preview_ids = list(
                self.select_for_update(of=("self",))
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            if preview_ids and PoolKnockoutBracket.objects.using(self.db).filter(
                confirmed_preview_id__in=preview_ids
            ).exists():
                raise ValidationError(
                    "A confirmed draw preview can be removed only by resetting its "
                    "untouched bracket."
                )
            return super().delete()


class PoolBracketDrawPreviewManager(
    models.Manager.from_queryset(PoolBracketDrawPreviewQuerySet)
):
    pass


class PoolBracketDrawPreview(models.Model):
    """A saved, user-scoped draw preview that confirmation must reproduce."""

    class DrawMethod(models.TextChoices):
        MANUAL = "MANUAL", "Manual slot assignment"
        RANDOM = "RANDOM", "Random draw"
        SEEDED = "SEEDED", "Seeded draw"
        GROUP_QUALIFICATION = "GROUP_QUALIFICATION", "Group qualification"

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="bracket_draw_previews",
    )
    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="bracket_draw_previews",
    )
    handoff_from_round = models.ForeignKey(
        PoolKnockoutRoundDefinition,
        on_delete=models.RESTRICT,
        related_name="handoff_draw_previews",
        null=True,
        blank=True,
        help_text=(
            "Optional provenance for a later manual-round to bracket handoff."
        ),
    )
    draw_method = models.CharField(max_length=24, choices=DrawMethod.choices)
    bracket_size = models.PositiveSmallIntegerField()
    third_place_enabled = models.BooleanField(default=False)
    draw_order = models.JSONField(default=list, blank=True)
    life_draw_order = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Life-specific draw slots for a two-life knockout. Legacy entry "
            "draw_order values remain intact for ordinary brackets."
        ),
    )
    round_race_targets = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pool_bracket_draw_previews",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PoolBracketDrawPreviewManager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["competition", "stage", "-created_at"],
                name="pool_draw_prev_stage_idx",
            ),
        ]

    def __str__(self):
        return f"{self.competition.name} {self.get_draw_method_display()} preview"

    def clean(self):
        errors = {}
        if self.stage_id:
            if self.stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
                errors["stage"] = "A bracket preview requires a knockout stage."
            if self.competition_id and self.stage.competition_id != self.competition_id:
                errors["stage"] = "The stage must belong to this competition."
        if self.handoff_from_round_id:
            if self.handoff_from_round.stage_id != self.stage_id:
                errors["handoff_from_round"] = (
                    "A handoff source round must belong to this knockout stage."
                )
            elif (
                self.competition_id
                and self.handoff_from_round.stage.competition_id
                != self.competition_id
            ):
                errors["handoff_from_round"] = (
                    "A handoff source round must belong to this competition."
                )
        if self.bracket_size not in {2, 4, 8, 16, 32, 64, 128, 256}:
            errors["bracket_size"] = (
                "Bracket size must be 2, 4, 8, 16, 32, 64, 128, or 256."
            )
        if self.third_place_enabled and self.bracket_size < 4:
            errors["third_place_enabled"] = (
                "A third-place match requires a bracket with at least four entrants."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        database = using or self._state.db
        with transaction.atomic(using=database):
            if self.pk:
                # Lock a fresh row rather than trusting an in-memory preview
                # object whose confirmed relation may have changed since it
                # was fetched.
                locked_preview = (
                    type(self)._base_manager.using(database)
                    .select_for_update()
                    .filter(pk=self.pk)
                    .first()
                )
                if locked_preview is None:
                    return (0, {})
                if PoolKnockoutBracket.objects.using(database).filter(
                    confirmed_preview_id=locked_preview.pk
                ).exists():
                    raise ValidationError(
                        "A confirmed draw preview can be removed only by resetting its "
                        "untouched bracket."
                    )
            return super().delete(using=using, keep_parents=keep_parents)


class PoolKnockoutBracket(models.Model):
    """Persisted, immutable-layout topology for one pool knockout stage."""

    competition = models.ForeignKey(
        PoolCompetition,
        on_delete=models.CASCADE,
        related_name="knockout_brackets",
    )
    stage = models.OneToOneField(
        PoolCompetitionStage,
        on_delete=models.CASCADE,
        related_name="knockout_bracket",
    )
    handoff_from_round = models.ForeignKey(
        PoolKnockoutRoundDefinition,
        on_delete=models.RESTRICT,
        related_name="handoff_brackets",
        null=True,
        blank=True,
        help_text=(
            "Optional provenance for a confirmed manual-round to bracket handoff."
        ),
    )
    confirmed_preview = models.OneToOneField(
        PoolBracketDrawPreview,
        on_delete=models.SET_NULL,
        related_name="confirmed_pool_bracket",
        null=True,
        blank=True,
        help_text="The exact persisted preview used to generate this bracket.",
    )
    size = models.PositiveSmallIntegerField()
    draw_method = models.CharField(
        max_length=24,
        choices=PoolBracketDrawPreview.DrawMethod.choices,
    )
    draw_order = models.JSONField(default=list, blank=True)
    life_draw_order = models.JSONField(default=list, blank=True)
    third_place_enabled = models.BooleanField(default=False)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="generated_pool_brackets",
        null=True,
        blank=True,
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["competition__name", "stage__order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(size__in=[2, 4, 8, 16, 32, 64, 128, 256]),
                name="pool_bracket_supported_size",
            ),
        ]

    def __str__(self):
        return f"{self.competition.name}: {self.stage.name} bracket"

    def clean(self):
        errors = {}
        if self.stage_id:
            if self.stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
                errors["stage"] = "A bracket requires a knockout stage."
            if self.competition_id and self.stage.competition_id != self.competition_id:
                errors["stage"] = "The stage must belong to this competition."
        if self.size not in {2, 4, 8, 16, 32, 64, 128, 256}:
            errors["size"] = "Bracket size must be 2, 4, 8, 16, 32, 64, 128, or 256."
        if self.confirmed_preview_id:
            if self.confirmed_preview.competition_id != self.competition_id:
                errors["confirmed_preview"] = (
                    "The confirmed preview must belong to this competition."
                )
            elif self.confirmed_preview.stage_id != self.stage_id:
                errors["confirmed_preview"] = (
                    "The confirmed preview must belong to this knockout stage."
                )
        if self.handoff_from_round_id:
            if self.handoff_from_round.stage_id != self.stage_id:
                errors["handoff_from_round"] = (
                    "A handoff source round must belong to this knockout stage."
                )
            elif (
                self.competition_id
                and self.handoff_from_round.stage.competition_id
                != self.competition_id
            ):
                errors["handoff_from_round"] = (
                    "A handoff source round must belong to this competition."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolKnockoutRoundRule(models.Model):
    """The race target for a named bracket round, copied to new fixtures."""

    bracket = models.ForeignKey(
        PoolKnockoutBracket,
        on_delete=models.CASCADE,
        related_name="round_rules",
    )
    round_number = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=100)
    race_target = models.PositiveSmallIntegerField()
    is_third_place = models.BooleanField(default=False)

    class Meta:
        ordering = ["round_number", "is_third_place", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bracket", "round_number", "is_third_place"],
                name="pool_unique_bracket_round_rule",
            ),
            models.CheckConstraint(
                condition=Q(round_number__gte=1),
                name="pool_bracket_round_positive",
            ),
            models.CheckConstraint(
                condition=Q(race_target__gte=1),
                name="pool_bracket_round_race_positive",
            ),
        ]

    def __str__(self):
        return f"{self.bracket}: {self.name}"

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolKnockoutTie(models.Model):
    """A stable bracket node; its optional match is the existing pool fixture."""

    class ResolutionMethod(models.TextChoices):
        PENDING = "PENDING", "Pending"
        MATCH = "MATCH", "Match result"
        BYE = "BYE", "Bye"
        SAME_ENTRY_FINAL = "SAME_ENTRY_FINAL", "Both tournament lives reached the final"
        SAME_ENTRY_THIRD = "SAME_ENTRY_THIRD", "Both tournament lives shared third place"

    bracket = models.ForeignKey(
        PoolKnockoutBracket,
        on_delete=models.CASCADE,
        related_name="ties",
    )
    round_number = models.PositiveSmallIntegerField()
    position = models.PositiveSmallIntegerField()
    round_name = models.CharField(max_length=100)
    race_target = models.PositiveSmallIntegerField()
    is_third_place = models.BooleanField(default=False)
    match = models.OneToOneField(
        PoolMatch,
        on_delete=models.SET_NULL,
        related_name="knockout_tie",
        null=True,
        blank=True,
    )
    resolved_winner = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="resolved_pool_knockout_ties",
        null=True,
        blank=True,
    )
    resolved_winner_life = models.ForeignKey(
        PoolCompetitionEntryLife,
        on_delete=models.RESTRICT,
        related_name="resolved_pool_knockout_ties",
        null=True,
        blank=True,
    )
    resolution_method = models.CharField(
        max_length=20,
        choices=ResolutionMethod.choices,
        default=ResolutionMethod.PENDING,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["round_number", "is_third_place", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bracket", "round_number", "position", "is_third_place"],
                name="pool_unique_bracket_tie_position",
            ),
            models.CheckConstraint(
                condition=Q(round_number__gte=1),
                name="pool_bracket_tie_round_positive",
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1),
                name="pool_bracket_tie_position_positive",
            ),
            models.CheckConstraint(
                condition=Q(race_target__gte=1),
                name="pool_bracket_tie_race_positive",
            ),
        ]

    def __str__(self):
        return f"{self.bracket}: {self.round_name} {self.position}"

    @property
    def competition(self):
        return self.bracket.competition

    def clean(self):
        errors = {}
        if self.match_id:
            if self.match.competition_id != self.bracket.competition_id:
                errors["match"] = "Bracket match must belong to the bracket competition."
            elif self.match.stage_id != self.bracket.stage_id:
                errors["match"] = "Bracket match must belong to the bracket stage."
            elif self.pk:
                slots = {
                    slot.slot: slot
                    for slot in PoolKnockoutTieSlot.objects.filter(tie_id=self.pk)
                }
                if slots and (
                    self.match.home_entry_id
                    != getattr(slots.get(PoolKnockoutTieSlot.Slot.HOME), "entry_id", None)
                    or self.match.away_entry_id
                    != getattr(slots.get(PoolKnockoutTieSlot.Slot.AWAY), "entry_id", None)
                ):
                    errors["match"] = (
                        "Bracket fixture participants must match the explicit tie slots."
                    )
                elif slots and (
                    self.match.home_entry_life_id
                    != getattr(
                        slots.get(PoolKnockoutTieSlot.Slot.HOME),
                        "entry_life_id",
                        None,
                    )
                    or self.match.away_entry_life_id
                    != getattr(
                        slots.get(PoolKnockoutTieSlot.Slot.AWAY),
                        "entry_life_id",
                        None,
                    )
                ):
                    errors["match"] = (
                        "Bracket fixture tournament lives must match the explicit tie slots."
                    )
        if self.resolved_winner_id and (
            self.resolved_winner.competition_id != self.bracket.competition_id
        ):
            errors["resolved_winner"] = "Resolved winner must be an entry in this competition."
        elif self.resolved_winner_id and self.pk:
            slot_entry_ids = set(
                PoolKnockoutTieSlot.objects.filter(tie_id=self.pk).values_list(
                    "entry_id", flat=True
                )
            )
            slot_entry_ids.discard(None)
            if slot_entry_ids and self.resolved_winner_id not in slot_entry_ids:
                errors["resolved_winner"] = (
                    "Resolved winner must occupy this bracket tie."
                )
        if self.resolved_winner_life_id:
            if not self.resolved_winner_id:
                errors["resolved_winner_life"] = (
                    "A resolved tournament life requires a resolved entry."
                )
            elif (
                self.resolved_winner_life.competition_entry_id
                != self.resolved_winner_id
            ):
                errors["resolved_winner_life"] = (
                    "The resolved tournament life must belong to the resolved entry."
                )
            elif self.pk and not PoolKnockoutTieSlot.objects.filter(
                tie_id=self.pk,
                entry_life_id=self.resolved_winner_life_id,
            ).exists():
                errors["resolved_winner_life"] = (
                    "Resolved tournament life must occupy this bracket tie."
                )
        if self.resolution_method == self.ResolutionMethod.BYE and not self.resolved_winner_id:
            errors["resolved_winner"] = "A bye resolution requires an advancing entry."
        if self.resolution_method in {
            self.ResolutionMethod.SAME_ENTRY_FINAL,
            self.ResolutionMethod.SAME_ENTRY_THIRD,
        } and not self.resolved_winner_id:
            errors["resolved_winner"] = (
                "A same-entry convergence resolution requires its shared entry."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolKnockoutTieSlot(models.Model):
    """One explicit source and resolved participant position in a bracket tie."""

    class Slot(models.TextChoices):
        HOME = "HOME", "Home"
        AWAY = "AWAY", "Away"

    class SourceType(models.TextChoices):
        DIRECT_ENTRY = "DIRECT_ENTRY", "Direct entrant"
        GROUP_QUALIFIER = "GROUP_QUALIFIER", "Group-position qualifier"
        TIE_WINNER = "TIE_WINNER", "Winner of an earlier match"
        TIE_LOSER = "TIE_LOSER", "Loser of an earlier match"
        BYE = "BYE", "Bye"
        TBD = "TBD", "To be determined"

    tie = models.ForeignKey(
        PoolKnockoutTie,
        on_delete=models.CASCADE,
        related_name="slots",
    )
    slot = models.CharField(max_length=4, choices=Slot.choices)
    entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="bracket_slots",
        null=True,
        blank=True,
    )
    entry_life = models.ForeignKey(
        PoolCompetitionEntryLife,
        on_delete=models.RESTRICT,
        related_name="bracket_slots",
        null=True,
        blank=True,
    )
    source_type = models.CharField(
        max_length=24,
        choices=SourceType.choices,
        default=SourceType.TBD,
    )
    source_entry = models.ForeignKey(
        PoolCompetitionEntry,
        on_delete=models.RESTRICT,
        related_name="direct_bracket_slot_sources",
        null=True,
        blank=True,
    )
    source_entry_life = models.ForeignKey(
        PoolCompetitionEntryLife,
        on_delete=models.RESTRICT,
        related_name="direct_bracket_slot_sources",
        null=True,
        blank=True,
    )
    source_group = models.ForeignKey(
        PoolGroup,
        on_delete=models.RESTRICT,
        related_name="bracket_qualification_slots",
        null=True,
        blank=True,
    )
    source_position = models.PositiveSmallIntegerField(null=True, blank=True)
    source_tie = models.ForeignKey(
        PoolKnockoutTie,
        on_delete=models.RESTRICT,
        related_name="dependent_slots",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tie__round_number", "tie__position", "slot", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tie", "slot"],
                name="pool_unique_bracket_tie_slot",
            ),
            models.CheckConstraint(
                condition=Q(source_position__isnull=True) | Q(source_position__gte=1),
                name="pool_bracket_slot_source_position_positive",
            ),
        ]

    def __str__(self):
        return f"{self.tie} {self.get_slot_display()}"

    def clean(self):
        errors = {}
        competition_id = self.tie.bracket.competition_id if self.tie_id else None
        if self.entry_id and self.entry.competition_id != competition_id:
            errors["entry"] = "Bracket entry must belong to the bracket competition."
        if self.source_entry_id and self.source_entry.competition_id != competition_id:
            errors["source_entry"] = "Source entry must belong to the bracket competition."
        if self.entry_life_id:
            if not self.entry_id:
                errors["entry_life"] = "A bracket life requires a resolved bracket entry."
            elif self.entry_life.competition_entry_id != self.entry_id:
                errors["entry_life"] = (
                    "The bracket life must belong to the resolved bracket entry."
                )
        if self.source_entry_life_id:
            if not self.source_entry_id:
                errors["source_entry_life"] = (
                    "A source tournament life requires a direct source entry."
                )
            elif (
                self.source_entry_life.competition_entry_id
                != self.source_entry_id
            ):
                errors["source_entry_life"] = (
                    "The source tournament life must belong to its source entry."
                )
        if self.source_group_id:
            if self.source_group.stage.competition_id != competition_id:
                errors["source_group"] = "Source group must belong to the bracket competition."
            elif self.source_group.stage.order >= self.tie.bracket.stage.order:
                errors["source_group"] = (
                    "A group qualification source must precede its knockout stage."
                )
            if not self.source_position:
                errors["source_position"] = "A group source needs a finishing position."
        if self.source_tie_id:
            if self.source_tie.bracket_id != self.tie.bracket_id:
                errors["source_tie"] = "A bracket slot source must stay in the same bracket."
            elif self.source_tie_id == self.tie_id:
                errors["source_tie"] = "A bracket tie cannot source itself."
            elif self.source_tie.round_number > self.tie.round_number:
                errors["source_tie"] = "A bracket source must come from an earlier round."
            elif (
                self.source_tie.round_number == self.tie.round_number
                and not (
                    self.tie.is_third_place
                    and not self.source_tie.is_third_place
                    and self.source_type == self.SourceType.TIE_LOSER
                )
            ):
                errors["source_tie"] = "A bracket source must come from an earlier round."

        source_fields = {
            "source_entry": bool(self.source_entry_id),
            "source_group": bool(self.source_group_id),
            "source_tie": bool(self.source_tie_id),
        }
        if self.source_type == self.SourceType.DIRECT_ENTRY:
            if not source_fields["source_entry"] or source_fields["source_group"] or source_fields["source_tie"]:
                errors["source_type"] = "A direct slot must reference exactly one entry."
            elif self.entry_id and self.entry_id != self.source_entry_id:
                errors["entry"] = "A direct slot must retain its configured entrant."
            elif self.source_entry_life_id and self.entry_life_id != self.source_entry_life_id:
                errors["entry_life"] = (
                    "A direct slot must retain its configured tournament life."
                )
        elif self.source_type == self.SourceType.GROUP_QUALIFIER:
            if not source_fields["source_group"] or source_fields["source_entry"] or source_fields["source_tie"]:
                errors["source_type"] = "A group slot must reference exactly one group position."
            if self.entry_life_id or self.source_entry_life_id:
                errors["entry_life"] = (
                    "Group qualification slots do not use tournament-life sources."
                )
        elif self.source_type in {self.SourceType.TIE_WINNER, self.SourceType.TIE_LOSER}:
            if not source_fields["source_tie"] or source_fields["source_entry"] or source_fields["source_group"]:
                errors["source_type"] = "An advancing slot must reference exactly one prior tie."
            if self.source_entry_life_id:
                errors["source_entry_life"] = (
                    "An advancing slot derives its life from the source tie."
                )
        elif self.source_type in {self.SourceType.BYE, self.SourceType.TBD}:
            if any(source_fields.values()) or self.source_position:
                errors["source_type"] = "Bye and TBD slots cannot have a participant source."
            if self.entry_id or self.entry_life_id or self.source_entry_life_id:
                errors["entry"] = "Bye and TBD slots cannot contain an entrant."

        if errors:
            raise ValidationError(errors)

    @property
    def source_label(self):
        if self.source_type == self.SourceType.DIRECT_ENTRY and self.source_entry_id:
            return self.source_entry.display_name
        if self.source_type == self.SourceType.GROUP_QUALIFIER and self.source_group_id:
            return f"{self.source_group.name} #{self.source_position}"
        if self.source_type == self.SourceType.TIE_WINNER and self.source_tie_id:
            return f"Winner of {self.source_tie.round_name} {self.source_tie.position}"
        if self.source_type == self.SourceType.TIE_LOSER and self.source_tie_id:
            return f"Loser of {self.source_tie.round_name} {self.source_tie.position}"
        if self.source_type == self.SourceType.BYE:
            return "Bye"
        return "TBD"

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolBracketAuditEvent(models.Model):
    """Audits bracket topology and knockout-stage structural operations."""

    class Action(models.TextChoices):
        DRAW_PREVIEWED = "DRAW_PREVIEWED", "Draw previewed"
        BRACKET_GENERATED = "BRACKET_GENERATED", "Bracket generated"
        BYE_ADVANCED = "BYE_ADVANCED", "Bye advanced"
        SLOT_UPDATED = "SLOT_UPDATED", "Bracket slot updated"
        BRACKET_RESET = "BRACKET_RESET", "Bracket reset"
        ADVANCEMENT_BLOCKED = "ADVANCEMENT_BLOCKED", "Advancement blocked"
        SAME_ENTRY_FINAL = "SAME_ENTRY_FINAL", "Both tournament lives reached the final"
        SAME_ENTRY_THIRD = "SAME_ENTRY_THIRD", "Both tournament lives shared third place"
        ROUND_DEFS_CONFIGURED = "ROUND_DEFS_CONFIGURED", "Knockout rounds configured"

    bracket = models.ForeignKey(
        PoolKnockoutBracket,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    # These stable scope fields preserve the audit record after an owner-only
    # pre-activity reset removes the bracket topology itself.
    competition = models.ForeignKey(
        PoolCompetition,
        # Resetting only a bracket deliberately retains audit history through
        # its nullable ``bracket`` field.  Deleting the parent competition is
        # a deliberate superuser lifecycle action, so its audit is owned data
        # and must cascade with the rest of the competition graph.
        on_delete=models.CASCADE,
        related_name="bracket_audit_events",
        null=True,
        blank=True,
    )
    stage = models.ForeignKey(
        PoolCompetitionStage,
        on_delete=models.SET_NULL,
        related_name="bracket_audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="pool_bracket_audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=24, choices=Action.choices)
    message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["bracket", "-created_at"],
                name="pool_bracket_audit_date_idx",
            ),
            models.Index(
                fields=["competition", "stage", "-created_at"],
                name="pool_bracket_audit_scope_idx",
            ),
        ]

    def __str__(self):
        return f"{self.bracket or 'Removed bracket'}: {self.get_action_display()}"

    def clean(self):
        errors = {}
        if self.bracket_id:
            if (
                self.competition_id
                and self.competition_id != self.bracket.competition_id
            ):
                errors["competition"] = "Audit competition must match its bracket."
            if self.stage_id and self.stage_id != self.bracket.stage_id:
                errors["stage"] = "Audit stage must match its bracket."
        if self.stage_id and self.competition_id and (
            self.stage.competition_id != self.competition_id
        ):
            errors["stage"] = "Audit stage must belong to the audit competition."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
