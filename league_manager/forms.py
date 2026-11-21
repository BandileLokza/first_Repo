from io import BytesIO

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, ImageOps

from leagues.models import ReportCommitteeMember, ReportDocumentSettings
from leagues.validators import validate_fixture_letterhead, validate_report_template

from matches.models import (
    AbandonedMatchDecision,
    FootballKnockoutRoundRule,
    FootballKnockoutTie,
    FootballKnockoutTieSlot,
    Player,
    Match,
)
from matches.knockout import (
    assign_match_to_knockout_tie,
    get_round_rule,
    validate_fixture_knockout_config,
)
from teams.models import Team
from leagues.models import Competition, CompetitionSeries, Group, Region, Stage, Standing
from django.contrib.auth import get_user_model
from django.db.models import Q

from league_manager.models import LeagueMembership, OrganisationAdministrator, RegionalAdministrator

User = get_user_model()


class AbandonedMatchResolutionForm(forms.Form):
    """One validated input surface for every official abandonment outcome."""

    decision_type = forms.ChoiceField(
        choices=AbandonedMatchDecision.DecisionType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    additional_note = forms.CharField(
        required=False,
        label="Additional note (optional)",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional context for this official decision",
            }
        ),
        help_text="Leave blank to use the default description automatically.",
    )
    scheduled_for = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={"class": "form-control", "type": "datetime-local"},
        ),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    venue = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    resume_minute = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    official_home_score = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    official_away_score = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )

    def __init__(self, *args, match, **kwargs):
        self.match = match
        super().__init__(*args, **kwargs)
        self.fields["resume_minute"].initial = match.abandoned_minute
        self.fields["venue"].initial = match.location
        if match.stage.stage_type == "KNOCKOUT":
            self.fields["decision_type"].choices = [
                (value, label) for value, label in AbandonedMatchDecision.DecisionType.choices
                if value == AbandonedMatchDecision.DecisionType.RESUME
            ]

    def clean(self):
        cleaned = super().clean()
        decision_type = cleaned.get("decision_type")
        if not decision_type:
            return cleaned
        if decision_type == AbandonedMatchDecision.DecisionType.REPLAY:
            if not cleaned.get("scheduled_for"):
                self.add_error("scheduled_for", "Set the replay date and time.")
        elif decision_type == AbandonedMatchDecision.DecisionType.RESUME:
            if cleaned.get("resume_minute") is None:
                self.add_error("resume_minute", "Set the minute from which play resumes.")
            if cleaned.get("scheduled_for") and cleaned["scheduled_for"] > timezone.now():
                self.add_error(
                    "scheduled_for",
                    "Resuming starts play immediately. Return at the scheduled time to resume this match.",
                )
        elif decision_type == AbandonedMatchDecision.DecisionType.AWARD:
            if cleaned.get("official_home_score") is None:
                self.add_error("official_home_score", "Enter the official home score.")
            if cleaned.get("official_away_score") is None:
                self.add_error("official_away_score", "Enter the official away score.")
        return cleaned


