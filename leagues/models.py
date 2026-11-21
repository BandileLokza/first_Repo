from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import F
from django.utils.text import slugify
from teams.storage_backends import SupabaseStorage
from .validators import validate_fixture_letterhead, validate_report_template
from .storage import fixture_letterhead_storage, report_template_storage


class Region(models.Model):
    """A public football association/area that groups league series."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)
    display_order = models.PositiveIntegerField(default=100, db_index=True)
    is_active = models.BooleanField(default=True)
    # Region identity outlives any individual league season.
    logo = models.ImageField(
        upload_to="football/regions/",
        storage=SupabaseStorage(folder="football/regions", image_profile="logo"),
        blank=True,
        null=True,
    )
    report_template = models.FileField(
        "Word report template (.docx)",
        storage=report_template_storage,
        upload_to="football/report-templates/regions/",
        blank=True,
        null=True,
        validators=[validate_report_template],
        help_text="Reusable Word stationery with a page Header for football fixtures and results.",
    )

    fixture_letterhead = models.ImageField(
        "Fixture letterhead",
        storage=fixture_letterhead_storage,
        upload_to="football/letterheads/regions/",
        blank=True,
        null=True,
        validators=[validate_fixture_letterhead],
        help_text="Optional PNG or JPEG stationery top strip for football fixtures.",
    )

    # Newspaper report branding is optional so existing regions retain their
    # current public identity and receive safe name-based fallbacks.
    report_abbreviation = models.CharField(max_length=30, blank=True)
    report_subheading = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["display_order", "name", "id"]

    def __str__(self):
        return self.name


class FootballOrganisation(models.Model):
    """Branding identity for a non-regional football organisation."""

    class Code(models.TextChoices):
        EWFA = "EWFA", "EWFA"
        PLE = "PLE", "Premier League of Eswatini (PLE)"

    code = models.CharField(max_length=10, choices=Code.choices, unique=True)
    name = models.CharField(max_length=160)
    logo = models.ImageField(
        upload_to="football/organisations/",
        storage=SupabaseStorage(folder="football/organisations", image_profile="logo"),
        blank=True,
        null=True,
    )

    report_template = models.FileField(
        "Word report template (.docx)",
        storage=report_template_storage,
        upload_to="football/report-templates/organisations/",
        blank=True,
        null=True,
        validators=[validate_report_template],
        help_text="Reusable Word stationery with a page Header for football fixtures and results.",
    )

    fixture_letterhead = models.ImageField(
        "Fixture letterhead",
        storage=fixture_letterhead_storage,
        upload_to="football/letterheads/organisations/",
        blank=True,
        null=True,
        validators=[validate_fixture_letterhead],
        help_text="Optional PNG or JPEG stationery top strip for football fixtures.",
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.name


class FootballHomepageSection(models.Model):
    """The administrator-controlled order of Football's directory sections.

    These rows deliberately model the four permanent homepage sections rather
    than leagues or seasons. Moving one section shifts the others into a
    compact, unambiguous order without touching any football data.
    """

    class Section(models.TextChoices):
        REGIONS = "regions", "Regions"
        EWFA = "ewfa", "EWFA"
        PLE = "ple", "PLE"
        ZONAL = "zonal", "Zonal Leagues"

    SECTION_COUNT = len(Section)

    section = models.CharField(max_length=16, choices=Section.choices, unique=True)
    position = models.PositiveSmallIntegerField(
        default=SECTION_COUNT,
        validators=[MinValueValidator(1), MaxValueValidator(SECTION_COUNT)],
        help_text="Lower numbers appear first on the Football homepage.",
    )

    class Meta:
        ordering = ["position", "section"]
        verbose_name = "Football Section"
        verbose_name_plural = "Football Section Ordering"

    def __str__(self):
        return self.get_section_display()

    def clean(self):
        super().clean()
        if self.pk:
            current_section = type(self).objects.filter(pk=self.pk).values_list(
                "section", flat=True
            ).first()
            if current_section and current_section != self.section:
                raise ValidationError({"section": "The required homepage section cannot be changed."})

    def save(self, *args, **kwargs):
        """Move an existing section while keeping positions one through four.

        ``position`` is intentionally not database-unique: a unique constraint
        makes a normal admin move (for example PLE from 3 to 1) fail before the
        other rows can be shifted. The transaction below performs that shift
        atomically, leaving a unique compact sequence after every save.
        """
        if self._state.adding:
            return super().save(*args, **kwargs)

        if not 1 <= self.position <= self.SECTION_COUNT:
            raise ValidationError({"position": "Choose a position from 1 to 4."})

        model = type(self)
        with transaction.atomic():
            current = model.objects.select_for_update().get(pk=self.pk)
            if current.section != self.section:
                raise ValidationError({"section": "The required homepage section cannot be changed."})

            if self.position < current.position:
                model.objects.select_for_update().filter(
                    position__gte=self.position,
                    position__lt=current.position,
                ).exclude(pk=self.pk).update(position=F("position") + 1)
            elif self.position > current.position:
                model.objects.select_for_update().filter(
                    position__gt=current.position,
                    position__lte=self.position,
                ).exclude(pk=self.pk).update(position=F("position") - 1)

            return super().save(*args, **kwargs)


class CompetitionSeries(models.Model):
    """The permanent identity shared by seasons of one football league."""

    class LeagueType(models.TextChoices):
        # This is the football section of the permanent league identity.  The
        # historic ``league_type`` column is deliberately retained: it already
        # expresses the category and keeping it avoids a destructive rename for
        # existing production integrations and administration data.
        REGIONAL = "REGIONAL", "Regional"
        EWFA = "EWFA", "EWFA"
        PLE = "PLE", "Premier League of Eswatini (PLE)"
        ZONAL = "ZONAL", "Zonal Leagues"

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    league_type = models.CharField(
        verbose_name="Football section",
        max_length=10,
        choices=LeagueType.choices,
        default=LeagueType.ZONAL,
        db_index=True,
    )
    region = models.ForeignKey(
        Region,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="league_series",
    )
    # This belongs to the permanent league identity, rather than a season.
    logo = models.ImageField(
        upload_to="football/leagues/",
        storage=SupabaseStorage(folder="football/leagues", image_profile="logo"),
        blank=True,
        null=True,
    )
    report_template = models.FileField(
        "Word report template (.docx)",
        storage=report_template_storage,
        upload_to="football/report-templates/leagues/",
        blank=True,
        null=True,
        validators=[validate_report_template],
        help_text="Reusable Word stationery with a page Header for football fixtures and results.",
    )

    fixture_letterhead = models.ImageField(
        "Fixture letterhead",
        storage=fixture_letterhead_storage,
        upload_to="football/letterheads/leagues/",
        blank=True,
        null=True,
        validators=[validate_fixture_letterhead],
        help_text="Optional PNG or JPEG stationery top strip for football fixtures.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def clean(self):
        if self.league_type == self.LeagueType.REGIONAL and not self.region_id:
            raise ValidationError({"region": "Regional leagues must be assigned to a region."})
        # Region membership is meaningful only for Regional football.  Clear a
        # stale browser/admin value rather than carrying an inappropriate
        # association into EWFA, PLE, or independent Zonal sections.
        if self.league_type != self.LeagueType.REGIONAL:
            self.region = None

    def save(self, *args, **kwargs):
        """Keep non-regional series detached from regional administration."""
        if self.league_type != self.LeagueType.REGIONAL:
            self.region = None
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "region" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "region"]
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ReportDocumentSettings(models.Model):
    """Official closing details owned by exactly one permanent stationery scope."""

    region = models.OneToOneField(Region, on_delete=models.CASCADE, null=True, blank=True)
    organisation = models.OneToOneField(FootballOrganisation, on_delete=models.CASCADE, null=True, blank=True)
    series = models.OneToOneField(
        CompetitionSeries, on_delete=models.CASCADE, null=True, blank=True,
        limit_choices_to={"league_type": CompetitionSeries.LeagueType.ZONAL},
    )
    signoff_enabled = models.BooleanField("Include official sign-off", default=False)
    signoff_heading = models.CharField("Sign-off heading", max_length=80, default="Yours Faithfully", blank=True)
    signatory_name = models.CharField("Full name", max_length=160, blank=True)
    signatory_position = models.CharField("Designation", max_length=120, blank=True)
    # Reusable signing artwork is private. Small, normalized PNGs are stored
    # in the database and are only read for approved final exports.
    signature_image = models.BinaryField(null=True, editable=False)
    stamp_image = models.BinaryField(null=True, editable=False)
    committee_enabled = models.BooleanField("Include executive committee", default=False)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(
                models.Q(region__isnull=False, organisation__isnull=True, series__isnull=True)
                | models.Q(region__isnull=True, organisation__isnull=False, series__isnull=True)
                | models.Q(region__isnull=True, organisation__isnull=True, series__isnull=False)
            ),
            name="report_document_settings_one_owner",
        )]

    @staticmethod
    def owner_filter(owner):
        if isinstance(owner, Region):
            return {"region": owner}
        if isinstance(owner, FootballOrganisation):
            return {"organisation": owner}
        if isinstance(owner, CompetitionSeries) and owner.league_type == CompetitionSeries.LeagueType.ZONAL:
            return {"series": owner}
        raise ValueError("Unsupported report stationery owner.")

    @classmethod
    def for_owner(cls, owner, *, include_signature=False, include_stamp=False):
        deferred = [field for field, include in (
            ("signature_image", include_signature), ("stamp_image", include_stamp),
        ) if not include]
        return cls.objects.defer(*deferred).prefetch_related("committee_members").filter(
            **cls.owner_filter(owner),
        ).first()


class ReportCommitteeMember(models.Model):
    settings = models.ForeignKey(ReportDocumentSettings, on_delete=models.CASCADE, related_name="committee_members")
    position = models.CharField(max_length=120)
    name = models.CharField(max_length=160)
    contact = models.CharField("Contact number", max_length=40, blank=True)
    display_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ("display_order", "pk")


class Competition(models.Model):

    FORMAT_CHOICES = [
        ("TABLE", "Table Only"),
        ("GROUP_KNOCKOUT", "Group + Knockout"),
        ("KNOCKOUT", "Knockout Only"),
    ]

    KNOCKOUT_SIZE_CHOICES = [
        (2, "2 teams"),
        (4, "4 teams"),
        (8, "8 teams"),
        (16, "16 teams"),
        (32, "32 teams"),
    ]

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    season = models.CharField(max_length=20)

    # ``blank=True`` preserves the existing admin/direct-ORM creation flow.
    # ``save()`` below gives such a new competition its own series before the
    # non-null database constraint is reached.  Next-season creation always
    # supplies an existing series explicitly.
    series = models.ForeignKey(
        CompetitionSeries,
        on_delete=models.PROTECT,
        related_name="competitions",
        blank=True,
    )

    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="archived_competitions",
    )

    # Publication protection is deliberately explicit and season-scoped.  A
    # null timestamp means this competition remains editable; no historical
    # fixtures are inferred or backfilled when the feature is introduced.
    schedule_locked_at = models.DateTimeField(null=True, blank=True)
    schedule_locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="schedule_locked_competitions",
    )

    format_type = models.CharField(
        max_length=20,
        choices=FORMAT_CHOICES,
        default="TABLE"
    )

    knockout_size = models.PositiveSmallIntegerField(
        choices=KNOCKOUT_SIZE_CHOICES,
        null=True,
        blank=True,
        help_text=(
            "Number of teams entering the knockout stage. "
            "Configured by the league owner."
        ),
    )

    predictions_enabled = models.BooleanField(default=False)

    # Sponsorship is season-specific: a later season may have another sponsor.
    sponsor_name = models.CharField(max_length=100, blank=True)
    sponsor_logo = models.ImageField(
        upload_to="football/sponsors/",
        storage=SupabaseStorage(folder="football/sponsors", image_profile="sponsor"),
        blank=True,
        null=True,
    )

    # Editorial priority for the public current-league directory.  A lower
    # value appears first; it is intentionally independent of season/status.
    display_order = models.PositiveIntegerField(default=100, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["series", "season"],
                name="unique_competition_season_per_series",
            ),
        ]

    @staticmethod
    def _new_series_slug(name, competition_slug):
        """Return a safe unique slug for a one-season legacy competition."""

        max_length = CompetitionSeries._meta.get_field("slug").max_length
        base = slugify(competition_slug or name) or "competition"
        base = f"{base}-series"[:max_length]
        candidate = base
        suffix_number = 2

        while CompetitionSeries.objects.filter(slug=candidate).exists():
            suffix = f"-{suffix_number}"
            candidate = f"{base[:max_length - len(suffix)]}{suffix}"
            suffix_number += 1

        return candidate

    def save(self, *args, **kwargs):
        """Keep existing direct competition creation safely series-backed.

        The established project creates competitions directly in Django admin
        and in tests.  Treat each of those as a new independent league series;
        it never joins records merely because their names happen to match.
        """

        if self._state.adding and not self.series_id:
            with transaction.atomic():
                self.series = CompetitionSeries.objects.create(
                    name=self.name,
                    slug=self._new_series_slug(self.name, self.slug),
                )
                return super().save(*args, **kwargs)

        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def schedule_is_locked(self):
        """Whether this season's published fixture structure is protected."""

        return self.schedule_locked_at is not None


