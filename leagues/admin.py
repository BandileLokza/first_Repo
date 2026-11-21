from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.models import (
    BaseInlineFormSet,
    InlineForeignKeyField,
    construct_instance,
)

from .models import (
    Competition,
    CompetitionSeries,
    CompetitionTieBreaker,
    FootballHomepageSection,
    FootballOrganisation,
    Group,
    HomepageBanner,
    SiteIdentity,
    Stage,
    Standing,
    Region,
)
from .services import (
    competition_has_format_data,
    DEFAULT_COMPETITION_TIE_BREAKERS,
    ensure_default_tie_breakers,
    ensure_competition_structure,
    replace_competition_tie_breakers,
    rebuild_competition_structure,
    validate_tie_breaker_configuration,
)


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("name", "report_abbreviation", "display_order", "is_active", "logo")
    list_editable = ("display_order", "is_active")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)

    def get_readonly_fields(self, request, obj=None):
        from league_manager.permissions import can_manage_region
        if not request.user.is_superuser and not (obj and can_manage_region(request.user, obj)):
            return (*super().get_readonly_fields(request, obj), "fixture_letterhead", "report_template")
        return super().get_readonly_fields(request, obj)


@admin.register(FootballOrganisation)
class FootballOrganisationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "logo")
    search_fields = ("name", "code")
    readonly_fields = ("code", "name")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FootballHomepageSection)
class FootballHomepageSectionAdmin(admin.ModelAdmin):
    list_display = ("section_name", "position")
    list_display_links = ("section_name",)
    list_editable = ("position",)
    readonly_fields = ("section",)
    fields = ("section", "position")

    @admin.display(description="Football Section", ordering="section")
    def section_name(self, obj):
        return obj.get_section_display()

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CompetitionSeries)
class CompetitionSeriesAdmin(admin.ModelAdmin):
    list_display = ("name", "league_type", "region", "logo", "slug")
    list_filter = ("league_type", "region")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}

    def get_readonly_fields(self, request, obj=None):
        from league_manager.models import LeagueMembership
        from league_manager.permissions import can_manage_organisation
        allowed = request.user.is_superuser or bool(
            obj and obj.league_type == obj.LeagueType.ZONAL and (
                can_manage_organisation(request.user, obj.LeagueType.ZONAL)
                or LeagueMembership.objects.filter(
                    user=request.user, competition__series=obj,
                    competition__is_archived=False, is_active=True,
                    role=LeagueMembership.Role.OWNER,
                ).exists()
            )
        )
        if not allowed:
            return (*super().get_readonly_fields(request, obj), "fixture_letterhead", "report_template")
        return super().get_readonly_fields(request, obj)

    class Media:
        js = ("admin/js/football_section.js",)


class CompetitionAdminForm(forms.ModelForm):
    league_type = forms.ChoiceField(
        label="Football Section",
        choices=CompetitionSeries.LeagueType.choices,
        initial=CompetitionSeries.LeagueType.ZONAL,
    )
    region = forms.ModelChoiceField(
        queryset=Region.objects.filter(is_active=True).order_by("display_order", "name"),
        required=False,
    )

    class Meta:
        model = Competition
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.series_id:
            # A new competition may omit series and receive its own legacy
            # series in Competition.save(). An existing competition cannot:
            # accepting an omitted value here would defer the error until the
            # non-null database constraint instead of showing a form error.
            self.fields["series"].required = True
            self.fields["league_type"].initial = self.instance.series.league_type
            self.fields["region"].initial = self.instance.series.region_id

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("league_type") == CompetitionSeries.LeagueType.REGIONAL
            and not cleaned_data.get("region")
        ):
            self.add_error("region", "Select a region for a regional league.")
        if cleaned_data.get("league_type") != CompetitionSeries.LeagueType.REGIONAL:
            cleaned_data["region"] = None
        return cleaned_data

    def clean_format_type(self):
        new_format = self.cleaned_data["format_type"]

        # A new competition does not have an existing format yet.
        if not self.instance.pk:
            return new_format

        old_format = (
            Competition.objects
            .filter(pk=self.instance.pk)
            .values_list("format_type", flat=True)
            .first()
        )

        if old_format == new_format:
            return new_format

        if competition_has_format_data(self.instance):
            raise ValidationError(
                "You cannot change the competition format after "
                "teams, groups, matches or standings have been added."
            )

        return new_format

    class Media:
        js = ("admin/js/football_section.js",)