class LeagueRegionForm(forms.Form):
    league_type = forms.ChoiceField(
        label="Football Section",
        choices=CompetitionSeries.LeagueType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    region = forms.ModelChoiceField(
        queryset=Region.objects.filter(is_active=True).order_by("display_order", "name"),
        required=False,
        empty_label="No region assigned",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

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



class FixtureLetterheadForm(forms.Form):
    report_template = forms.FileField(
        label="Word report template (.docx)",
        required=False,
        validators=[validate_report_template],
        widget=forms.ClearableFileInput(
            attrs={
                "class": "form-control",
                "accept": ".docx",
            }
        ),
        help_text=(
            "Upload the official Word document template. "
            "Place association stationery in Word's page Header. "
            "This template is used for Word fixture and result exports "
            "across all seasons. Sample body content is replaced with "
            "live report data. Clearing the template restores the "
            "standard Word layout or image fallback."
        ),
    )

    fixture_letterhead = forms.ImageField(
        label="PDF letterhead image (PNG or JPEG)",
        required=False,
        validators=[validate_fixture_letterhead],
        widget=forms.ClearableFileInput(
            attrs={
                "class": "form-control",
                "accept": "image/png,image/jpeg",
            }
        ),
        help_text=(
            "Upload the official letterhead as a wide PNG or JPEG image. "
            "It is used at the top of PDF fixture and result exports, "
            "even when a Word template exists. "
            "For best results, use a high-resolution horizontal strip "
            "with clear logos and adequate spacing. "
            "The image is resized proportionally without cropping. "
            "If no image is uploaded, PDFs use the standard layout."
        ),
    )


def _normalize_signing_artwork(upload, size):
    if upload.size > 2 * 1024 * 1024:
        raise ValidationError("Upload an image no larger than 2 MB.")
    try:
        with Image.open(upload) as source:
            if source.format not in {"PNG", "JPEG"} or source.width * source.height > 4_000_000:
                raise ValidationError("Use a PNG or JPEG image of up to 4 megapixels.")
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValidationError("Upload a valid PNG or JPEG image.") from error
    return output.getvalue()


class ReportDocumentSettingsForm(forms.ModelForm):
    signature_upload = forms.FileField(label="Signature image", required=False)
    stamp_upload = forms.FileField(label="Official stamp image", required=False)
    remove_signature = forms.BooleanField(label="Remove saved signature image", required=False)
    remove_stamp = forms.BooleanField(label="Remove saved stamp image", required=False)

    class Meta:
        model = ReportDocumentSettings
        fields = ("signoff_enabled", "signoff_heading", "signatory_name", "signatory_position", "committee_enabled")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"
        for name in ("signature_upload", "stamp_upload"):
            self.fields[name].widget.attrs["accept"] = "image/png,image/jpeg"

    def clean(self):
        cleaned = super().clean()
        for kind, size in (("signature", (640, 240)), ("stamp", (360, 360))):
            field = f"{kind}_upload"
            upload = cleaned.get(field)
            if upload:
                if cleaned.get(f"remove_{kind}"):
                    self.add_error(field, "Choose either to replace or remove the saved image.")
                    continue
                try:
                    cleaned[field] = _normalize_signing_artwork(upload, size)
                except ValidationError as error:
                    self.add_error(field, error)
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        for kind in ("signature", "stamp"):
            upload = self.cleaned_data[f"{kind}_upload"]
            if upload or self.cleaned_data[f"remove_{kind}"]:
                setattr(instance, f"{kind}_image", upload or None)
        if commit:
            instance.save()
        return instance


ReportCommitteeFormSet = forms.inlineformset_factory(
    ReportDocumentSettings, ReportCommitteeMember,
    fields=("position", "name", "contact", "display_order"), extra=1, max_num=100,
    validate_max=True, absolute_max=110,
    widgets={
        "position": forms.TextInput(attrs={"class": "form-control"}),
        "name": forms.TextInput(attrs={"class": "form-control"}),
        "contact": forms.TextInput(attrs={"class": "form-control", "type": "tel"}),
        "display_order": forms.NumberInput(attrs={"class": "form-control"}),
    },
)


class LeagueBrandingForm(forms.Form):
    """Owner-managed permanent league and current-season branding."""

    league_logo = forms.ImageField(
        label="League logo",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
    )
    sponsor_name = forms.CharField(
        label="Sponsor name",
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    sponsor_logo = forms.ImageField(
        label="Sponsor logo",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
    )


class NextCompetitionSeasonForm(forms.Form):
    """Minimal superuser input for a clean next football league season."""

    season = forms.CharField(
        label="New season label",
        max_length=20,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Example: 2027 or 2026/27",
                "autocomplete": "off",
            }
        ),
    )
    slug = forms.SlugField(
        label="New competition slug",
        max_length=50,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "example-2027",
                "autocomplete": "off",
            }
        ),
        help_text="This must be unique across football competitions.",
    )

    def clean_season(self):
        return self.cleaned_data["season"].strip()