class CompetitionTieBreaker(models.Model):
    """One ordered standings tie-breaker configured for a competition."""

    class Criterion(models.TextChoices):
        POINTS = "POINTS", "Points"
        OVERALL_GOAL_DIFFERENCE = (
            "OVERALL_GOAL_DIFFERENCE",
            "Overall Goal Difference",
        )
        OVERALL_GOALS_SCORED = (
            "OVERALL_GOALS_SCORED",
            "Overall Goals Scored",
        )
        HEAD_TO_HEAD_POINTS = "HEAD_TO_HEAD_POINTS", "Head-to-Head Points"
        HEAD_TO_HEAD_GOAL_DIFFERENCE = (
            "HEAD_TO_HEAD_GOAL_DIFFERENCE",
            "Head-to-Head Goal Difference",
        )
        HEAD_TO_HEAD_GOALS_SCORED = (
            "HEAD_TO_HEAD_GOALS_SCORED",
            "Head-to-Head Goals Scored",
        )
        HEAD_TO_HEAD_AWAY_GOALS = (
            "HEAD_TO_HEAD_AWAY_GOALS",
            "Head-to-Head Away Goals",
        )
        WINS = "WINS", "Wins"
        FAIR_PLAY = "FAIR_PLAY", "Fair Play"
        DRAWING_OF_LOTS = (
            "DRAWING_OF_LOTS",
            "Drawing of Lots / Manual Decision",
        )

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="tie_breakers",
    )
    criterion = models.CharField(
        max_length=32,
        choices=Criterion.choices,
    )
    order = models.PositiveSmallIntegerField(
        verbose_name="Position",
        help_text="Points must remain at position 1.",
    )

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "criterion"],
                name="unique_tie_breaker_criterion_per_competition",
            ),
            models.UniqueConstraint(
                fields=["competition", "order"],
                name="unique_tie_breaker_order_per_competition",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(criterion="POINTS", order=1)
                    | (
                        ~models.Q(criterion="POINTS")
                        & models.Q(order__gte=2)
                    )
                ),
                name="competition_tie_breaker_points_first",
            ),
        ]

    def clean(self):
        """Give administrators clear validation before database constraints run."""
        errors = {}
        valid_order = isinstance(self.order, int) and not isinstance(self.order, bool)

        if self.criterion == self.Criterion.POINTS and self.order != 1:
            errors["order"] = "Points must be at position 1."
        elif self.criterion != self.Criterion.POINTS and valid_order:
            if self.order < 2:
                errors["order"] = (
                    "Only Points may use position 1; other criteria start at position 2."
                )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.competition.name}: {self.order}. "
            f"{self.get_criterion_display()}"
        )


