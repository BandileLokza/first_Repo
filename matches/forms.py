import re

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.forms.models import ModelChoiceIteratorValue
from django.db import transaction

import uuid

from .models import Goal, Match, Player
from .services import resolve_goal_scorer
from teams.models import Team
from leagues.models import Stage, Group
from .models import YellowCard
from .knockout import (
    assign_match_to_knockout_tie,
    validate_fixture_knockout_config,
    validate_official_result,
)


class TeamPlayerSelect(forms.Select):
    """Expose a roster entry's team to the small progressive-enhancement UI."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        player = getattr(value, "instance", None)
        if player is not None:
            option["attrs"]["data-team-id"] = str(player.team_id)
        return option


class TeamLineupForm(forms.Form):
    """Server-side validation for a team's submitted lineup."""

    starting_players = forms.ModelMultipleChoiceField(
        label="Starting XI",
        queryset=Player.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    substitute_players = forms.ModelMultipleChoiceField(
        label="Substitutes",
        queryset=Player.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, team, **kwargs):
        super().__init__(*args, **kwargs)
        squad = team.players.all().order_by("name", "id")
        self.team = team
        self.maximum_substitutes = getattr(
            settings,
            "TEAM_LINEUP_MAX_SUBSTITUTES",
            12,
        )
        self.fields["starting_players"].queryset = squad
        self.fields["substitute_players"].queryset = squad
        self.fields["substitute_players"].help_text = (
            f"Maximum {self.maximum_substitutes} substitutes."
        )

    def clean(self):
        cleaned_data = super().clean()
        starters = cleaned_data.get("starting_players")
        substitutes = cleaned_data.get("substitute_players")

        raw_starters = self.data.getlist("starting_players")
        raw_substitutes = self.data.getlist("substitute_players")
        if len(raw_starters) != len(set(raw_starters)):
            self.add_error(
                "starting_players",
                "Each player can only be selected once.",
            )
        if len(raw_substitutes) != len(set(raw_substitutes)):
            self.add_error(
                "substitute_players",
                "Each player can only be selected once.",
            )

        if starters is None or substitutes is None:
            return cleaned_data

        if len(starters) != 11:
            self.add_error(
                "starting_players",
                "Select exactly 11 starting players.",
            )

        if len(substitutes) > self.maximum_substitutes:
            self.add_error(
                "substitute_players",
                f"Select no more than {self.maximum_substitutes} substitutes.",
            )

        starter_ids = set(starters.values_list("id", flat=True))
        substitute_ids = set(substitutes.values_list("id", flat=True))
        if starter_ids & substitute_ids:
            raise ValidationError(
                "A player cannot be both a starter and a substitute."
            )

        return cleaned_data


class GoalEntryForm(forms.Form):
    """Validate a match goal without conflating own goals and shootouts."""

    allow_unattributed_own_goal = False

    goal_type = forms.ChoiceField(
        choices=Goal.GoalType.choices,
        initial=Goal.GoalType.NORMAL,
        required=False,
        widget=forms.RadioSelect,
    )
    player = forms.ModelChoiceField(
        queryset=Player.objects.none(),
        required=False,
        empty_label="-- Select player --",
        widget=TeamPlayerSelect(attrs={"class": "form-select"}),
    )
    player_name = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Type player name",
                "autocomplete": "off",
            }
        ),
    )
    new_player_requested = forms.CharField(required=False, widget=forms.HiddenInput)
    minute = forms.IntegerField(
        required=False,
        min_value=0,
        label="Goal Minute (optional)",
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": 0,
            "inputmode": "numeric",
        }),
    )
    idempotency_key = forms.UUIDField(required=True, widget=forms.HiddenInput)

    def __init__(self, *args, match, benefiting_team, goal=None, **kwargs):
        self.match = match
        self.benefiting_team = benefiting_team
        self.opposing_team = (
            match.away_team
            if benefiting_team.id == match.home_team_id
            else match.home_team
        )
        if goal and "initial" not in kwargs:
            kwargs["initial"] = {
                "goal_type": goal.goal_type,
                "player": goal.player_id,
                "player_name": "" if goal.player_id else goal.player_name,
                "minute": goal.minute,
            }
        initial = kwargs.get("initial") or {}
        kwargs["initial"] = initial
        initial.setdefault("idempotency_key", uuid.uuid4())
        super().__init__(*args, **kwargs)
        self.fields["goal_type"].label = "Goal type"
        self.fields["player"].label = "Player"
        self.fields["player_name"].label = "New player's name"
        self.fields["player"].queryset = Player.objects.filter(
            team__in=[self.benefiting_team, self.opposing_team],
        ).select_related("team").order_by("team__name", "name", "id")

    @property
    def expected_player_team(self):
        if self.cleaned_data.get("goal_type") == Goal.GoalType.OWN_GOAL:
            return self.opposing_team
        return self.benefiting_team

    def clean_player_name(self):
        return self.cleaned_data["player_name"].strip()

    def clean(self):
        cleaned_data = super().clean()
        goal_type = cleaned_data.get("goal_type") or Goal.GoalType.NORMAL
        cleaned_data["goal_type"] = goal_type
        player = cleaned_data.get("player")
        player_name = cleaned_data.get("player_name")

        if player and player.team_id != self.expected_player_team.id:
            if goal_type == Goal.GoalType.OWN_GOAL:
                self.add_error(
                    "player",
                    "For an own goal, choose a player from the opposing team.",
                )
            else:
                self.add_error(
                    "player",
                    "Choose a player from the team receiving the goal.",
                )

        if player and player_name:
            self.add_error(
                "player_name",
                "Select an existing player or enter a new player, not both.",
            )

        if cleaned_data.get("new_player_requested") and not player_name:
            self.add_error("player_name", "Enter the new player's name.")

        if (
            goal_type == Goal.GoalType.OWN_GOAL
            and not self.allow_unattributed_own_goal
            and not (player or player_name)
        ):
            self.add_error(
                "player",
                "Select or enter the opposing player responsible for the own goal.",
            )

        minute = cleaned_data.get("minute")
        if (
            minute is not None
            and self.match.status == "LIVE"
            and minute > self.match.get_current_minute()
        ):
            self.add_error(
                "minute",
                "The goal minute cannot be later than the current live match minute.",
            )

        return cleaned_data

    def resolve_player(self):
        """Resolve through the shared, match-derived roster service."""
        return resolve_goal_scorer(
            match=self.match,
            benefiting_team_id=self.benefiting_team.id,
            goal_type=self.cleaned_data["goal_type"],
            player=self.cleaned_data.get("player"),
            new_player_name=self.cleaned_data.get("player_name", ""),
            allow_unattributed_own_goal=self.allow_unattributed_own_goal,
        )