class TeamForm(forms.ModelForm):
    group = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        required=False,
        empty_label="Select a group",
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
    )

    class Meta:
        model = Team
        fields = [
            "name",
            "home_ground",
            "logo",
        ]

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Enter team name",
                    "autocomplete": "off",
                }
            ),
            "logo": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                }
            ),
            "home_ground": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Optional home ground or venue",
                    "autocomplete": "off",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

        self.group_stage = (
            Stage.objects
            .filter(
                competition=self.league,
                stage_type="GROUP",
            )
            .first()
        )

        if self.league.format_type == "GROUP_KNOCKOUT":
            groups = (
                Group.objects
                .filter(stage=self.group_stage)
                .order_by("name")
                if self.group_stage
                else Group.objects.none()
            )

            self.fields["group"].queryset = groups
            self.fields["group"].required = True

            if self.instance.pk:
                current_group = (
                    groups
                    .filter(teams=self.instance)
                    .first()
                )

                self.fields["group"].initial = current_group

        else:
            self.fields.pop("group")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        duplicate = Team.objects.filter(
            competition=self.league,
            name__iexact=name,
        )

        if self.instance.pk:
            duplicate = duplicate.exclude(
                pk=self.instance.pk
            )

        if duplicate.exists():
            raise ValidationError(
                "A team with this name already exists in this competition."
            )

        return name

    def clean(self):
        cleaned_data = super().clean()

        if self.league.format_type != "GROUP_KNOCKOUT":
            return cleaned_data

        selected_group = cleaned_data.get("group")

        if not self.group_stage:
            self.add_error(
                "group",
                (
                    "The Group Stage has not been created. "
                    "Ask the superuser to check the competition format."
                ),
            )

            return cleaned_data

        if not selected_group:
            self.add_error(
                "group",
                "Select the group this team belongs to.",
            )

            return cleaned_data

        if selected_group.stage_id != self.group_stage.id:
            self.add_error(
                "group",
                "The selected group does not belong to this competition.",
            )

        if self.instance.pk:
            current_group = (
                Group.objects
                .filter(
                    stage=self.group_stage,
                    teams=self.instance,
                )
                .first()
            )

            group_changed = (
                not current_group
                or current_group.id != selected_group.id
            )

            if group_changed:
                has_group_matches = (
                    Match.objects
                    .filter(
                        competition=self.league,
                        stage=self.group_stage,
                    )
                    .filter(
                        Q(home_team=self.instance)
                        | Q(away_team=self.instance)
                    )
                    .exists()
                )

                has_group_standing = Standing.objects.filter(
                    competition=self.league,
                    stage=self.group_stage,
                    team=self.instance,
                ).exists()

                if has_group_matches or has_group_standing:
                    self.add_error(
                        "group",
                        (
                            "This team cannot be moved to another group "
                            "because it already has group-stage matches "
                            "or standings."
                        ),
                    )

        return cleaned_data

    def save_group_assignment(self, team):
        if self.league.format_type != "GROUP_KNOCKOUT":
            return

        selected_group = self.cleaned_data.get("group")

        if not selected_group:
            return

        current_groups = Group.objects.filter(
            stage=self.group_stage,
            teams=team,
        )

        for current_group in current_groups.exclude(
            pk=selected_group.pk
        ):
            current_group.teams.remove(team)

        selected_group.teams.add(team)

class PlayerForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = [
            "name",
            "photo",
            "team",
            "jersey_number",
            "starting_goals",
        ]

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Enter player name",
                    "autocomplete": "off",
                }
            ),
            "photo": forms.ClearableFileInput(
                attrs={"class": "form-control"}
            ),
            "team": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "jersey_number": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": 0,
                    "placeholder": "Optional",
                }
            ),
            "starting_goals": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": 0,
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league", None)
        super().__init__(*args, **kwargs)

        if self.league:
            self.fields["team"].queryset = Team.objects.filter(
                competition=self.league
            ).order_by("name")
        else:
            self.fields["team"].queryset = Team.objects.none()

        self.fields["jersey_number"].required = False
        self.fields["photo"].required = False
        self.fields["photo"].label = "Player Photo"
        self.fields["photo"].help_text = (
            "Optional. You can add, replace or clear this photo later."
        )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not name:
            raise ValidationError("Player name is required.")

        return name

    def clean(self):
        cleaned_data = super().clean()

        team = cleaned_data.get("team")
        name = cleaned_data.get("name")
        jersey_number = cleaned_data.get("jersey_number")

        if team and self.league:
            if team.competition_id != self.league.id:
                raise ValidationError(
                    "The selected team does not belong to this league."
                )

        if team and name:
            duplicate_players = Player.objects.filter(
                team=team,
                name__iexact=name,
            )

            if self.instance.pk:
                duplicate_players = duplicate_players.exclude(
                    pk=self.instance.pk
                )

            if duplicate_players.exists():
                self.add_error(
                    "name",
                    "This player is already registered for the selected team.",
                )

        if team and jersey_number is not None:
            jersey_users = Player.objects.filter(
                team=team,
                jersey_number=jersey_number,
            )

            if self.instance.pk:
                jersey_users = jersey_users.exclude(
                    pk=self.instance.pk
                )

            if jersey_users.exists():
                self.add_error(
                    "jersey_number",
                    "This jersey number is already used by another player on this team.",
                )

        return cleaned_data
    