class CompetitionTieBreakerInlineFormSet(BaseInlineFormSet):
    """Keep each competition's ordered criteria complete and unambiguous."""

    DEFAULT_ROWS = DEFAULT_COMPETITION_TIE_BREAKERS

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)

        if (
            not self.is_bound
            and self.instance.pk is None
            and index is not None
            and index < len(self.DEFAULT_ROWS)
        ):
            position, criterion = self.DEFAULT_ROWS[index]
            initial = dict(kwargs.get("initial", {}))
            initial.update({
                "order": position,
                "criterion": criterion,
            })
            kwargs["initial"] = initial

        return kwargs

    def clean(self):
        super().clean()

        if any(self.errors):
            return

        self._desired_configuration = validate_tie_breaker_configuration(
            [
                (form.cleaned_data.get("order"), form.cleaned_data.get("criterion"))
                for form in self.forms
                if hasattr(form, "cleaned_data")
                and not form.cleaned_data.get("DELETE", False)
                and form.cleaned_data.get("criterion")
                and form.cleaned_data.get("order") is not None
            ]
        )

    def save(self, commit=True):
        """Atomically persist the formset's fully validated final state."""

        if not self.is_valid():
            raise ValueError("Cannot save an invalid tie-breaker configuration.")

        if not commit:
            return super().save(commit=False)

        created_rows, deleted_rows = replace_competition_tie_breakers(
            self.instance,
            self._desired_configuration,
        )
        # Django Admin reads these to construct its ordinary change message.
        self.new_objects = created_rows
        self.changed_objects = []
        self.deleted_objects = deleted_rows
        return created_rows


class CompetitionTieBreakerInlineForm(forms.ModelForm):
    """Defer cross-row constraints to the inline formset's final state.

    ModelForm normally checks every database constraint against the currently
    saved rows.  That rejects a valid position swap before the formset can run
    its whole-configuration validation.  The model's field and ``clean``
    validation still run here; the formset checks uniqueness across the final
    rows, and the database remains the final safety net.
    """

    class Meta:
        model = CompetitionTieBreaker
        fields = ("order", "criterion")

    def _post_clean(self):
        opts = self._meta
        exclude = self._get_validation_exclusions()

        for name, field in self.fields.items():
            if isinstance(field, InlineForeignKeyField):
                exclude.add(name)

        try:
            self.instance = construct_instance(
                self,
                self.instance,
                opts.fields,
                opts.exclude,
            )
        except ValidationError as error:
            self._update_errors(error)

        try:
            self.instance.full_clean(
                exclude=exclude,
                validate_unique=False,
                validate_constraints=False,
            )
        except ValidationError as error:
            self._update_errors(error)

        if self._validate_unique:
            self.validate_unique()


class StageInline(admin.TabularInline):
    model = Stage
    extra = 0
    readonly_fields = (
        "name",
        "stage_type",
        "order",
    )

    can_delete = False

    def has_add_permission(self, request, obj=None):
        # Required stages are created automatically from the format.
        return False


class CompetitionTieBreakerInline(admin.TabularInline):
    model = CompetitionTieBreaker
    form = CompetitionTieBreakerInlineForm
    formset = CompetitionTieBreakerInlineFormSet
    extra = 0
    min_num = 1
    validate_min = True
    fields = ("order", "criterion")
    ordering = ("order", "id")
    verbose_name = "Standings tie-breaker"
    verbose_name_plural = (
        "Standings tie-breakers (Points is fixed at position 1)"
    )

    def get_extra(self, request, obj=None, **kwargs):
        # A new competition opens with the existing Points, GD, GF behavior.
        if obj is None or not obj.tie_breakers.exists():
            return len(CompetitionTieBreakerInlineFormSet.DEFAULT_ROWS)
        return 0


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    form = CompetitionAdminForm

    list_display = (
        "name",
        "display_order",
        "season",
        "format_type",
        "knockout_size",
        "predictions_enabled",
        "slug",
    )

    readonly_fields = (
        "knockout_size",
    )

    list_editable = ("display_order",)

    search_fields = (
        "name",
    )

    prepopulated_fields = {
        "slug": ("name",),
    }

    list_filter = (
        "season",
        "format_type",
        "predictions_enabled",
    )

    inlines = [
        StageInline,
        CompetitionTieBreakerInline,
    ]

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        previous_format = None

        if change and obj.pk:
            previous_format = (
                Competition.objects
                .filter(pk=obj.pk)
                .values_list(
                    "format_type",
                    flat=True,
                )
                .first()
            )

        # The normal post-save signal provisions defaults for direct ORM
        # creation. Defer that work for the admin add view so the validated
        # inline rows are saved as the competition's first configuration.
        if not change:
            obj._skip_default_tie_breaker_provisioning = True

        try:
            super().save_model(
                request,
                obj,
                form,
                change,
            )
        finally:
            if not change:
                delattr(obj, "_skip_default_tie_breaker_provisioning")

        if not change:
            # Newly created competition.
            ensure_competition_structure(obj)

        elif previous_format != obj.format_type:
            # Existing empty competition whose format was changed.
            rebuild_competition_structure(obj)

        else:
            # Ordinary edit. Also repairs any accidentally missing stage.
            ensure_competition_structure(obj)

        obj.series.league_type = form.cleaned_data["league_type"]
        obj.series.region = form.cleaned_data["region"]
        obj.series.full_clean()
        obj.series.save(update_fields=["league_type", "region"])

    @transaction.atomic
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # This only fills an empty configuration. In the normal add flow the
        # ordered inline has already saved the administrator's selections.
        ensure_default_tie_breakers(form.instance)