class FinishedGoalPlayerChoice(forms.ModelChoiceField):
    def to_python(self, value):
        return None if value == "__new__" else super().to_python(value)


class FinishedGoalEntryForm(GoalEntryForm):
    allow_unattributed_own_goal = True
    idempotency_key = None
    new_player_requested = None
    player = FinishedGoalPlayerChoice(
        queryset=Player.objects.none(),
        required=False,
        widget=TeamPlayerSelect(attrs={"class": "form-select"}),
    )
    record_unknown = forms.BooleanField(required=False, label="Record without a known scorer")

    def __init__(self, *args, roster, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["player"].choices = [
            ("", "Scorer not recorded"),
            *((ModelChoiceIteratorValue(player.pk, player), str(player)) for player in roster),
            ("__new__", "+ Add New Player"),
        ]

    def clean(self):
        data = super().clean()
        if self["player"].data == "__new__" and not data.get("player_name"):
            self.add_error("player_name", "Enter the new player's name.")
        if data.get("record_unknown") and (data.get("player") or data.get("player_name")):
            self.add_error("record_unknown", "Choose a scorer or record the goal without one.")
        return data


class StartFirstHalfForm(forms.Form):
    """Validate the optional elapsed minute supplied at the initial start."""

    current_minute = forms.CharField(
        required=False,
        label="Current Match Minute (optional)",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "inputmode": "numeric",
            "pattern": r"[0-9]+(\+[0-9]+)?",
        }),
        help_text=(
            "Leave empty to start from 0'. If the match has already started, "
            "enter its current minute. Use 45+2 for first-half stoppage time."
        ),
    )

    def clean_current_minute(self):
        raw_value = self.cleaned_data["current_minute"].strip()
        if not raw_value:
            return 0

        match = re.fullmatch(r"(?P<minute>\d+)(?:\+(?P<stoppage>\d+))?", raw_value)
        if not match:
            raise ValidationError("Enter a whole minute or first-half stoppage time such as 45+2.")

        minute = int(match.group("minute"))
        stoppage = match.group("stoppage")
        if stoppage is not None:
            if minute != 45 or int(stoppage) < 1:
                raise ValidationError(
                    "Only first-half stoppage time in the form 45+N can be used when starting a match."
                )
            return minute + int(stoppage)

        if minute > 45:
            raise ValidationError(
                "Enter a first-half minute from 0 to 45, or use 45+N for stoppage time."
            )
        return minute