class FixtureForm(forms.ModelForm):
    class Meta:
        model = Match
        fields = [
            "stage",
            "group",
            "home_team",
            "away_team",
            "gameweek",
            "match_date",
            "location",
            "leg",
        ]

        widgets = {
            "stage": forms.Select(
                attrs={"class": "form-select"}
            ),
            "group": forms.Select(
                attrs={"class": "form-select"}
            ),
            "home_team": forms.Select(
                attrs={"class": "form-select"}
            ),
            "away_team": forms.Select(
                attrs={"class": "form-select"}
            ),
            "gameweek": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
            "match_date": forms.DateTimeInput(
                attrs={
                    "type": "datetime-local",
                    "class": "form-control",
                },
                format="%Y-%m-%dT%H:%M",
            ),
            "location": forms.TextInput(
                attrs={"class": "form-control"}
            ),
            "leg": forms.NumberInput(
                attrs={
                    "class": "form-control",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

        self.instance.competition = self.league
        self.generated_fixture_locked = bool(
            self.instance.pk and self.instance.generated_by_system
        )
        self.schedule_fixture_locked = bool(
            self.instance.pk and self.league.schedule_locked_at
        )
        self.fixture_structure_locked = (
            self.generated_fixture_locked or self.schedule_fixture_locked
        )

        self.fields["location"].required = False
        self.fields["group"].required = False
        self.fields["match_date"].required = False

        self.fields["match_date"].input_formats = [
            "%Y-%m-%dT%H:%M",
        ]

        if self.league.format_type == "TABLE":
            stage_queryset = Stage.objects.filter(
                competition=self.league,
                stage_type="LEAGUE",
            )

        elif self.league.format_type == "GROUP_KNOCKOUT":
            stage_queryset = Stage.objects.filter(
                competition=self.league,
                stage_type__in=[
                    "GROUP",
                    "KNOCKOUT",
                ],
            )

        else:
            stage_queryset = Stage.objects.filter(
                competition=self.league,
                stage_type="KNOCKOUT",
            )

        stage_queryset = stage_queryset.order_by(
            "order",
            "name",
        )

        self.fields["stage"].queryset = stage_queryset

        if stage_queryset.count() == 1:
            only_stage = stage_queryset.first()

            self.fields["stage"].initial = only_stage
            self.fields["stage"].disabled = True

        teams = Team.objects.filter(
            competition=self.league
        ).order_by("name")

        self.fields["home_team"].queryset = teams
        self.fields["away_team"].queryset = teams

        if self.league.format_type == "GROUP_KNOCKOUT":
            self.fields["group"].queryset = (
                Group.objects
                .filter(
                    stage__competition=self.league,
                    stage__stage_type="GROUP",
                )
                .select_related("stage")
                .order_by("name")
            )
        else:
            self.fields.pop("group")

        if self.fixture_structure_locked:
            # The model repeats this protection for crafted submissions and
            # non-form callers. Disabled fields make the permitted schedule
            # edit obvious in the manager UI.
            for name in (
                "stage",
                "group",
                "home_team",
                "away_team",
                "gameweek",
                "leg",
            ):
                if name in self.fields:
                    self.fields[name].disabled = True

    def _reject_forged_locked_fixture_fields(self):
        """Reject values injected for fields the browser correctly disables."""

        field_to_attribute = {
            "stage": "stage_id",
            "group": "group_id",
            "home_team": "home_team_id",
            "away_team": "away_team_id",
            "gameweek": "gameweek",
            "leg": "leg",
        }
        for name, attribute in field_to_attribute.items():
            if name not in self.fields:
                continue
            submitted = self.data.get(self.add_prefix(name))
            # Disabled HTML controls are omitted from a legitimate browser
            # submission. A supplied value is therefore intentional input and
            # must agree exactly with the stored generated fixture.
            if submitted is None:
                continue
            expected = getattr(self.instance, attribute)
            if submitted != ("" if expected is None else str(expected)):
                message = (
                    "This generated fixture field is locked and cannot be changed."
                    if self.generated_fixture_locked
                    else "This competition's published schedule locks this field."
                )
                self.add_error(name, message)

    def clean(self):
        cleaned_data = super().clean()

        if self.fixture_structure_locked:
            self._reject_forged_locked_fixture_fields()

        stage = cleaned_data.get("stage")
        group = cleaned_data.get("group")
        home_team = cleaned_data.get("home_team")
        away_team = cleaned_data.get("away_team")
        leg = cleaned_data.get("leg")

        if home_team and away_team and home_team == away_team:
            self.add_error(
                "away_team",
                "A team cannot play against itself.",
            )

        # Manual fixtures retain complete control over the venue.  For a new
        # fixture only, an omitted venue gets the selected home team's optional
        # default; an explicit neutral/override value always wins.
        if (
            not self.instance.pk
            and home_team
            and not cleaned_data.get("location")
            and home_team.home_ground
        ):
            cleaned_data["location"] = home_team.home_ground

        if stage and stage.competition_id != self.league.id:
            self.add_error(
                "stage",
                "The selected stage does not belong to this competition.",
            )

        if home_team and home_team.competition_id != self.league.id:
            self.add_error(
                "home_team",
                "The home team does not belong to this competition.",
            )

        if away_team and away_team.competition_id != self.league.id:
            self.add_error(
                "away_team",
                "The away team does not belong to this competition.",
            )

        allowed_stage_types = {
            "TABLE": {"LEAGUE"},
            "GROUP_KNOCKOUT": {
                "GROUP",
                "KNOCKOUT",
            },
            "KNOCKOUT": {"KNOCKOUT"},
        }

        if (
            stage
            and stage.stage_type
            not in allowed_stage_types.get(
                self.league.format_type,
                set(),
            )
        ):
            self.add_error(
                "stage",
                "This stage is not allowed for this competition format.",
            )

        if stage and stage.stage_type == "GROUP":
            if not group:
                self.add_error(
                    "group",
                    "Select the group for this group-stage fixture.",
                )

            elif group.stage_id != stage.id:
                self.add_error(
                    "group",
                    "The selected group does not belong to this stage.",
                )

            else:
                if (
                    home_team
                    and not group.teams.filter(
                        pk=home_team.pk
                    ).exists()
                ):
                    self.add_error(
                        "home_team",
                        (
                            f"{home_team.name} does not belong "
                            f"to {group.name}."
                        ),
                    )

                if (
                    away_team
                    and not group.teams.filter(
                        pk=away_team.pk
                    ).exists()
                ):
                    self.add_error(
                        "away_team",
                        (
                            f"{away_team.name} does not belong "
                            f"to {group.name}."
                        ),
                    )

        else:
            cleaned_data["group"] = None
            self.instance.group = None

        if (
            stage
            and home_team
            and away_team
            and leg is not None
        ):
            duplicate = Match.objects.filter(
                competition=self.league,
                stage=stage,
                home_team=home_team,
                away_team=away_team,
                leg=leg,
            )

            if self.instance.pk:
                duplicate = duplicate.exclude(
                    pk=self.instance.pk
                )

            if duplicate.exists():
                raise ValidationError(
                    "This fixture already exists in this stage and leg."
                )

            candidate = Match(
                pk=self.instance.pk,
                competition=self.league,
                stage=stage,
                group=cleaned_data.get("group"),
                home_team=home_team,
                away_team=away_team,
                leg=leg,
                knockout_tie=self.instance.knockout_tie,
            )
            try:
                validate_fixture_knockout_config(candidate)
            except ValidationError as error:
                raise ValidationError(error)

        return cleaned_data

    def save(self, commit=True):
        match = super().save(commit=False)
        assign_match_to_knockout_tie(match)
        if commit:
            match.save()
            self.save_m2m()
        return match


class ScheduleUnlockForm(forms.Form):
    reason = forms.CharField(
        label="Reason for unlocking",
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Explain why fixture structure must be reopened.",
            }
        ),
    )

    def clean_reason(self):
        return self.cleaned_data["reason"].strip()


class BracketSeedingForm(forms.Form):
    """Dynamic first-round slot form; an empty slot is an explicit bye."""

    def __init__(self, *args, competition, slots, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition = competition
        self.slots = list(slots)
        teams = Team.objects.filter(competition=competition).order_by("name")
        for slot in self.slots:
            field_name = self.slot_field_name(slot)
            self.fields[field_name] = forms.ModelChoiceField(
                queryset=teams,
                required=False,
                empty_label="Bye",
                initial=slot.team_id,
                label=(
                    f"{slot.tie.stage.name} tie {slot.tie.position} "
                    f"{slot.get_slot_display()}"
                ),
                widget=forms.Select(attrs={"class": "form-select"}),
            )

    @staticmethod
    def slot_field_name(slot):
        return f"slot_{slot.id}"

    def clean(self):
        cleaned_data = super().clean()
        chosen_team_ids = []
        slots_by_tie = {}
        for slot in self.slots:
            team = cleaned_data.get(self.slot_field_name(slot))
            if team:
                chosen_team_ids.append(team.id)
            slots_by_tie.setdefault(slot.tie_id, []).append((slot, team))

        if len(chosen_team_ids) != len(set(chosen_team_ids)):
            raise ValidationError("A team can only be seeded once in the first round.")

        for tie_slots in slots_by_tie.values():
            if not any(team for _, team in tie_slots):
                raise ValidationError("A first-round tie cannot contain two byes.")

        return cleaned_data

    def selections(self):
        return {
            slot.id: self.cleaned_data.get(self.slot_field_name(slot))
            for slot in self.slots
        }


class BracketRandomDrawForm(forms.Form):
    teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.none(),
        required=True,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, competition, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teams"].queryset = Team.objects.filter(
            competition=competition
        ).order_by("name")
        self.fields["teams"].label = "Teams entering the draw"


class BracketScheduleForm(forms.Form):
    """Collect real kickoffs for every tie before bracket fixtures exist."""

    def __init__(self, *args, ties, **kwargs):
        super().__init__(*args, **kwargs)
        self.ties = list(ties)
        self.rules_by_tie_id = {}
        for tie in self.ties:
            rule = get_round_rule(tie.stage)
            self.rules_by_tie_id[tie.id] = rule
            self.fields[self.field_name(tie, 1)] = forms.DateTimeField(
                required=True,
                initial=tie.leg_one_scheduled_for,
                label=f"{tie.stage.name} tie {tie.position} â€” leg 1 kickoff",
                input_formats=["%Y-%m-%dT%H:%M"],
                widget=forms.DateTimeInput(
                    format="%Y-%m-%dT%H:%M",
                    attrs={"type": "datetime-local", "class": "form-control"},
                ),
            )
            if rule.number_of_legs == FootballKnockoutRoundRule.Legs.TWO:
                self.fields[self.field_name(tie, 2)] = forms.DateTimeField(
                    required=True,
                    initial=tie.leg_two_scheduled_for,
                    label=(
                        f"{tie.stage.name} tie {tie.position} â€” leg 2 kickoff"
                    ),
                    input_formats=["%Y-%m-%dT%H:%M"],
                    widget=forms.DateTimeInput(
                        format="%Y-%m-%dT%H:%M",
                        attrs={"type": "datetime-local", "class": "form-control"},
                    ),
                )

    @staticmethod
    def field_name(tie, leg):
        return f"tie_{tie.id}_leg_{leg}_scheduled_for"

    def clean(self):
        cleaned_data = super().clean()
        schedules = {}
        for tie in self.ties:
            leg_one = cleaned_data.get(self.field_name(tie, 1))
            rule = self.rules_by_tie_id[tie.id]
            values = {1: leg_one}
            if rule.number_of_legs == FootballKnockoutRoundRule.Legs.TWO:
                leg_two = cleaned_data.get(self.field_name(tie, 2))
                if leg_one and leg_two and leg_two <= leg_one:
                    self.add_error(
                        self.field_name(tie, 2),
                        "The second leg must be after the first leg.",
                    )
                values[2] = leg_two
            schedules[tie.id] = values
        self.cleaned_schedules = schedules
        return cleaned_data


class BracketLinkForm(forms.Form):
    """Dynamic, server-validated explicit source-to-destination graph form."""

    def __init__(self, *args, ties, **kwargs):
        super().__init__(*args, **kwargs)
        self.ties = list(ties)
        self.ties_by_id = {tie.id: tie for tie in self.ties}
        for tie in self.ties:
            choices = [("", "Final / no destination")]
            for destination in self.ties:
                if destination.stage.order <= tie.stage.order:
                    continue
                choices.extend([
                    (
                        f"{destination.id}:{FootballKnockoutTie.DestinationSlot.HOME}",
                        f"{destination.stage.name} tie {destination.position} — home",
                    ),
                    (
                        f"{destination.id}:{FootballKnockoutTie.DestinationSlot.AWAY}",
                        f"{destination.stage.name} tie {destination.position} — away",
                    ),
                ])
            initial = ""
            if tie.winner_destination_id:
                initial = (
                    f"{tie.winner_destination_id}:"
                    f"{tie.winner_destination_slot}"
                )
            self.fields[self.tie_field_name(tie)] = forms.ChoiceField(
                choices=choices,
                required=False,
                initial=initial,
                label=f"{tie.stage.name} tie {tie.position}",
                widget=forms.Select(attrs={"class": "form-select"}),
            )

    @staticmethod
    def tie_field_name(tie):
        return f"tie_{tie.id}_destination"

    def clean(self):
        cleaned_data = super().clean()
        links = {}
        occupied_destination_slots = set()
        for tie in self.ties:
            value = cleaned_data.get(self.tie_field_name(tie))
            if not value:
                links[tie.id] = None
                continue
            destination_id_text, slot_name = value.split(":", 1)
            destination = self.ties_by_id.get(int(destination_id_text))
            if destination is None:
                raise ValidationError("A bracket link points outside this bracket.")
            key = (destination.id, slot_name)
            if key in occupied_destination_slots:
                raise ValidationError(
                    "Two ties cannot populate the same destination slot."
                )
            occupied_destination_slots.add(key)
            links[tie.id] = (destination, slot_name)
        self.cleaned_links = links
        return cleaned_data


class AgentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        full_name = user.get_full_name().strip()

        if full_name and user.email:
            return f"{full_name} — {user.email}"

        if user.email:
            return user.email

        return user.username


class TeamCoachRegistrationForm(forms.Form):
    identifier = forms.CharField(
        label="Coach Email or Username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter the coach's email address or username",
                "autocomplete": "off",
            }
        ),
        help_text="The coach must already have an active EsportsLive account.",
    )

    def __init__(self, *args, **kwargs):
        self.team = kwargs.pop("team")
        super().__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()
        users = User.objects.filter(is_active=True).filter(
            Q(email__iexact=identifier)
            | Q(username__iexact=identifier)
        ).distinct()

        if not users.exists():
            raise ValidationError(
                "No active EsportsLive account was found with this email or username."
            )

        if users.count() > 1:
            raise ValidationError(
                "More than one account uses this email. Enter the exact username instead."
            )

        self.cleaned_data["user"] = users.first()
        return identifier