class HomepageBanner(models.Model):
    title = models.CharField(max_length=150, blank=True)
    subtitle = models.CharField(max_length=255, blank=True)
    image = models.ImageField(
        upload_to="home/",
        storage=SupabaseStorage(folder="home", image_profile="general")
    )
    sponsor_name = models.CharField(max_length=100, blank=True)
    sponsor_logo = models.ImageField(
        upload_to="home/",
        storage=SupabaseStorage(folder="home", image_profile="sponsor"),
        blank=True,
        null=True
    )

    button_text = models.CharField(max_length=50, blank=True)
    button_link = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "-created_at"]

    def __str__(self):
        if self.title and self.sponsor_name:
            return f"{self.title} - {self.sponsor_name}"
        if self.title:
            return self.title
        if self.sponsor_name:
            return f"Banner - {self.sponsor_name}"
        return f"Banner #{self.pk or 'new'}"

class SiteIdentity(models.Model):
    site_name = models.CharField(max_length=100, default="EsportsLive")

    emblem = models.ImageField(
        upload_to="home/",
        storage=SupabaseStorage(folder="home", image_profile="logo"),
        blank=True,
        null=True
    )

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.site_name


class Stage(models.Model):

    STAGE_TYPES = [
        ("LEAGUE", "League Table"),
        ("GROUP", "Group Stage"),
        ("KNOCKOUT", "Knockout"),
    ]

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="stages"
    )

    name = models.CharField(max_length=100)
    stage_type = models.CharField(max_length=20, choices=STAGE_TYPES)
    order = models.IntegerField()

    def __str__(self):
        return f"{self.competition.name} - {self.name}"