class QuickFinalResultForm(forms.Form):
    """Small, explicit score form for an untracked football match."""

    home_score = forms.IntegerField(
        min_value=0,
        label="Home score",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    away_score = forms.IntegerField(
        min_value=0,
        label="Away score",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )


ABANDONED_MATCH_ADMIN_METADATA_FIELDS = frozenset({"match_date", "location"})


def validate_abandoned_match_admin_edit(match, *, cleaned_data=None, lock=False):
    """Keep suspended sporting records immutable outside their decision service."""
    if not match.pk:
        return
    queryset = Match.objects.select_for_update() if lock else Match.objects.all()
    original = queryset.filter(pk=match.pk).first()
    if not original or not goal_admin_match_is_protected(original):
        return
    changed = []
    for field in Match._meta.concrete_fields:
        if field.primary_key or field.name in ABANDONED_MATCH_ADMIN_METADATA_FIELDS:
            continue
        if cleaned_data is not None:
            if field.name not in cleaned_data:
                continue
            value = cleaned_data[field.name]
            if field.is_relation:
                value = value.pk if value is not None else None
        else:
            value = getattr(match, field.attname)
        if value != getattr(original, field.attname):
            changed.append(str(field.verbose_name))
    if changed:
        raise ValidationError(
            "An abandoned match's score, clock, fixture identity and history are "
            "locked. Use its controlled resolution workflow. Only kickoff and "
            "location metadata may be edited here. Changed fields: " + ", ".join(changed) + "."
        )


def goal_admin_match_is_protected(match):
    return bool(
        match and (
            match.status == "ABANDONED"
            or match.abandonment_resolution in {
                Match.AbandonmentResolution.AWARDED,
                Match.AbandonmentResolution.CONFIRMED,
            }
        )
    )


def validate_goal_admin_edit(goal, *, target_match_id=None, lock=False):
    """Check both ends of a goal move, including its persisted source fixture."""
    match_ids = {target_match_id if target_match_id is not None else goal.match_id}
    source_match_id = None
    if goal.pk:
        source_match_id = Goal.objects.filter(pk=goal.pk).values_list("match_id", flat=True).first()
        match_ids.add(source_match_id)
    queryset = Match.objects.order_by("pk")
    if lock:
        queryset = queryset.select_for_update()
    matches = list(queryset.filter(pk__in=match_ids - {None}))
    if lock and goal.pk:
        locked_source_id = (
            Goal.objects.select_for_update().filter(pk=goal.pk)
            .values_list("match_id", flat=True).first()
        )
        if locked_source_id != source_match_id:
            raise ValidationError("The goal changed concurrently. Reload it before editing its history.")
    for match in matches:
        if goal_admin_match_is_protected(match):
            raise ValidationError(
                "Goal history for an abandoned match or an awarded/confirmed "
                "abandonment decision is read-only. Reopen the decision through "
                "the controlled workflow before correcting its sporting record."
            )


class GoalAdminForm(forms.ModelForm):
    class Meta:
        model = Goal
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        target_match = cleaned_data.get("match")
        validate_goal_admin_edit(
            self.instance,
            target_match_id=target_match.pk if target_match else None,
        )
        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        validate_goal_admin_edit(self.instance, lock=True)
        return super().save(commit=commit)


class MatchAdminForm(forms.ModelForm):
    home_score = forms.IntegerField(
        min_value=0,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "inputmode": "numeric",
            "autocomplete": "off",
        })
    )
    away_score = forms.IntegerField(
        min_value=0,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "inputmode": "numeric",
            "autocomplete": "off",
        })
    )

    class Meta:
        model = Match
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        validate_abandoned_match_admin_edit(self.instance, cleaned_data=cleaned_data)

        competition = cleaned_data.get("competition")
        stage = cleaned_data.get("stage")
        home_team = cleaned_data.get("home_team")
        away_team = cleaned_data.get("away_team")
        candidate = None

        if home_team and away_team and home_team == away_team:
            raise ValidationError({
                "away_team": "A team cannot play against itself."
            })

        if competition and stage and home_team and away_team:
            candidate = Match(
                pk=self.instance.pk,
                competition=competition,
                stage=stage,
                group=cleaned_data.get("group"),
                home_team=home_team,
                away_team=away_team,
                leg=(
                    cleaned_data.get("leg")
                    if cleaned_data.get("leg") is not None
                    else 1
                ),
                knockout_tie=cleaned_data.get("knockout_tie"),
                status=cleaned_data.get("status") or "UPCOMING",
                phase=cleaned_data.get("phase") or "NONE",
                home_score=cleaned_data.get("home_score") or 0,
                away_score=cleaned_data.get("away_score") or 0,
                regulation_home_score=cleaned_data.get("regulation_home_score"),
                regulation_away_score=cleaned_data.get("regulation_away_score"),
                penalty_home_score=cleaned_data.get("penalty_home_score"),
                penalty_away_score=cleaned_data.get("penalty_away_score"),
                resolution_method=cleaned_data.get("resolution_method") or "",
            )
            try:
                validate_fixture_knockout_config(candidate)
            except ValidationError as error:
                raise ValidationError(error)

        if cleaned_data.get("status") == "FINISHED" and candidate is not None:
            if (
                candidate.stage.stage_type == "KNOCKOUT"
                and not candidate.knockout_tie_id
            ):
                self.add_error(
                    "status",
                    (
                        "Save this knockout fixture first so it can be linked to "
                        "a configured tie before entering its result."
                    ),
                )
            else:
                try:
                    self.normalized_result = validate_official_result(
                        candidate,
                        home_score=candidate.home_score,
                        away_score=candidate.away_score,
                        regulation_home_score=candidate.regulation_home_score,
                        regulation_away_score=candidate.regulation_away_score,
                        penalty_home_score=candidate.penalty_home_score,
                        penalty_away_score=candidate.penalty_away_score,
                    )
                except ValidationError as error:
                    raise ValidationError(error)

        if candidate is not None and candidate.status != "FINISHED":
            has_official_result_details = any([
                candidate.regulation_home_score is not None,
                candidate.regulation_away_score is not None,
                candidate.penalty_home_score is not None,
                candidate.penalty_away_score is not None,
                bool(candidate.resolution_method),
            ])
            if has_official_result_details:
                self.add_error(
                    "status",
                    "Official result details can only be saved with a finished match.",
                )

            if (
                candidate.stage.stage_type == "KNOCKOUT"
                and candidate.phase == "PENALTIES"
                and any(
                    field in self.changed_data
                    for field in ("home_score", "away_score")
                )
            ):
                self.add_error(
                    "home_score",
                    "Penalty shootout kicks must be recorded separately from football goals.",
                )

        if (
            self.instance.pk
            and self.instance.status == "FINISHED"
            and cleaned_data.get("status") not in {None, "FINISHED"}
        ):
            self.add_error(
                "status",
                "A finished match cannot be reopened directly in Django admin.",
            )

        if (
            self.instance.pk
            and self.instance.status == "ABANDONED"
            and cleaned_data.get("status") not in {None, "ABANDONED"}
        ):
            self.add_error(
                "status",
                "An abandoned match must be resolved through the controlled "
                "abandoned-match workflow, not by changing its status here.",
            )

        if (
            self.instance.pk
            and cleaned_data.get("status") == "ABANDONED"
            and self.instance.status != "ABANDONED"
        ):
            self.add_error(
                "status",
                "Use the match control to abandon a fixture so its frozen score, "
                "minute and reason are recorded safely.",
            )

        fixture_identity_fields = {
            "competition",
            "stage",
            "group",
            "home_team",
            "away_team",
            "leg",
            "knockout_tie",
        }
        changed_fixture_identity_fields = (
            fixture_identity_fields.intersection(self.changed_data)
        )
        if (
            candidate is not None
            and candidate.knockout_tie_id
            and candidate.knockout_tie.position is not None
            and changed_fixture_identity_fields
        ):
            self.add_error(
                None,
                (
                    "Teams, stage, leg and tie links for a connected bracket fixture "
                    "are controlled by the bracket service."
                ),
            )
        is_safe_legacy_tie_link = (
            changed_fixture_identity_fields == {"knockout_tie"}
            and not self.instance.knockout_tie_id
            and candidate is not None
            and candidate.knockout_tie_id
        )
        if (
            candidate is not None
            and candidate.status == "FINISHED"
            and self.instance.pk
            and changed_fixture_identity_fields
            and not is_safe_legacy_tie_link
        ):
            self.add_error(
                None,
                (
                    "Save fixture identity changes separately before finalizing or "
                    "correcting a result. An unlinked legacy fixture may be linked "
                    "to a valid tie explicitly."
                ),
            )

        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        validate_abandoned_match_admin_edit(self.instance, lock=True)
        match = super().save(commit=False)
        if match.stage_id and match.home_team_id and match.away_team_id:
            assign_match_to_knockout_tie(match)
        if commit:
            match.save()
            self.save_m2m()
        return match

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance.pk:
            competition = self.instance.competition
            for name, model in (("home_team", Team), ("away_team", Team), ("stage", Stage)):
                if name in self.fields:
                    self.fields[name].queryset = model.objects.filter(competition=competition)

        elif "competition" in self.data:
            try:
                competition_id = int(self.data.get("competition"))

                self.fields["home_team"].queryset = Team.objects.filter(
                    competition_id=competition_id
                )
                self.fields["away_team"].queryset = Team.objects.filter(
                    competition_id=competition_id
                )
                self.fields["stage"].queryset = Stage.objects.filter(
                    competition_id=competition_id
                )

            except (ValueError, TypeError):
                pass
            