class AgentRegistrationForm(forms.Form):
    identifier = forms.CharField(
        label="Agent Email or Username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter the agent's email address",
                "autocomplete": "off",
            }
        ),
        help_text=(
            "The agent must already have an EsportsLive account. "
            "On the deployed site, ask them to sign in with Google once."
        ),
    )

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()

        users = User.objects.filter(
            is_active=True,
        ).filter(
            Q(email__iexact=identifier)
            | Q(username__iexact=identifier)
        ).distinct()

        if not users.exists():
            raise ValidationError(
                "No active EsportsLive account was found with this email or username."
            )

        if users.count() > 1:
            raise ValidationError(
                "More than one account uses this email. Enter the exact username instead."
            )

        user = users.first()

        if user.is_superuser:
            raise ValidationError(
                "A platform superuser does not need an agent membership."
            )

        existing_membership = LeagueMembership.objects.filter(
            user=user,
            competition=self.league,
        ).first()

        if existing_membership:
            if existing_membership.role in {
                LeagueMembership.Role.OWNER,
                LeagueMembership.Role.ADMIN,
            }:
                raise ValidationError(
                    "This user already manages the league and does not need an agent role."
                )

            if (
                existing_membership.role == LeagueMembership.Role.AGENT
                and existing_membership.is_active
            ):
                raise ValidationError(
                    "This user is already an active match agent for this league."
                )

        self.cleaned_data["user"] = user

        return identifier