@admin.register(HomepageBanner)
class HomepageBannerAdmin(admin.ModelAdmin):
    list_display = ("title", "sponsor_name", "is_active", "order", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "subtitle", "sponsor_name")
    list_editable = ("is_active", "order")
    ordering = ("order", "-created_at")

    fieldsets = (
        ("Banner Content", {
            "fields": ("title", "subtitle", "image")
        }),
        ("Sponsor Details", {
            "fields": ("sponsor_name", "sponsor_logo")
        }),
        ("Call To Action", {
            "fields": ("button_text", "button_link")
        }),
        ("Display Control", {
            "fields": ("is_active", "order")
        }),
    )

@admin.register(SiteIdentity)
class SiteIdentityAdmin(admin.ModelAdmin):
    list_display = ("site_name", "is_active")
    list_editable = ("is_active",)

@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    list_display = ("name", "competition", "stage_type", "order")
    list_filter = ("competition", "stage_type")
    search_fields = ("name", "competition__name")
    ordering = ("competition", "order")

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.competition.schedule_locked_at:
            return ("competition", "name", "stage_type", "order")
        return ()

    def has_delete_permission(self, request, obj=None):
        if obj and obj.competition.schedule_locked_at:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "stage", "competition_name")
    list_filter = ("stage", "stage__competition")
    search_fields = ("name", "stage__name", "stage__competition__name")
    filter_horizontal = ("teams",)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.stage.competition.schedule_locked_at:
            return ("stage", "name", "teams")
        return ()

    def has_delete_permission(self, request, obj=None):
        if obj and obj.stage.competition.schedule_locked_at:
            return False
        return super().has_delete_permission(request, obj)

    def competition_name(self, obj):
        return obj.stage.competition.name
    competition_name.short_description = "Competition"


@admin.register(Standing)
class StandingAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "competition",
        "stage",
        "group",

        "baseline_played",
        "baseline_won",
        "baseline_drawn",
        "baseline_lost",
        "baseline_goals_for",
        "baseline_goals_against",
        "baseline_points",

        "played",
        "won",
        "drawn",
        "lost",
        "goals_for",
        "goals_against",
        "points",

        "total_played_display",
        "total_goal_difference_display",
        "total_points_display",
    )

    list_filter = ("competition", "stage", "group")
    search_fields = ("team__name", "competition__name")
    ordering = ("competition", "stage", "group", "-baseline_points", "-points")

    list_editable = (
        "baseline_played",
        "baseline_won",
        "baseline_drawn",
        "baseline_lost",
        "baseline_goals_for",
        "baseline_goals_against",
        "baseline_points",
    )

    readonly_fields = (
        "played",
        "won",
        "drawn",
        "lost",
        "goals_for",
        "goals_against",
        "points",
        "total_played_display",
        "total_won_display",
        "total_drawn_display",
        "total_lost_display",
        "total_goals_for_display",
        "total_goals_against_display",
        "total_goal_difference_display",
        "total_points_display",
    )

    fieldsets = (
        ("Standing Info", {
            "fields": ("competition", "stage", "group", "team")
        }),
        ("Baseline / Starting Log", {
            "fields": (
                "baseline_played",
                "baseline_won",
                "baseline_drawn",
                "baseline_lost",
                "baseline_goals_for",
                "baseline_goals_against",
                "baseline_points",
            )
        }),
        ("Auto Calculated From Matches", {
            "fields": (
                "played",
                "won",
                "drawn",
                "lost",
                "goals_for",
                "goals_against",
                "points",
            )
        }),
        ("Final Displayed Totals", {
            "fields": (
                "total_played_display",
                "total_won_display",
                "total_drawn_display",
                "total_lost_display",
                "total_goals_for_display",
                "total_goals_against_display",
                "total_goal_difference_display",
                "total_points_display",
            )
        }),
    )

    def total_played_display(self, obj):
        return obj.total_played

    def total_won_display(self, obj):
        return obj.total_won

    def total_drawn_display(self, obj):
        return obj.total_drawn

    def total_lost_display(self, obj):
        return obj.total_lost

    def total_goals_for_display(self, obj):
        return obj.total_goals_for

    def total_goals_against_display(self, obj):
        return obj.total_goals_against

    def total_goal_difference_display(self, obj):
        return obj.total_goal_difference

    def total_points_display(self, obj):
        return obj.total_points

    total_played_display.short_description = "Total P"
    total_won_display.short_description = "Total W"
    total_drawn_display.short_description = "Total D"
    total_lost_display.short_description = "Total L"
    total_goals_for_display.short_description = "Total GF"
    total_goals_against_display.short_description = "Total GA"
    total_goal_difference_display.short_description = "Total GD"
    total_points_display.short_description = "Total Pts"
    
    def goal_difference_display(self, obj):
        return obj.goals_for - obj.goals_against
    goal_difference_display.short_description = "GD"