class MatchCreateForm(forms.ModelForm):
    class Meta:
        model = Match
        fields = [
            "stage",
            "group",
            "home_team",
            "away_team",
            "match_date",
            "location",
            "leg",
        ]
        widgets = {
            "stage": forms.Select(attrs={"class": "form-select"}),
            "group": forms.Select(attrs={"class": "form-select"}),
            "home_team": forms.Select(attrs={"class": "form-select"}),
            "away_team": forms.Select(attrs={"class": "form-select"}),
            "match_date": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "form-control"},
                format="%Y-%m-%dT%H:%M"
            ),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "leg": forms.NumberInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        league = kwargs.pop("league", None)
        super().__init__(*args, **kwargs)

        self.league = league
        self.fields["group"].required = False

        if league:
            self.fields["stage"].queryset = Stage.objects.filter(
                competition=league
            ).order_by("order")

            self.fields["group"].queryset = Group.objects.filter(
                stage__competition=league
            ).select_related("stage").order_by("stage__order", "name")

            self.fields["home_team"].queryset = Team.objects.filter(
                competition=league
            ).order_by("name")

            self.fields["away_team"].queryset = Team.objects.filter(
                competition=league
            ).order_by("name")
        else:
            self.fields["stage"].queryset = Stage.objects.none()
            self.fields["group"].queryset = Group.objects.none()
            self.fields["home_team"].queryset = Team.objects.none()
            self.fields["away_team"].queryset = Team.objects.none()

    def clean(self):
        cleaned_data = super().clean()

        stage = cleaned_data.get("stage")
        group = cleaned_data.get("group")
        home_team = cleaned_data.get("home_team")
        away_team = cleaned_data.get("away_team")
        leg = cleaned_data.get("leg")

        if home_team and away_team and home_team == away_team:
            raise ValidationError({
                "away_team": "A team cannot play against itself."
            })

        if group and stage and group.stage != stage:
            raise ValidationError({
                "group": "Selected group does not belong to the selected stage."
            })

        if stage and stage.stage_type != "GROUP":
            cleaned_data["group"] = None

        league = getattr(self, "league", None)
        if league and stage and home_team and away_team and leg is not None:
            already_exists = Match.objects.filter(
                competition=league,
                stage=stage,
                home_team=home_team,
                away_team=away_team,
                leg=leg,
            ).exists()

            if already_exists:
                raise ValidationError(
                    "This match already exists in this stage for this leg."
                )

            candidate = Match(
                competition=league,
                stage=stage,
                group=cleaned_data.get("group"),
                home_team=home_team,
                away_team=away_team,
                leg=leg,
            )
            try:
                validate_fixture_knockout_config(candidate)
            except ValidationError as error:
                raise ValidationError(error)

        return cleaned_data