class AgentAssignmentForm(forms.Form):
    agent = AgentChoiceField(
        queryset=User.objects.none(),
        label="Match Agent",
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

        self.fields["agent"].queryset = (
            User.objects
            .filter(
                league_memberships__competition=self.league,
                league_memberships__role=LeagueMembership.Role.AGENT,
                league_memberships__is_active=True,
                is_active=True,
            )
            .distinct()
            .order_by("first_name", "last_name", "email", "username")
        )

    def clean_agent(self):
        agent = self.cleaned_data["agent"]

        valid_membership = LeagueMembership.objects.filter(
            user=agent,
            competition=self.league,
            role=LeagueMembership.Role.AGENT,
            is_active=True,
        ).exists()

        if not valid_membership:
            raise ValidationError(
                "This user is not an active agent for this league."
            )

        return agent
    
class LeagueAdminRegistrationForm(forms.Form):
    identifier = forms.CharField(
        label="Administrator Email or Username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter email address or username",
                "autocomplete": "off",
            }
        ),
        help_text=(
            "The administrator must already have an EsportsLive account. "
            "On the deployed site, ask them to sign in with Google first."
        ),
    )

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()

        users = (
            User.objects
            .filter(is_active=True)
            .filter(
                Q(email__iexact=identifier)
                | Q(username__iexact=identifier)
            )
            .distinct()
        )

        if not users.exists():
            raise ValidationError(
                "No active EsportsLive account was found with this email or username."
            )

        if users.count() > 1:
            raise ValidationError(
                "More than one account uses this email. Enter the exact username instead."
            )

        user = users.first()

        if user.is_superuser:
            raise ValidationError(
                "An EsportsLive superuser does not need a league administrator membership."
            )

        existing_membership = LeagueMembership.objects.filter(
            user=user,
            competition=self.league,
        ).first()

        if existing_membership:
            if existing_membership.role == LeagueMembership.Role.OWNER:
                raise ValidationError(
                    "This user is already a league owner."
                )

            if (
                existing_membership.role == LeagueMembership.Role.ADMIN
                and existing_membership.is_active
            ):
                raise ValidationError(
                    "This user is already an active league administrator."
                )

            if existing_membership.role == LeagueMembership.Role.AGENT:
                raise ValidationError(
                    "This user is currently registered as a match agent. "
                    "A user cannot hold both agent and administrator roles in the same league."
                )

        self.cleaned_data["user"] = user

        return identifier