class Group(models.Model):
    stage = models.ForeignKey(
        Stage,
        on_delete=models.CASCADE,
        related_name="groups"
    )

    name = models.CharField(max_length=20)

    teams = models.ManyToManyField(
        "teams.Team",
        related_name="group_memberships",
        blank=True
    )

    def __str__(self):
        return f"{self.stage.name} - {self.name}"    


class Standing(models.Model):

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="standings"
    )

    stage = models.ForeignKey(
        Stage,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="standings"
    )

    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="standings"
    )

    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE
    )

    played = models.IntegerField(default=0)
    won = models.IntegerField(default=0)
    drawn = models.IntegerField(default=0)
    lost = models.IntegerField(default=0)

    goals_for = models.IntegerField(default=0)
    goals_against = models.IntegerField(default=0)

    points = models.IntegerField(default=0)

    baseline_played = models.IntegerField(default=0)
    baseline_won = models.IntegerField(default=0)
    baseline_drawn = models.IntegerField(default=0)
    baseline_lost = models.IntegerField(default=0)
    baseline_goals_for = models.IntegerField(default=0)
    baseline_goals_against = models.IntegerField(default=0)
    baseline_points = models.IntegerField(default=0)

    @property
    def total_played(self):
        return self.baseline_played + self.played

    @property
    def total_won(self):
        return self.baseline_won + self.won

    @property
    def total_drawn(self):
        return self.baseline_drawn + self.drawn

    @property
    def total_lost(self):
        return self.baseline_lost + self.lost

    @property
    def total_goals_for(self):
        return self.baseline_goals_for + self.goals_for

    @property
    def total_goals_against(self):
        return self.baseline_goals_against + self.goals_against

    @property
    def total_points(self):
        return self.baseline_points + self.points

    @property
    def total_goal_difference(self):
        return self.total_goals_for - self.total_goals_against

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.won + self.drawn + self.lost != self.played:
            errors["played"] = "Won + Drawn + Lost must equal Played."
        if self.team_id and self.competition_id and self.team.competition_id != self.competition_id:
            errors["team"] = "A standing team must belong to the same competition."
        if self.stage_id and self.stage.competition_id != self.competition_id:
            errors["stage"] = "A standing stage must belong to the same competition."
        if self.group_id:
            if self.group.stage.competition_id != self.competition_id:
                errors["group"] = "A standing group must belong to the same competition."
            if self.stage_id != self.group.stage_id:
                errors["group"] = "A standing group must belong to the standing stage."
        if errors:
            raise ValidationError(errors)
        
    class Meta:
        unique_together = ("competition", "stage", "group", "team")

    def goal_difference(self):
        return self.goals_for - self.goals_against

    def __str__(self):
        return f"{self.team.name} - {self.competition.name}"