class MatchResultForm(forms.ModelForm):
    class Meta:
        model = Match
        fields = [
            "home_score",
            "away_score",
            "regulation_home_score",
            "regulation_away_score",
            "penalty_home_score",
            "penalty_away_score",
        ]
        widgets = {
            "home_score": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
            "away_score": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
            "regulation_home_score": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
            "regulation_away_score": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
            "penalty_home_score": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
            "penalty_away_score": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 0,
            }),
        }

    def clean(self):
        cleaned_data = super().clean()

        home_score = cleaned_data.get("home_score")
        away_score = cleaned_data.get("away_score")

        if home_score is None or away_score is None:
            raise ValidationError("Both scores are required.")

        if home_score < 0 or away_score < 0:
            raise ValidationError("Scores cannot be negative.")

        try:
            self.normalized_result = validate_official_result(
                self.instance,
                home_score=home_score,
                away_score=away_score,
                regulation_home_score=cleaned_data.get("regulation_home_score"),
                regulation_away_score=cleaned_data.get("regulation_away_score"),
                penalty_home_score=cleaned_data.get("penalty_home_score"),
                penalty_away_score=cleaned_data.get("penalty_away_score"),
            )
        except ValidationError as error:
            raise ValidationError(error)

        return cleaned_data
    
class CardEntryForm(forms.Form):
    """One server-validated entry workflow for yellow and red cards."""

    team = forms.ModelChoiceField(
        queryset=Team.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    player = forms.ModelChoiceField(
        queryset=Player.objects.none(),
        required=False,
        empty_label="-- Select player --",
        widget=TeamPlayerSelect(attrs={"class": "form-select"}),
    )
    player_name = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "New player's name",
            "autocomplete": "off",
        }),
    )
    new_player_requested = forms.CharField(required=False, widget=forms.HiddenInput)
    minute = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    idempotency_key = forms.UUIDField(required=True, widget=forms.HiddenInput)

    def __init__(self, *args, match, **kwargs):
        self.match = match
        initial = kwargs.get("initial") or {}
        kwargs["initial"] = initial
        initial.setdefault("idempotency_key", uuid.uuid4())
        super().__init__(*args, **kwargs)
        teams = Team.objects.filter(pk__in=[match.home_team_id, match.away_team_id]).order_by("name")
        self.fields["team"].queryset = teams
        self.fields["player"].queryset = Player.objects.filter(team__in=teams).select_related("team").order_by("team__name", "name", "id")

    def clean_player_name(self):
        return " ".join(self.cleaned_data["player_name"].split())

    def clean(self):
        cleaned_data = super().clean()
        team = cleaned_data.get("team")
        player = cleaned_data.get("player")
        player_name = cleaned_data.get("player_name")
        if team and team.id not in {self.match.home_team_id, self.match.away_team_id}:
            self.add_error("team", "Choose one of the teams playing in this match.")
        if player and team and player.team_id != team.id:
            self.add_error("player", "Choose a player registered for the selected team.")
        if player and player_name:
            self.add_error("player_name", "Select an existing player or enter a new player, not both.")
        if not player and not player_name:
            self.add_error("player", "Select a player or choose Add New Player.")
        if cleaned_data.get("new_player_requested") and not player_name:
            self.add_error("player_name", "Enter the new player's name.")
        return cleaned_data