class RegionalLeagueOwnerForm(forms.Form):
    """Resolve a safe existing account for a region-scoped owner assignment."""

    identifier = forms.CharField(
        label="Owner email or username",
        max_length=254,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Enter email address or username",
            "autocomplete": "off",
        }),
    )

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()
        users = User.objects.filter(is_active=True).filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier)
        ).distinct()
        if not users.exists():
            raise ValidationError("No active EsportsLive account was found with this email or username.")
        if users.count() > 1:
            raise ValidationError("More than one account uses this email. Enter the exact username instead.")
        user = users.first()
        if user.is_superuser:
            raise ValidationError("A platform superuser does not need a league-owner assignment.")
        if RegionalAdministrator.objects.filter(user=user, is_active=True).exists():
            raise ValidationError("Regional Administrators cannot be appointed as League Owners.")
        if OrganisationAdministrator.objects.filter(user=user, is_active=True).exists():
            raise ValidationError("Organisation Administrators cannot be appointed as League Owners.")
        self.cleaned_data["user"] = user
        return identifier

class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = [
            "name",
            "teams",
        ]

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Example: Group A",
                    "autocomplete": "off",
                }
            ),
            "teams": forms.SelectMultiple(
                attrs={
                    "class": "form-select",
                    "size": 12,
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

        self.group_stage = (
            Stage.objects
            .filter(
                competition=self.league,
                stage_type="GROUP",
            )
            .first()
        )

        self.fields["teams"].queryset = (
            Team.objects
            .filter(competition=self.league)
            .order_by("name")
        )

        self.fields["teams"].required = False

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not self.group_stage:
            return name

        duplicate = Group.objects.filter(
            stage=self.group_stage,
            name__iexact=name,
        )

        if self.instance.pk:
            duplicate = duplicate.exclude(
                pk=self.instance.pk
            )

        if duplicate.exists():
            raise ValidationError(
                "A group with this name already exists."
            )

        return name

    def clean(self):
        cleaned_data = super().clean()

        selected_teams = cleaned_data.get("teams")

        if not self.group_stage:
            raise ValidationError(
                "The Group Stage is missing. Ask the EsportsLive "
                "superuser to check this competition's format."
            )

        if selected_teams is None:
            return cleaned_data

        conflicting_groups = (
            Group.objects
            .filter(
                stage=self.group_stage,
                teams__in=selected_teams,
            )
            .exclude(pk=self.instance.pk)
            .distinct()
        )

        if conflicting_groups.exists():
            conflict_names = ", ".join(
                conflicting_groups.values_list(
                    "name",
                    flat=True,
                )
            )

            self.add_error(
                "teams",
                (
                    "One or more selected teams already belong to "
                    f"another group: {conflict_names}."
                ),
            )

        if self.instance.pk:
            existing_team_ids = set(
                self.instance.teams.values_list(
                    "id",
                    flat=True,
                )
            )

            selected_team_ids = set(
                selected_teams.values_list(
                    "id",
                    flat=True,
                )
            )

            removed_team_ids = (
                existing_team_ids - selected_team_ids
            )

            if removed_team_ids:
                used_in_matches = (
                    Match.objects
                    .filter(
                        competition=self.league,
                        group=self.instance,
                    )
                    .filter(
                        Q(home_team_id__in=removed_team_ids)
                        | Q(away_team_id__in=removed_team_ids)
                    )
                    .exists()
                )

                used_in_standings = Standing.objects.filter(
                    competition=self.league,
                    group=self.instance,
                    team_id__in=removed_team_ids,
                ).exists()

                if used_in_matches or used_in_standings:
                    self.add_error(
                        "teams",
                        (
                            "A team cannot be removed from this group "
                            "because it already has matches or standings."
                        ),
                    )

        return cleaned_data   

class KnockoutRoundsForm(forms.Form):
    knockout_size = forms.TypedChoiceField(
        label="Teams Entering Knockout Stage",
        choices=Competition.KNOCKOUT_SIZE_CHOICES,
        coerce=int,
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
        help_text=(
            "This determines which knockout rounds will be generated."
        ),
    )

    def __init__(self, *args, **kwargs):
        self.league = kwargs.pop("league")
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()

        if self.league.format_type not in {
            "GROUP_KNOCKOUT",
            "KNOCKOUT",
        }:
            raise ValidationError(
                "This competition does not have a knockout section."
            )

        return cleaned_data


class KnockoutRoundRuleForm(forms.ModelForm):
    class Meta:
        model = FootballKnockoutRoundRule
        fields = ["number_of_legs", "decider_policy"]
        widgets = {
            "number_of_legs": forms.Select(attrs={"class": "form-select"}),
            "decider_policy": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.locked = locked
        if locked:
            self.fields["number_of_legs"].disabled = True
            self.fields["decider_policy"].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        if self.instance.stage_id and self.instance.stage.stage_type != "KNOCKOUT":
            raise ValidationError(
                "Football knockout rules require a knockout stage."
            )
        number_of_legs = cleaned_data.get("number_of_legs")
        if number_of_legs and self.instance.stage_id:
            highest_existing_leg = (
                Match.objects.filter(stage=self.instance.stage)
                .order_by("-leg")
                .values_list("leg", flat=True)
                .first()
                or 1
            )
            if highest_existing_leg > number_of_legs:
                self.add_error(
                    "number_of_legs",
                    "Existing fixtures use a higher leg number.",
                )
        return cleaned_data
