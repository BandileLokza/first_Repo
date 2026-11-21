"""Forms for the competition-scoped pool manager.

All foreign-key choices are deliberately narrowed to the selected pool
competition.  The views still enforce the same scope on lookup, so a crafted
POST cannot use these forms to cross a competition boundary.
"""

import uuid
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Max, Q
from django.forms import BaseFormSet, formset_factory

from .models import (
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionMembership,
    PoolCompetitionTeamStaffAssignment,
    PoolCompetitionMarginPointRule,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolKnockoutBracket,
    PoolKnockoutRoundDefinition,
    PoolMatch,
    PoolStandingBaseline,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchLineup,
    PoolTeamMatchLineupMember,
    PoolTeamMatchScheduledRack,
    PoolTeamRoster,
    PoolMatchScorerAssignment,
)


User = get_user_model()


def competition_allows_two_lives(competition: PoolCompetition) -> bool:
    """Return the narrow configuration in which entry lives are meaningful."""

    return bool(
        competition.allow_two_lives
        and competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
        and competition.format_type == PoolCompetition.FormatType.KNOCKOUT
    )


def competition_supports_two_lives(competition: PoolCompetition) -> bool:
    """Whether settings may offer the two-life option (before it is enabled)."""

    return bool(
        competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
        and competition.format_type == PoolCompetition.FormatType.KNOCKOUT
    )


class PoolCompetitionInfoForm(forms.ModelForm):
    """Editable information for an existing competition.

    Ownership and format are intentionally omitted.  The superuser assigns an
    initial owner in Django admin, and changing format after stages have been
    provisioned is already prohibited by the domain model.
    """

    class Meta:
        model = PoolCompetition
        fields = (
            "name",
            "slug",
            "description",
            "logo",
            "cover_image",
            "participant_mode",
            "visibility",
            "default_race_target",
            "match_time_limit_minutes",
            "allow_two_lives",
            "start_date",
            "end_date",
            "venue",
            "rules",
        )
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "slug": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "logo": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "cover_image": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "participant_mode": forms.Select(attrs={"class": "form-select"}),
            "visibility": forms.Select(attrs={"class": "form-select"}),
            "default_race_target": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
            "match_time_limit_minutes": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "placeholder": "No limit"}
            ),
            "allow_two_lives": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "end_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "venue": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "rules": forms.Textarea(attrs={"class": "form-control", "rows": 6}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["match_time_limit_minutes"].required = False
        if self.instance.format_type in {
            PoolCompetition.FormatType.LEAGUE,
            PoolCompetition.FormatType.MONEY_MATCH,
        }:
            # A pure league has no tournament countdown setting.  The model
            # repeats this restriction for crafted/admin saves.
            self.fields.pop("match_time_limit_minutes")
        else:
            self.fields["match_time_limit_minutes"].help_text = (
                "Optional. A duration is copied to a fixture only when it starts; "
                "changing this later affects future fixtures, not live ones."
            )

        if not competition_supports_two_lives(self.instance):
            self.fields.pop("allow_two_lives")
        else:
            self.fields["allow_two_lives"].help_text = (
                "Allow each singles entry to select one or two tournament lives. "
                "This cannot change after a draw preview or bracket exists."
            )


def _resolve_money_match_scorer(form, cleaned_data):
    """Resolve the optional single-fixture scorer used by Money Match forms."""

    identifier = (cleaned_data.get("scorer_identifier") or "").strip()
    if identifier:
        users = (
            User.objects.filter(is_active=True)
            .filter(Q(email__iexact=identifier) | Q(username__iexact=identifier))
            .distinct()
        )
        if not users.exists():
            form.add_error(
                "scorer_identifier",
                "No active account matches that email or username.",
            )
        elif users.count() > 1:
            form.add_error(
                "scorer_identifier",
                "That identifier matches more than one account. Use the exact username.",
            )
        else:
            cleaned_data["scorer"] = users.get()
    else:
        cleaned_data["scorer"] = None
    return cleaned_data


class PoolMoneyMatchCreateForm(forms.Form):
    """Small, typed input surface for atomically creating one Money Match."""

    title = forms.CharField(
        required=False,
        max_length=200,
        label="Title (optional)",
        help_text="Leave blank to use the two opponents' names.",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    player_one = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(), required=False, label="Player 1",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    player_two = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(), required=False, label="Player 2",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    race_target = forms.IntegerField(
        min_value=1,
        initial=7,
        label="Race target",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )
    money_match_prize_pot = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
        label="Prize Pot",
        help_text="Optional. Total prize money for this Money Match.",
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": "0", "step": "0.01"}
        ),
    )
    scheduled_at = forms.DateTimeField(
        required=False,
        label="Match date and time",
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    venue = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    scorer_identifier = forms.CharField(
        required=False,
        max_length=254,
        label="Scorer email or username (optional)",
        help_text="The scorer may operate only this Money Match.",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    visibility = forms.ChoiceField(
        choices=PoolCompetition.Visibility.choices,
        initial=PoolCompetition.Visibility.DRAFT,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["player_one"].queryset = PoolPlayer.objects.filter(
            is_active=True
        ).order_by("full_name", "id")
        self.fields["player_two"].queryset = self.fields["player_one"].queryset

    def clean(self):
        cleaned_data = super().clean()
        opponent_one = cleaned_data.get("player_one")
        opponent_two = cleaned_data.get("player_two")
        if not opponent_one:
            self.add_error("player_one", "Select Player 1.")
        if not opponent_two:
            self.add_error("player_two", "Select Player 2.")
        if opponent_one and opponent_two and opponent_one.pk == opponent_two.pk:
            self.add_error("player_two", "A player cannot face themselves.")
        cleaned_data["opponent_one"] = opponent_one
        cleaned_data["opponent_two"] = opponent_two

        return _resolve_money_match_scorer(self, cleaned_data)


class PoolMoneyMatchEditForm(forms.Form):
    """Configure or edit the two opponents and pre-play Money Match details."""

    title = forms.CharField(
        required=False,
        max_length=200,
        label="Title (optional)",
        help_text="Leave blank to use the two opponents' names.",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    player_one = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(), required=False, label="Player 1",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    player_two = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(), required=False, label="Player 2",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    race_target = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )
    money_match_prize_pot = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
        label="Prize Pot",
        help_text="Optional. Total prize money for this Money Match.",
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": "0", "step": "0.01"}
        ),
    )
    scheduled_at = forms.DateTimeField(
        required=False,
        label="Match date and time",
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    venue = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    scorer_identifier = forms.CharField(
        required=False,
        max_length=254,
        label="Scorer email or username (optional)",
        help_text="The scorer may operate only this Money Match.",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
    )
    visibility = forms.ChoiceField(
        choices=PoolCompetition.Visibility.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, competition, pool_match=None, **kwargs):
        self.competition = competition
        self.pool_match = pool_match
        entries = list(competition.entries.select_related("player").order_by("id"))
        if pool_match is not None:
            entries = [pool_match.home_entry, pool_match.away_entry]

        initial = dict(kwargs.pop("initial", {}))
        initial.update(
            {
                "title": competition.name,
                "race_target": (
                    pool_match.race_target
                    if pool_match is not None
                    else competition.default_race_target
                ),
                "scheduled_at": (
                    pool_match.scheduled_at if pool_match is not None else None
                ),
                "venue": pool_match.venue if pool_match is not None else competition.venue,
                "money_match_prize_pot": competition.money_match_prize_pot,
                "visibility": competition.visibility,
            }
        )
        scorer_assignment = (
            pool_match.scorer_assignments.filter(is_active=True)
            .select_related("user")
            .order_by("id")
            .first()
            if pool_match is not None
            else None
        )
        if scorer_assignment is not None:
            initial["scorer_identifier"] = scorer_assignment.user.username

        initial["player_one"] = entries[0].player if len(entries) > 0 else None
        initial["player_two"] = entries[1].player if len(entries) > 1 else None
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)
        self.fields["player_one"].queryset = PoolPlayer.objects.filter(
            is_active=True
        ).order_by("full_name", "id")
        self.fields["player_two"].queryset = self.fields["player_one"].queryset

    def clean(self):
        cleaned_data = super().clean()
        opponent_one = cleaned_data.get("player_one")
        opponent_two = cleaned_data.get("player_two")
        if not opponent_one:
            self.add_error("player_one", "Select Player 1.")
        if not opponent_two:
            self.add_error("player_two", "Select Player 2.")
        if opponent_one and opponent_two and opponent_one.pk == opponent_two.pk:
            self.add_error("player_two", "A player cannot face themselves.")
        cleaned_data["opponent_one"] = opponent_one
        cleaned_data["opponent_two"] = opponent_two
        return _resolve_money_match_scorer(self, cleaned_data)


class PoolNextSeasonForm(forms.Form):
    """The deliberately small superuser input for a fresh league season."""

    season = forms.CharField(
        max_length=20,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "e.g. 2027 or 2026/27",
            }
        ),
    )
    slug = forms.SlugField(
        max_length=50,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "e.g. city-pool-league-2027",
            }
        ),
    )


class PoolCompetitionEntryForm(forms.ModelForm):
    """Create one correctly typed entry in a single competition."""

    class Meta:
        model = PoolCompetitionEntry
        fields = ("player", "team", "seed")
        widgets = {
            "player": forms.Select(attrs={"class": "form-select"}),
            "team": forms.Select(attrs={"class": "form-select"}),
            "seed": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "placeholder": "Optional"}
            ),
        }

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        self.allows_two_lives = competition_allows_two_lives(competition)
        super().__init__(*args, **kwargs)
        self.instance.competition = competition
        self.fields["player"].queryset = PoolPlayer.objects.filter(
            is_active=True
        ).order_by("full_name", "id")
        self.fields["team"].queryset = PoolTeam.objects.filter(is_active=True).order_by(
            "name", "id"
        )
        self.fields["seed"].required = False
        self.selected_player = None

        if competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES:
            self.fields.pop("team")
            self.fields["player"].label = "Player"
            # A normal ModelChoiceField retains the authoritative server-side
            # validation while HiddenInput avoids rendering the whole global
            # player registry into every competition-entry page.  The manager
            # template fills this ID only after a same-scope search endpoint
            # has returned a deliberate selection.
            self.fields["player"].widget = forms.HiddenInput()
            self.fields["player"].help_text = (
                "Search for an existing active player, then select the result."
            )
            selected_value = (
                self.data.get(self.add_prefix("player"))
                if self.is_bound
                else self.initial.get("player", self.instance.player_id)
            )
            selected_id = getattr(selected_value, "pk", selected_value)
            if selected_id:
                self.selected_player = PoolPlayer.objects.filter(
                    pk=selected_id,
                    is_active=True,
                ).first()
            if self.allows_two_lives:
                self.fields["lives"] = forms.TypedChoiceField(
                    choices=((1, "1 life"), (2, "2 lives")),
                    coerce=int,
                    initial=1,
                    label="Lives",
                    help_text=(
                        "Life 1 and Life 2 share this one real player and "
                        "competition entry."
                    ),
                    widget=forms.Select(attrs={"class": "form-select"}),
                )
        else:
            self.fields.pop("player")
            self.fields["team"].label = "Team"

    def clean(self):
        cleaned_data = super().clean()
        self.instance.competition = self.competition
        requested_lives = cleaned_data.get("lives", 1)
        if not self.allows_two_lives:
            posted_lives = self.data.get(self.add_prefix("lives"))
            if posted_lives not in (None, "", "1", 1):
                self.add_error(
                    None,
                    "Two lives are not available for this competition.",
                )
        elif requested_lives not in {1, 2}:
            self.add_error("lives", "Choose either one or two lives.")
        cleaned_data["lives"] = requested_lives
        return cleaned_data


class PoolExistingPlayerBulkEntryForm(forms.Form):
    """Validate a basket of existing players for one singles competition.

    The picker intentionally posts a small set of hidden IDs rather than a
    rendered copy of the global player registry.  This form treats those IDs
    only as a request: every player, activity state, competition entry, and
    requested life count is checked on the server before the view writes.
    """

    player_ids = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        self.allows_two_lives = competition_allows_two_lives(competition)
        self.selected_players = ()
        self.lives_by_player_id = {}
        super().__init__(*args, **kwargs)

    def _submitted_player_ids(self):
        """Read repeated picker values, while accepting the old one-player POST."""

        field_name = self.add_prefix("player_ids")
        if hasattr(self.data, "getlist"):
            values = self.data.getlist(field_name)
        else:
            raw_values = self.data.get(field_name, [])
            values = (
                list(raw_values)
                if isinstance(raw_values, (list, tuple))
                else [raw_values]
            )
        if not any(str(value).strip() for value in values):
            legacy_value = self.data.get(self.add_prefix("player"), "")
            if str(legacy_value).strip():
                values = [legacy_value]
        return values

    def clean(self):
        cleaned_data = super().clean()
        if self.competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
            self.add_error(None, "Existing players can only be added to a singles competition.")
            return cleaned_data

        submitted_values = self._submitted_player_ids()
        player_ids = []
        invalid_values = []
        for value in submitted_values:
            value = str(value).strip()
            if not value:
                continue
            try:
                player_id = int(value)
            except (TypeError, ValueError):
                invalid_values.append(value)
                continue
            if player_id < 1:
                invalid_values.append(value)
                continue
            player_ids.append(player_id)

        if not player_ids:
            self.add_error("player_ids", "Select at least one existing player.")
            return cleaned_data
        if invalid_values:
            self.add_error("player_ids", "One or more selected players are invalid.")
            return cleaned_data
        if len(set(player_ids)) != len(player_ids):
            self.add_error("player_ids", "Select each player only once.")
            return cleaned_data

        players_by_id = {
            player.pk: player
            for player in PoolPlayer.objects.filter(pk__in=player_ids)
        }
        missing_player_ids = [
            player_id for player_id in player_ids if player_id not in players_by_id
        ]
        if missing_player_ids:
            self.add_error("player_ids", "One or more selected players no longer exist.")
            return cleaned_data

        selected_players = [players_by_id[player_id] for player_id in player_ids]
        self.selected_players = tuple(selected_players)
        inactive_players = [player.full_name for player in selected_players if not player.is_active]
        if inactive_players:
            self.add_error(
                "player_ids",
                "Only active players can be added to a competition: "
                + ", ".join(inactive_players[:3])
                + ("." if len(inactive_players) <= 3 else ", and others."),
            )
            return cleaned_data

        already_entered_ids = set(
            PoolCompetitionEntry.objects.filter(
                competition=self.competition,
                player_id__in=player_ids,
            ).values_list("player_id", flat=True)
        )
        if already_entered_ids:
            already_entered_names = [
                player.full_name
                for player in selected_players
                if player.pk in already_entered_ids
            ]
            self.add_error(
                "player_ids",
                "These players are already entered in this competition: "
                + ", ".join(already_entered_names[:3])
                + ("." if len(already_entered_names) <= 3 else ", and others."),
            )
            return cleaned_data

        lives_by_player_id = {}
        for player in selected_players:
            raw_lives = self.data.get(
                self.add_prefix(f"player_lives_{player.pk}"),
                self.data.get(self.add_prefix("lives"), "1"),
            )
            try:
                requested_lives = int(raw_lives or 1)
            except (TypeError, ValueError):
                requested_lives = None
            if requested_lives not in {1, 2}:
                self.add_error(
                    "player_ids",
                    f"Choose either one or two lives for {player.full_name}.",
                )
                return cleaned_data
            if not self.allows_two_lives and requested_lives != 1:
                self.add_error(
                    "player_ids",
                    "Two lives are not available for this competition.",
                )
                return cleaned_data
            lives_by_player_id[player.pk] = requested_lives

        self.lives_by_player_id = lives_by_player_id
        cleaned_data["player_ids"] = player_ids
        cleaned_data["players"] = selected_players
        cleaned_data["lives_by_player_id"] = lives_by_player_id
        return cleaned_data


class PoolPlayerRegistrationForm(forms.ModelForm):
    """Create a global player profile from the scoped entry workflow.

    A player does not need a site login to enter a tournament.  This form
    intentionally does not expose the optional ``PoolPlayer.user`` relation,
    and its contact fields stay on the manager-only registration path.
    """

    seed = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": 1, "placeholder": "Optional"}
        ),
        help_text="Optional competition seed.",
    )

    class Meta:
        model = PoolPlayer
        fields = (
            "full_name",
            "nickname",
            "region",
            "country",
            "gender",
            "contact_email",
            "contact_phone",
            "profile_picture",
        )
        widgets = {
            "full_name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "name"}
            ),
            "nickname": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "region": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "address-level1"}
            ),
            "country": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "country-name"}
            ),
            "gender": forms.Select(attrs={"class": "form-select"}),
            "contact_email": forms.EmailInput(
                attrs={"class": "form-control", "autocomplete": "email"}
            ),
            "contact_phone": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "tel"}
            ),
            "profile_picture": forms.ClearableFileInput(attrs={"class": "form-control"}),
        }
        help_texts = {
            "contact_email": "Private: shown only to authorised organisers.",
            "contact_phone": "Private: shown only to authorised organisers.",
        }

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        self.allows_two_lives = competition_allows_two_lives(competition)
        super().__init__(*args, **kwargs)
        if self.allows_two_lives:
            self.fields["lives"] = forms.TypedChoiceField(
                choices=((1, "1 life"), (2, "2 lives")),
                coerce=int,
                initial=1,
                label="Lives",
                help_text=(
                    "Life 1 and Life 2 share this one real player and "
                    "competition entry."
                ),
                widget=forms.Select(attrs={"class": "form-select"}),
            )

    def clean_full_name(self):
        full_name = " ".join(self.cleaned_data["full_name"].split())
        if not full_name:
            raise ValidationError("A full name is required.")
        return full_name

    def clean_region(self):
        """Keep duplicate matching and the saved profile's region consistent."""

        return " ".join(self.cleaned_data.get("region", "").split())

    def clean(self):
        cleaned_data = super().clean()
        requested_lives = cleaned_data.get("lives", 1)
        if not self.allows_two_lives:
            posted_lives = self.data.get(self.add_prefix("lives"))
            if posted_lives not in (None, "", "1", 1):
                self.add_error(
                    None,
                    "Two lives are not available for this competition.",
                )
        elif requested_lives not in {1, 2}:
            self.add_error("lives", "Choose either one or two lives.")
        cleaned_data["lives"] = requested_lives
        return cleaned_data

    def potential_duplicate_players(self):
        """Return a small, explainable set of possible existing people.

        This is deliberately a suggestion mechanism, rather than an automatic
        merge rule: exact names and nicknames can legitimately belong to
        different people.  The manager gets region context before choosing
        either an existing profile or an explicit new registration.
        """

        if not hasattr(self, "cleaned_data"):
            return PoolPlayer.objects.none()

        full_name = self.cleaned_data.get("full_name")
        nickname = self.cleaned_data.get("nickname", "").strip()
        region = self.cleaned_data.get("region", "")
        if not full_name:
            return PoolPlayer.objects.none()

        candidates = Q(full_name__iexact=full_name) | Q(
            full_name__icontains=full_name
        )
        if nickname:
            candidates |= Q(nickname__iexact=nickname) | Q(
                nickname__icontains=nickname
            )

        candidates = (
            PoolPlayer.objects.filter(is_active=True)
            .filter(candidates)
        )
        if region:
            # A normalised submitted region is the strongest useful duplicate
            # signal in this lightweight workflow.  Keep suggestions local
            # when a same-region candidate exists, while still allowing the
            # manager to register a genuinely new player when it does not.
            same_region = candidates.filter(region__iexact=region)
            if same_region.exists():
                candidates = same_region
        return candidates.order_by("full_name", "id")[:10]


class PoolTeamRosterPlayerRegistrationForm(PoolPlayerRegistrationForm):
    """Register a global player profile from one competition team roster.

    The profile and duplicate-detection behaviour deliberately matches singles
    registration, while competition-entry-only settings stay out of this
    roster-scoped flow.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("seed", None)
        self.fields.pop("lives", None)


class PoolCompetitionEntrySeedForm(forms.ModelForm):
    """Allow sequencing changes without changing an entered participant."""

    class Meta:
        model = PoolCompetitionEntry
        fields = ("seed",)
        widgets = {
            "seed": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "placeholder": "Optional"}
            ),
        }

    def __init__(self, *args, competition: PoolCompetition | None = None, **kwargs):
        self.competition = competition
        self.allows_two_lives = bool(
            competition and competition_allows_two_lives(competition)
        )
        super().__init__(*args, **kwargs)
        self.fields["seed"].required = False
        if self.allows_two_lives:
            self.fields["lives"] = forms.TypedChoiceField(
                choices=((1, "1 life"), (2, "2 lives")),
                coerce=int,
                initial=self.instance.tournament_life_count,
                label="Lives",
                help_text=(
                    "Changing this is blocked once a draw preview or bracket "
                    "depends on the entry."
                ),
                widget=forms.Select(attrs={"class": "form-select"}),
            )

    def clean(self):
        cleaned_data = super().clean()
        requested_lives = cleaned_data.get("lives", 1)
        if not self.allows_two_lives:
            posted_lives = self.data.get(self.add_prefix("lives"))
            if posted_lives not in (None, "", "1", 1):
                self.add_error(
                    None,
                    "Two lives are not available for this competition.",
                )
        cleaned_data["lives"] = requested_lives
        return cleaned_data


class PoolTeamForm(forms.ModelForm):
    """A pool team linked through a competition entry by the calling view."""

    class Meta:
        model = PoolTeam
        fields = ("name", "logo", "description", "is_active")
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "logo": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise ValidationError("A team name is required.")
        return name


class PoolTeamRosterForm(forms.ModelForm):
    """Add a player to one competition-specific team roster."""

    class Meta:
        model = PoolTeamRoster
        fields = ("player", "is_active", "sort_order")
        widgets = {
            "player": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, competition_entry: PoolCompetitionEntry, **kwargs):
        self.competition_entry = competition_entry
        super().__init__(*args, **kwargs)
        self.instance.competition_entry = competition_entry
        self.fields["player"].queryset = PoolPlayer.objects.filter(
            is_active=True
        ).order_by("full_name", "id")

    def clean(self):
        cleaned_data = super().clean()
        self.instance.competition_entry = self.competition_entry
        return cleaned_data


class PoolTeamRosterBulkForm(forms.Form):
    """Validate a small roster-player basket without rendering every player.

    The manager view repeats this validation after taking the competition root
    lock.  The form provides clear feedback for a normal POST, while model
    validation remains the final guard for admin/direct writes and races.
    """

    player_ids = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, competition_entry: PoolCompetitionEntry, **kwargs):
        self.competition_entry = competition_entry
        self.selected_players = ()
        super().__init__(*args, **kwargs)

    def _submitted_player_ids(self):
        field_name = self.add_prefix("player_ids")
        if hasattr(self.data, "getlist"):
            values = self.data.getlist(field_name)
        else:
            raw_values = self.data.get(field_name, [])
            values = (
                list(raw_values)
                if isinstance(raw_values, (list, tuple))
                else [raw_values]
            )
        return [str(value).strip() for value in values if str(value).strip()]

    def clean(self):
        cleaned_data = super().clean()
        entry = self.competition_entry
        if not entry.team_id or (
            entry.competition.participant_mode
            != PoolCompetition.ParticipantMode.TEAMS
        ):
            self.add_error(None, "Roster players can only be added to a team competition entry.")
            return cleaned_data

        submitted_values = self._submitted_player_ids()
        player_ids = []
        for value in submitted_values:
            try:
                player_id = int(value)
            except (TypeError, ValueError):
                self.add_error("player_ids", "One or more selected players are invalid.")
                return cleaned_data
            if player_id < 1:
                self.add_error("player_ids", "One or more selected players are invalid.")
                return cleaned_data
            player_ids.append(player_id)
        if not player_ids:
            self.add_error("player_ids", "Select at least one active player.")
            return cleaned_data
        if len(set(player_ids)) != len(player_ids):
            self.add_error("player_ids", "Select each player only once.")
            return cleaned_data

        players_by_id = {
            player.pk: player
            for player in PoolPlayer.objects.filter(pk__in=player_ids)
        }
        if len(players_by_id) != len(player_ids):
            self.add_error("player_ids", "One or more selected players no longer exist.")
            return cleaned_data
        selected_players = [players_by_id[player_id] for player_id in player_ids]
        inactive_names = [
            player.full_name for player in selected_players if not player.is_active
        ]
        if inactive_names:
            self.add_error(
                "player_ids",
                "Only active players can join a roster: " + ", ".join(inactive_names[:3]),
            )
            return cleaned_data

        memberships = list(
            PoolTeamRoster._base_manager.filter(
                competition_entry__competition=entry.competition,
                player_id__in=player_ids,
            ).select_related("competition_entry__team", "player")
        )
        if memberships:
            existing = memberships[0]
            if existing.competition_entry_id == entry.pk:
                self.add_error(
                    "player_ids",
                    f"{existing.player.full_name} is already on this competition roster.",
                )
            else:
                self.add_error(
                    "player_ids",
                    (
                        f"{existing.player.full_name} is already registered for "
                        f"{existing.competition_entry.team.name} in this competition."
                    ),
                )
            return cleaned_data

        self.selected_players = tuple(selected_players)
        cleaned_data["player_ids"] = player_ids
        cleaned_data["players"] = selected_players
        return cleaned_data


class PoolTeamRosterUpdateForm(forms.ModelForm):
    """Keep the selected roster player immutable when updating a roster row."""

    class Meta:
        model = PoolTeamRoster
        fields = ("is_active", "sort_order")
        widgets = {
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }


class PoolCompetitionTeamStaffForm(forms.Form):
    """Assign one existing account as a coach or captain for one team entry.

    The form intentionally uses a precise account identifier rather than a
    broad user picker.  The view scopes the target entry first, and the model
    repeats that it is a team entry, so a crafted POST cannot turn this into a
    competition-wide administrator assignment.
    """

    identifier = forms.CharField(
        label="Coach or captain email or username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "Enter exact email address or username",
            }
        ),
        help_text="The person must already have an active EsportsLive account.",
    )
    role = forms.ChoiceField(
        choices=PoolCompetitionTeamStaffAssignment.Role.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, competition_entry: PoolCompetitionEntry, **kwargs):
        self.competition_entry = competition_entry
        super().__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()
        users = (
            User.objects.filter(is_active=True)
            .filter(Q(email__iexact=identifier) | Q(username__iexact=identifier))
            .distinct()
        )
        if not users.exists():
            raise ValidationError("No active account matches that email or username.")
        if users.count() > 1:
            raise ValidationError(
                "That identifier matches more than one account. Use the exact username."
            )
        return users.get()

    def save(self):
        """Create or reactivate the entry-scoped assignment atomically enough for UI.

        The unique constraint keeps concurrent requests from duplicating an
        assignment; the manager view handles any resulting integrity error as
        a normal validation message.
        """

        assignment, _created = PoolCompetitionTeamStaffAssignment.objects.update_or_create(
            competition_entry=self.competition_entry,
            user=self.cleaned_data["identifier"],
            defaults={
                "role": self.cleaned_data["role"],
                "is_active": True,
            },
        )
        return assignment

class PoolMatchScorerForm(forms.Form):
    """Assign one existing active account to one exact pool fixture."""

    identifier = forms.CharField(
        label="Scorer email or username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "Enter exact email address or username",
            }
        ),
        help_text=(
            "The person must already have an active EsportsLive account. "
            "They will be able to score only this fixture."
        ),
    )

    def __init__(self, *args, match: PoolMatch, **kwargs):
        self.match = match
        super().__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()

        users = (
            User.objects.filter(is_active=True)
            .filter(
                Q(email__iexact=identifier)
                | Q(username__iexact=identifier)
            )
            .distinct()
        )

        if not users.exists():
            raise ValidationError(
                "No active account matches that email or username."
            )

        if users.count() > 1:
            raise ValidationError(
                "That identifier matches more than one account. "
                "Use the exact username."
            )

        return users.get()

    def save(self, *, assigned_by=None):
        assignment, _created = (
            PoolMatchScorerAssignment.objects.update_or_create(
                match=self.match,
                user=self.cleaned_data["identifier"],
                defaults={
                    "is_active": True,
                    "assigned_by": assigned_by,
                },
            )
        )
        return assignment


# Keep integrations that imported the former team-only form working while the
# public language and new call sites use the match-generic name.
PoolTeamMatchScorerForm = PoolMatchScorerForm

class PoolTeamMatchLineupForm(forms.Form):
    """A five-position team sheet with honest vacant positions when needed."""

    substitutes = forms.ModelMultipleChoiceField(
        queryset=PoolPlayer.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Optional. A substitute may later re-enter if free and still eligible.",
    )

    def __init__(
        self,
        *args,
        match: PoolMatch,
        team_entry: PoolCompetitionEntry,
        lineup: PoolTeamMatchLineup | None = None,
        **kwargs,
    ):
        self.match = match
        self.team_entry = team_entry
        self.lineup = lineup
        super().__init__(*args, **kwargs)
        roster_players = PoolPlayer.objects.filter(
            is_active=True,
            team_roster_memberships__competition_entry=team_entry,
            team_roster_memberships__is_active=True,
        ).distinct().order_by("full_name", "id")
        self.fields["substitutes"].queryset = roster_players
        self.fields["substitutes"].widget.attrs.update(
            {"class": "form-check-input"}
        )
        self.fields["substitutes"].label = "Optional substitutes"

        positions_by_number = {}
        submitted_member_ids = set()
        if lineup is not None:
            positions_by_number = {
                position.position: position.player_id
                for position in lineup.positions.all()
            }
            submitted_member_ids = set(
                lineup.members.values_list("player_id", flat=True)
            )

        for position in range(1, 6):
            field_name = f"starter_{position}"
            self.fields[field_name] = forms.ModelChoiceField(
                queryset=roster_players,
                label=f"Starter position {position}",
                required=False,
                empty_label="Vacant",
                widget=forms.Select(attrs={"class": "form-select"}),
            )
            if positions_by_number.get(position):
                self.initial[field_name] = positions_by_number[position]

        starter_ids = {
            player_id for player_id in positions_by_number.values() if player_id
        }
        if submitted_member_ids:
            self.initial["substitutes"] = list(submitted_member_ids - starter_ids)

    @property
    def starter_fields(self):
        return [self[f"starter_{position}"] for position in range(1, 6)]

    def clean(self):
        cleaned_data = super().clean()
        starters = [
            cleaned_data.get(f"starter_{position}") for position in range(1, 6)
        ]
        starter_ids = [player.pk for player in starters if player is not None]
        if not starter_ids:
            raise ValidationError(
                "Select at least one starter; leave only genuinely vacant positions blank."
            )
        if len(set(starter_ids)) != len(starter_ids):
            raise ValidationError("A player can occupy only one starting position.")

        substitutes = list(cleaned_data.get("substitutes") or [])
        substitute_ids = {player.pk for player in substitutes}
        overlap = set(starter_ids) & substitute_ids
        if overlap:
            raise ValidationError(
                "A starting player should not also be selected as a substitute."
            )

        cleaned_data["starters"] = starters
        cleaned_data["substitutes"] = substitutes
        return cleaned_data


class PoolTeamScheduledRackResultForm(forms.Form):
    """Record an outcome only for the two immutable scheduled-rack snapshots."""

    winner_player = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(),
        label="Rack winner",
        widget=forms.RadioSelect,
    )
    outcome_type = forms.ChoiceField(
        choices=tuple(
            choice
            for choice in PoolRack.OutcomeType.choices
            if choice[0] != PoolRack.OutcomeType.LINEUP_VACANCY
        ),
        label="Rack outcome",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        help_text="Optional scoring note for the audit history.",
    )

    def __init__(self, *args, scheduled_rack: PoolTeamMatchScheduledRack, **kwargs):
        self.scheduled_rack = scheduled_rack
        super().__init__(*args, **kwargs)
        player_ids = [
            player_id
            for player_id in (
                scheduled_rack.home_player_id,
                scheduled_rack.away_player_id,
            )
            if player_id
        ]
        self.fields["winner_player"].queryset = PoolPlayer.objects.filter(
            pk__in=player_ids
        ).order_by("full_name", "id")


class PoolTeamScheduledRackCorrectionForm(PoolTeamScheduledRackResultForm):
    """A scheduled-rack correction with the same immutable player snapshots."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].required = True
        self.fields["reason"].label = "Correction reason"
        self.fields["reason"].help_text = (
            "Explain the correction for the match result audit history."
        )


class PoolTeamMatchSubstitutionForm(forms.Form):
    """Choose a free submitted-squad player for one current team position."""

    position = forms.ChoiceField(widget=forms.Select(attrs={"class": "form-select"}))
    incoming_player = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(),
        label="Incoming player",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    def __init__(
        self,
        *args,
        match: PoolMatch,
        team_entry: PoolCompetitionEntry,
        lineup: PoolTeamMatchLineup | None,
        busy_player_ids=(),
        **kwargs,
    ):
        self.match = match
        self.team_entry = team_entry
        self.lineup = lineup
        super().__init__(*args, **kwargs)
        positions = list(lineup.positions.select_related("player").all()) if lineup else []
        occupied_positions = [position for position in positions if position.player_id]
        current_player_ids = {position.player_id for position in occupied_positions}
        self.fields["position"].choices = [
            (str(position.position), f"Position {position.position} — {position.player.full_name}")
            for position in occupied_positions
        ]
        member_ids = (
            lineup.members.values_list("player_id", flat=True) if lineup else []
        )
        self.fields["incoming_player"].queryset = PoolPlayer.objects.filter(
            pk__in=member_ids,
            is_active=True,
            team_roster_memberships__competition_entry=team_entry,
            team_roster_memberships__is_active=True,
        ).exclude(
            pk__in=set(current_player_ids) | set(busy_player_ids)
        ).distinct().order_by("full_name", "id")

    def clean(self):
        cleaned_data = super().clean()
        if not self.lineup:
            raise ValidationError("This team has not submitted a lineup.")
        position_value = cleaned_data.get("position")
        incoming = cleaned_data.get("incoming_player")
        if not position_value or not incoming:
            return cleaned_data
        try:
            position_number = int(position_value)
        except (TypeError, ValueError):
            self.add_error("position", "Choose a valid current position.")
            return cleaned_data
        current_player_id = (
            self.lineup.positions.filter(position=position_number)
            .values_list("player_id", flat=True)
            .first()
        )
        if current_player_id is None:
            self.add_error("position", "Choose a valid current position.")
        elif current_player_id == incoming.pk:
            self.add_error("incoming_player", "Choose a different submitted squad player.")
        cleaned_data["position"] = position_number
        return cleaned_data


class PoolTeamMatchVacancyFillForm(forms.Form):
    """Fill one vacant live-match position without recording a substitution."""

    position = forms.ChoiceField(widget=forms.Select(attrs={"class": "form-select"}))
    incoming_player = forms.ModelChoiceField(
        queryset=PoolPlayer.objects.none(),
        label="Player joining this vacant position",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    def __init__(
        self,
        *args,
        match: PoolMatch,
        team_entry: PoolCompetitionEntry,
        lineup: PoolTeamMatchLineup | None,
        busy_player_ids=(),
        **kwargs,
    ):
        self.match = match
        self.team_entry = team_entry
        self.lineup = lineup
        super().__init__(*args, **kwargs)
        positions = list(lineup.positions.select_related("player").all()) if lineup else []
        vacant_positions = [position for position in positions if not position.player_id]
        occupied_ids = {
            position.player_id for position in positions if position.player_id
        }
        self.fields["position"].choices = [
            (str(position.position), f"Position {position.position} (Vacant)")
            for position in vacant_positions
        ]
        self.fields["incoming_player"].queryset = (
            PoolPlayer.objects.filter(
                is_active=True,
                team_roster_memberships__competition_entry=team_entry,
                team_roster_memberships__is_active=True,
            )
            .exclude(pk__in=occupied_ids | set(busy_player_ids))
            .distinct()
            .order_by("full_name", "id")
        )

    def clean(self):
        cleaned_data = super().clean()
        if not self.lineup:
            raise ValidationError("This team has not submitted a lineup.")
        position_value = cleaned_data.get("position")
        incoming = cleaned_data.get("incoming_player")
        if not position_value or not incoming:
            return cleaned_data
        try:
            position_number = int(position_value)
        except (TypeError, ValueError):
            self.add_error("position", "Choose a valid vacant position.")
            return cleaned_data
        current_player_id = (
            self.lineup.positions.filter(position=position_number)
            .values_list("player_id", flat=True)
            .first()
        )
        if current_player_id is not None:
            self.add_error("position", "Use a normal substitution for an occupied position.")
        cleaned_data["position"] = position_number
        return cleaned_data


class PoolTeamScheduledRackStartForm(forms.Form):
    """Bounded table selector for the next eligible scheduled team rack."""

    table_number = forms.IntegerField(
        min_value=1,
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, match: PoolMatch, **kwargs):
        self.match = match
        super().__init__(*args, **kwargs)
        self.fields["table_number"].max_value = match.table_count


class PoolCompetitionStageForm(forms.ModelForm):
    """Create or update an explicit stage within its competition only."""

    class Meta:
        model = PoolCompetitionStage
        fields = ("name", "stage_type", "order")
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "stage_type": forms.Select(attrs={"class": "form-select"}),
            "order": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)
        self.instance.competition = competition

        allowed_types = {
            PoolCompetition.FormatType.LEAGUE: {
                PoolCompetitionStage.StageType.LEAGUE,
            },
            PoolCompetition.FormatType.LEAGUE_KNOCKOUT: {
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
                PoolCompetitionStage.StageType.KNOCKOUT,
            },
            PoolCompetition.FormatType.KNOCKOUT: {
                PoolCompetitionStage.StageType.KNOCKOUT,
            },
        }[competition.format_type]
        self.allowed_stage_types = allowed_types
        self.fields["stage_type"].choices = [
            choice
            for choice in PoolCompetitionStage.StageType.choices
            if choice[0] in allowed_types
        ]

    def clean(self):
        cleaned_data = super().clean()
        self.instance.competition = self.competition
        stage_type = cleaned_data.get("stage_type")
        if stage_type and stage_type not in self.allowed_stage_types:
            self.add_error("stage_type", "This stage type is not available for this format.")
        return cleaned_data


class PoolKnockoutRoundConfigurationForm(forms.Form):
    """Accept the one real input needed to build canonical round metadata."""

    competitor_count = forms.IntegerField(
        min_value=2,
        max_value=256,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 2, "max": 256}),
    )

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)
        if competition_allows_two_lives(competition):
            label = "Number of tournament lives"
        elif competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS:
            label = "Number of teams"
        else:
            label = "Number of players"
        self.fields["competitor_count"].label = label
        self.fields["competitor_count"].help_text = (
            "The system uses the next supported power of two and creates "
            "read-only round definitions through the final."
        )


class PoolFixtureForm(forms.ModelForm):
    """Fixture fields that cannot escape the selected competition."""

    class Meta:
        model = PoolMatch
        fields = (
            "stage",
            "group",
            "knockout_round",
            "home_entry",
            "away_entry",
            "home_entry_life",
            "away_entry_life",
            "scheduled_at",
            "race_target",
            "table_count",
            "gameweek",
            "round_label",
            "venue",
            "stream_url",
        )
        widgets = {
            "stage": forms.Select(attrs={"class": "form-select"}),
            "group": forms.Select(attrs={"class": "form-select"}),
            "knockout_round": forms.Select(attrs={"class": "form-select"}),
            "home_entry": forms.Select(attrs={"class": "form-select"}),
            "away_entry": forms.Select(attrs={"class": "form-select"}),
            "home_entry_life": forms.Select(attrs={"class": "form-select"}),
            "away_entry_life": forms.Select(attrs={"class": "form-select"}),
            "scheduled_at": forms.DateTimeInput(
                attrs={"class": "form-control", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "race_target": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "table_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 5}
            ),
            "gameweek": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "round_label": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "venue": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "stream_url": forms.URLInput(
                attrs={"class": "form-control", "placeholder": "https://…"}
            ),
        }

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)
        self.instance.competition = competition
        self.fields["scheduled_at"].required = False
        self.fields["scheduled_at"].input_formats = ("%Y-%m-%dT%H:%M",)
        self.fields["race_target"].required = False
        self.fields["gameweek"].required = False
        self.fields["gameweek"].label = "Gameweek"
        self.fields["gameweek"].help_text = (
            "Optional. Use the league's actual gameweek so manually entered "
            "fixtures can be grouped with the rest of the schedule."
        )
        # Existing fixtures and manager tests predate configurable physical
        # table counts.  Treat an omitted value as the safe one-table default
        # while exposing the bounded field for new team fixtures.
        self.fields["table_count"].required = False
        if competition.participant_mode != PoolCompetition.ParticipantMode.TEAMS:
            # Physical-table scheduling is isolated to the new team-fixture
            # workflow. Singles retain their existing fixture form exactly.
            self.fields.pop("table_count")
        stages = list(competition.stages.order_by("order", "name", "id"))
        self.fields["stage"].queryset = PoolCompetitionStage.objects.filter(
            pk__in=[stage.pk for stage in stages]
        ).order_by("order", "name", "id")
        selected_stage = self._selected_stage(stages)
        if selected_stage is not None and len(stages) == 1 and not self.is_bound:
            self.fields["stage"].initial = selected_stage.pk
        self.fields["group"].required = False
        self.fields["group"].queryset = (
            PoolGroup.objects.filter(stage__competition=competition)
            .select_related("stage")
            .order_by("stage__order", "order", "name")
        )
        entries = competition.entries.select_related("player", "team").order_by("seed", "id")
        self.fields["home_entry"].queryset = entries
        self.fields["away_entry"].queryset = entries

        self._selected_knockout_stage = bool(
            selected_stage
            and selected_stage.stage_type
            == PoolCompetitionStage.StageType.KNOCKOUT
        )
        self._knockout_rounds_configured = False
        if self._selected_knockout_stage:
            # Gameweeks are table scheduling metadata, never bracket metadata.
            self.fields.pop("gameweek")
            round_queryset = PoolKnockoutRoundDefinition.objects.filter(
                stage=selected_stage
            ).order_by("order", "id")
            if round_queryset.exists():
                self._knockout_rounds_configured = True
                self.fields["knockout_round"].queryset = round_queryset
                self.fields["knockout_round"].required = True
                self.fields["knockout_round"].label = "Round"
                self.fields["knockout_round"].help_text = (
                    "Choose the configured knockout round for this manual fixture."
                )
            else:
                self.fields.pop("knockout_round")
            # A configured round relation is authoritative. Keep the legacy
            # text field out of new manual knockout workflows; model saving
            # mirrors the chosen canonical name into it for compatibility.
            self.fields.pop("round_label")
        else:
            self.fields.pop("knockout_round")

        self._life_aware = competition_allows_two_lives(competition)
        if self._life_aware:
            # Manual fresh draws operate on lives.  The real competition entry
            # is filled from the selected life in ``clean`` so statistics keep
            # their existing entry identity without duplicate records.
            self.fields.pop("home_entry")
            self.fields.pop("away_entry")
            life_queryset = PoolCompetitionEntryLife.objects.filter(
                competition_entry__competition=competition,
                status=PoolCompetitionEntryLife.Status.ACTIVE,
            ).select_related("competition_entry__player")
            existing_life_ids = {
                life_id
                for life_id in (
                    self.instance.home_entry_life_id,
                    self.instance.away_entry_life_id,
                )
                if life_id
            }
            if existing_life_ids:
                life_queryset = PoolCompetitionEntryLife.objects.filter(
                    Q(
                        competition_entry__competition=competition,
                        status=PoolCompetitionEntryLife.Status.ACTIVE,
                    )
                    | Q(pk__in=existing_life_ids)
                ).select_related("competition_entry__player")
            life_queryset = life_queryset.order_by(
                "competition_entry__player__full_name", "life_number", "id"
            )
            for field_name, label in (
                ("home_entry_life", "Home tournament life"),
                ("away_entry_life", "Away tournament life"),
            ):
                self.fields[field_name].queryset = life_queryset
                self.fields[field_name].required = True
                self.fields[field_name].label = label
                self.fields[field_name].help_text = (
                    "Life 1 and Life 2 are separate tournament paths for the same player."
                )
        else:
            self.fields.pop("home_entry_life")
            self.fields.pop("away_entry_life")

    def _selected_stage(self, stages):
        """Resolve the stage used to scope conditional manual-fixture fields."""

        stage_id = None
        if self.is_bound:
            stage_id = self.data.get(self.add_prefix("stage"))
        if not stage_id:
            stage_id = self.initial.get("stage") or self.instance.stage_id
        if hasattr(stage_id, "pk"):
            stage_id = stage_id.pk
        if not stage_id and len(stages) == 1:
            return stages[0]
        try:
            stage_id = int(stage_id)
        except (TypeError, ValueError):
            return None
        return next((stage for stage in stages if stage.pk == stage_id), None)

    def clean(self):
        cleaned_data = super().clean()
        self.instance.competition = self.competition
        home_entry = cleaned_data.get("home_entry")
        away_entry = cleaned_data.get("away_entry")
        stage = cleaned_data.get("stage")
        group = cleaned_data.get("group")
        knockout_round = cleaned_data.get("knockout_round")
        home_field_name = "home_entry_life" if self._life_aware else "home_entry"
        away_field_name = "away_entry_life" if self._life_aware else "away_entry"
        if "table_count" in self.fields and cleaned_data.get("table_count") is None:
            cleaned_data["table_count"] = 1
            self.instance.table_count = 1

        if self._life_aware:
            home_life = cleaned_data.get("home_entry_life")
            away_life = cleaned_data.get("away_entry_life")
            if home_life:
                home_entry = home_life.competition_entry
                cleaned_data["home_entry"] = home_entry
                self.instance.home_entry = home_entry
            if away_life:
                away_entry = away_life.competition_entry
                cleaned_data["away_entry"] = away_entry
                self.instance.away_entry = away_entry
            if home_life and away_life and home_life.pk == away_life.pk:
                self.add_error(
                    "away_entry_life",
                    "The same tournament life cannot occupy both sides of a fixture.",
                )

        if home_entry and home_entry.competition_id != self.competition.id:
            self.add_error(home_field_name, "The home participant is not entered in this competition.")
        if away_entry and away_entry.competition_id != self.competition.id:
            self.add_error(away_field_name, "The away participant is not entered in this competition.")
        if home_entry and away_entry and home_entry.pk == away_entry.pk:
            self.add_error(away_field_name, "A participant cannot play itself.")
        if stage and stage.competition_id != self.competition.id:
            self.add_error("stage", "The selected stage does not belong to this competition.")
        if stage and stage.stage_type == PoolCompetitionStage.StageType.KNOCKOUT:
            if not self._knockout_rounds_configured:
                self.add_error(
                    "stage",
                    "Configure knockout rounds before creating a manual knockout fixture. The bracket manager remains available for generated brackets.",
                )
            elif not knockout_round:
                self.add_error("knockout_round", "Choose a configured knockout round.")
            elif knockout_round.stage_id != stage.id:
                self.add_error(
                    "knockout_round",
                    "The selected round does not belong to this knockout stage.",
                )
            elif PoolKnockoutBracket.objects.filter(stage=stage).exists() and (
                not self.instance.pk
                or self.instance.knockout_round_id != knockout_round.pk
            ):
                self.add_error(
                    "stage",
                    "This knockout stage is managed by its generated bracket.",
                )
        elif knockout_round:
            self.add_error(
                "knockout_round",
                "Only knockout fixtures may be assigned a configured round.",
            )
        elif stage and stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
            if self.is_bound and self.data.get(self.add_prefix("knockout_round")):
                self.add_error(
                    "stage",
                    "Only knockout fixtures may be assigned a configured round.",
                )
            self.instance.knockout_round = None
        if group:
            if not stage or group.stage_id != stage.id:
                self.add_error("group", "The selected group does not belong to this stage.")
            elif group.stage.competition_id != self.competition.id:
                self.add_error("group", "The selected group does not belong to this competition.")
            else:
                group_entry_ids = set(
                    group.memberships.values_list("entry_id", flat=True)
                )
                if home_entry and home_entry.id not in group_entry_ids:
                    self.add_error(
                        home_field_name,
                        "The home participant is not assigned to this group.",
                    )
                if away_entry and away_entry.id not in group_entry_ids:
                    self.add_error(
                        away_field_name,
                        "The away participant is not assigned to this group.",
                    )
        elif stage and stage.stage_type == PoolCompetitionStage.StageType.GROUP:
            self.add_error("group", "Select the group for a group-stage fixture.")
        elif not group:
            self.instance.group = None
        if (
            stage
            and stage.stage_type
            not in {
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            }
            and cleaned_data.get("gameweek") is not None
        ):
            self.add_error(
                "gameweek",
                "Gameweeks are available only for league or group-stage fixtures.",
            )
        return cleaned_data


class PoolStandingBaselineEntryForm(forms.Form):
    """One editable imported row in a trusted competition-table formset."""

    entry = forms.ModelChoiceField(
        queryset=PoolCompetitionEntry.objects.none(),
        widget=forms.HiddenInput(),
    )
    played = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    won = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    lost = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    racks_for = forms.IntegerField(
        min_value=0,
        label="Racks won",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    racks_against = forms.IntegerField(
        min_value=0,
        label="Racks lost",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    points = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )

    def __init__(self, *args, entries=None, **kwargs):
        super().__init__(*args, **kwargs)
        entries = entries if entries is not None else PoolCompetitionEntry.objects.none()
        if not hasattr(entries, "all"):
            # The manager intentionally materialises its ordered table rows
            # once for rendering.  ModelChoiceField still requires a queryset
            # for authoritative submitted-ID validation.
            entries = PoolCompetitionEntry.objects.filter(
                pk__in=[entry.pk for entry in entries]
            )
        self.fields["entry"].queryset = entries

    def clean(self):
        cleaned_data = super().clean()
        played = cleaned_data.get("played")
        won = cleaned_data.get("won")
        lost = cleaned_data.get("lost")
        if (
            played is not None
            and won is not None
            and lost is not None
            and won + lost > played
        ):
            self.add_error("played", "Wins plus losses cannot exceed played.")
        return cleaned_data


class BasePoolStandingBaselineFormSet(BaseFormSet):
    """Reject altered formset rows instead of trusting hidden entrant IDs."""

    def __init__(self, *args, entries, **kwargs):
        self.entries = list(entries)
        self.expected_entry_ids = {entry.pk for entry in self.entries}
        super().__init__(*args, form_kwargs={"entries": self.entries}, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        submitted_entries = [form.cleaned_data.get("entry") for form in self.forms]
        # A malformed or deliberately incomplete POST must remain a normal
        # form error, never turn the manager page into a server error.
        if any(entry is None for entry in submitted_entries):
            raise ValidationError("Every baseline row must name a valid entrant.")
        submitted_ids = [entry.pk for entry in submitted_entries]
        if len(submitted_ids) != len(self.expected_entry_ids) or set(submitted_ids) != self.expected_entry_ids:
            raise ValidationError(
                "The baseline table changed. Reload it before saving your edits."
            )
        if len(submitted_ids) != len(set(submitted_ids)):
            raise ValidationError("Each entrant may appear only once in baseline standings.")


PoolStandingBaselineFormSet = formset_factory(
    PoolStandingBaselineEntryForm,
    formset=BasePoolStandingBaselineFormSet,
    extra=0,
    max_num=512,
    validate_max=True,
)


class PoolLeagueFixtureScopeForm(forms.Form):
    """Choose one valid league or group table for fixture generation."""

    stage = forms.ModelChoiceField(
        queryset=PoolCompetitionStage.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    group = forms.ModelChoiceField(
        queryset=PoolGroup.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)
        self.fields["stage"].queryset = competition.stages.filter(
            stage_type__in=(
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            )
        ).order_by("order", "id")
        self.fields["group"].queryset = PoolGroup.objects.filter(
            stage__competition=competition,
            stage__stage_type=PoolCompetitionStage.StageType.GROUP,
        ).select_related("stage").order_by("stage__order", "order", "id")

    def clean(self):
        cleaned_data = super().clean()
        stage = cleaned_data.get("stage")
        group = cleaned_data.get("group")
        if not stage:
            return cleaned_data
        if stage.stage_type == PoolCompetitionStage.StageType.GROUP:
            if not group:
                self.add_error("group", "Select a group table for this group stage.")
            elif group.stage_id != stage.pk:
                self.add_error("group", "The selected group does not belong to this stage.")
        elif group:
            self.add_error("group", "A league stage does not use a group.")
        return cleaned_data


class PoolLeagueFixturePreviewConfirmForm(forms.Form):
    """Explicit acknowledgement before a signed fixture preview is persisted."""

    token = forms.CharField(widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        label="I have reviewed these fixtures and want to generate them.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class PoolRackResultForm(forms.Form):
    """One authoritative rack result, recorded against the current fixture.

    A singles fixture continues to select the winning competition entry because
    that entry already identifies its player.  A team fixture deliberately
    takes the opposite approach: the scorer chooses the actual player and the
    result service infers the winning team entry from that player's active,
    competition-scoped roster membership.  Keeping the two inputs mutually
    exclusive prevents a forged or accidental team/player contradiction.
    """

    winner = forms.ModelChoiceField(
        queryset=PoolCompetitionEntry.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    outcome_type = forms.ChoiceField(
        choices=tuple(
            choice
            for choice in PoolRack.OutcomeType.choices
            if choice[0] != PoolRack.OutcomeType.LINEUP_VACANCY
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    breaker = forms.ModelChoiceField(
        queryset=PoolCompetitionEntry.objects.none(),
        required=False,
        empty_label="Not recorded",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )
    request_token = forms.UUIDField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, match: PoolMatch, **kwargs):
        preserved_winning_player = kwargs.pop("preserved_winning_player", None)
        preserved_winner_entry = kwargs.pop("preserved_winner_entry", None)
        require_complete_team_rosters = kwargs.pop(
            "require_complete_team_rosters", True
        )
        self.match = match
        self.is_team_match = (
            match.competition.participant_mode
            == PoolCompetition.ParticipantMode.TEAMS
        )
        self.team_roster_ready = True
        self.missing_roster_entries = ()
        self.ambiguous_roster_players = ()
        self.roster_groups = ()
        self._winning_players_by_value = {}
        self._preserved_winning_player = preserved_winning_player
        self._preserved_winner_entry_id = getattr(
            preserved_winner_entry,
            "pk",
            preserved_winner_entry,
        )
        self._require_complete_team_rosters = require_complete_team_rosters
        self._allows_legacy_unattributed_choice = (
            not require_complete_team_rosters and preserved_winning_player is None
        )
        self.allows_legacy_unattributed_choice = (
            self._allows_legacy_unattributed_choice
        )
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.initial.get("request_token"):
            # The server persists this action ID on the resulting rack.  A
            # transport retry therefore returns that same rack rather than
            # accidentally scoring the following rack too.
            self.initial["request_token"] = uuid.uuid4()
        participants = PoolCompetitionEntry.objects.filter(
            pk__in=(match.home_entry_id, match.away_entry_id)
        ).select_related("player", "team")
        self.fields["winner"].queryset = participants
        self.fields["breaker"].queryset = participants

        if self.is_team_match:
            # Team rack scoring never exposes a second, independently chosen
            # winner entry.  The central result service owns that inference.
            self.fields.pop("winner")
            self._configure_team_winning_player_field(participants)

    def _configure_team_winning_player_field(self, participants):
        """Build safe, display-ready player choices for this one fixture.

        The form is intentionally scoped to *active* memberships of the two
        exact team entries in the match.  The service repeats all of these
        checks after locking the fixture, so this is usability rather than a
        trust boundary.
        """
        entries_by_id = {entry.pk: entry for entry in participants}
        entries = [
            entries_by_id[entry_id]
            for entry_id in (self.match.home_entry_id, self.match.away_entry_id)
            if entry_id in entries_by_id
        ]
        roster_memberships = list(
            PoolTeamRoster.objects.filter(
                competition_entry_id__in=[entry.pk for entry in entries],
                is_active=True,
            )
            .select_related("player", "competition_entry__team")
            .order_by("competition_entry_id", "sort_order", "player__full_name", "id")
        )
        memberships_by_entry = {entry.pk: [] for entry in entries}
        player_entry_ids = {}
        for membership in roster_memberships:
            memberships_by_entry.setdefault(membership.competition_entry_id, []).append(
                membership
            )
            player_entry_ids.setdefault(membership.player_id, set()).add(
                membership.competition_entry_id
            )

        ambiguous_player_ids = {
            player_id
            for player_id, entry_ids in player_entry_ids.items()
            if len(entry_ids) != 1
        }
        missing_entries = []
        groups = []
        player_lookup = {}
        for entry in entries:
            memberships = [
                membership
                for membership in memberships_by_entry.get(entry.pk, [])
                if membership.player_id not in ambiguous_player_ids
                and not (
                    self._preserved_winning_player is not None
                    and membership.player_id == self._preserved_winning_player.pk
                    and self._preserved_winner_entry_id is not None
                    and entry.pk != self._preserved_winner_entry_id
                )
            ]
            if not memberships:
                missing_entries.append(entry)
            players = []
            for membership in memberships:
                value = str(membership.player_id)
                player_lookup[value] = membership.player
                players.append(
                    {
                        "player": membership.player,
                        "value": value,
                        "input_id": self.add_prefix(
                            f"winning_player_{membership.player_id}"
                        ),
                    }
                )
            groups.append({"entry": entry, "players": players})

        # A roster can legitimately change after a rack was recorded.  A
        # correction that only changes the outcome must still be able to keep
        # that historic attribution; present it distinctly rather than making
        # it look like a currently eligible team-roster choice.  No other
        # inactive or removed player is admitted.
        preserved_player = self._preserved_winning_player
        if (
            preserved_player is not None
            and str(preserved_player.pk) not in player_lookup
        ):
            value = str(preserved_player.pk)
            player_lookup[value] = preserved_player
            groups.append(
                {
                    "entry": None,
                    "label": "Previously recorded attribution",
                    "players": [
                        {
                            "player": preserved_player,
                            "value": value,
                            "input_id": self.add_prefix(
                                f"winning_player_{preserved_player.pk}"
                            ),
                            "is_preserved": True,
                        }
                    ],
                }
            )
        elif self._allows_legacy_unattributed_choice:
            groups.append(
                {
                    "entry": None,
                    "label": "Previously recorded attribution",
                    "players": [
                        {
                            "player": None,
                            "label": "Player not recorded",
                            "value": "",
                            "input_id": self.add_prefix("winning_player_legacy"),
                            "is_preserved": True,
                        }
                    ],
                }
            )

        self._winning_players_by_value = player_lookup
        self.missing_roster_entries = tuple(missing_entries)
        ambiguous_players_by_id = {
            membership.player_id: membership.player
            for membership in roster_memberships
            if membership.player_id in ambiguous_player_ids
        }
        self.ambiguous_roster_players = tuple(
            ambiguous_players_by_id[player_id]
            for player_id in sorted(ambiguous_players_by_id)
        )
        self.team_roster_ready = not missing_entries
        selected_value = self._selected_winning_player_value()
        for group in groups:
            for player_choice in group["players"]:
                player_choice["selected"] = (
                    player_choice["value"] == selected_value
                )
        self.roster_groups = tuple(groups)
        self.fields["winning_player"] = forms.ChoiceField(
            label="Rack winner",
            choices=[
                (value, player.full_name)
                for value, player in self._winning_players_by_value.items()
            ]
            + (
                [("", "Player not recorded (legacy)")]
                if self._allows_legacy_unattributed_choice
                else []
            ),
            required=not self._allows_legacy_unattributed_choice,
            widget=forms.RadioSelect(
                attrs={"class": "pool-team-rack-player-input"}
            ),
        )

    def _selected_winning_player_value(self):
        value = (
            self.data.get(self.add_prefix("winning_player"))
            if self.is_bound
            else self.initial.get("winning_player")
        )
        if hasattr(value, "pk"):
            value = value.pk
        return str(value) if value not in (None, "") else ""

    def clean(self):
        cleaned_data = super().clean()
        if not self.is_team_match:
            return cleaned_data

        if self._require_complete_team_rosters and not self.team_roster_ready:
            self.add_error(
                None,
                "Each team needs at least one active registered player before racks can be recorded.",
            )
            return cleaned_data

        value = cleaned_data.get("winning_player")
        if not value:
            if self._allows_legacy_unattributed_choice:
                cleaned_data["winning_player"] = None
            return cleaned_data
        winning_player = self._winning_players_by_value.get(str(value))
        if winning_player is None:
            # ChoiceField normally catches this before clean(), but retaining a
            # defensive error here makes a malformed form instance harmless.
            self.add_error(
                "winning_player",
                "Select a registered player from one of the teams in this match.",
            )
            return cleaned_data
        cleaned_data["winning_player"] = winning_player
        return cleaned_data


class PoolRackCorrectionForm(PoolRackResultForm):
    """A rack correction requires a reason for the audit history."""

    reason = forms.CharField(
        required=True,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        help_text="Explain the correction for the result history.",
    )


class PoolResultCorrectionForm(forms.Form):
    reason = forms.CharField(
        required=True,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        help_text="Explain why this completed result needs correction.",
    )


class PoolNonPlayedResultForm(forms.Form):
    """Finish a fixture without manufacturing rack records."""

    result_method = forms.ChoiceField(
        choices=(
            (PoolMatch.ResultMethod.WALKOVER, PoolMatch.ResultMethod.WALKOVER.label),
            (PoolMatch.ResultMethod.WITHDRAWAL, PoolMatch.ResultMethod.WITHDRAWAL.label),
            (
                PoolMatch.ResultMethod.DISQUALIFICATION,
                PoolMatch.ResultMethod.DISQUALIFICATION.label,
            ),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    winner = forms.ModelChoiceField(
        queryset=PoolCompetitionEntry.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )

    def __init__(self, *args, match: PoolMatch, **kwargs):
        self.match = match
        super().__init__(*args, **kwargs)
        self.fields["winner"].queryset = PoolCompetitionEntry.objects.filter(
            pk__in=(match.home_entry_id, match.away_entry_id)
        ).select_related("player", "team")


class PoolManualFinalResultForm(forms.Form):
    """Record a verified offline final score without inventing rack events."""

    home_score = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    away_score = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    home_green_apples = forms.IntegerField(required=False, min_value=0, initial=0, label="Home green apples", widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}))
    away_green_apples = forms.IntegerField(required=False, min_value=0, initial=0, label="Away green apples", widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}))
    home_red_apples = forms.IntegerField(required=False, min_value=0, initial=0, label="Home red apples", widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}))
    away_red_apples = forms.IntegerField(required=False, min_value=0, initial=0, label="Away red apples", widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}))
    home_one_visits = forms.IntegerField(required=False, min_value=0, initial=0, label="Home one visits", widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}))
    away_one_visits = forms.IntegerField(required=False, min_value=0, initial=0, label="Away one visits", widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}))
    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        help_text="Optional note about the verified offline result.",
    )
    confirm = forms.BooleanField(
        label="I have verified this final score and want to publish the result.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, match: PoolMatch, **kwargs):
        self.match = match
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("home_score", match.home_score)
            self.initial.setdefault("away_score", match.away_score)
            summaries = {
                summary.entry_id: summary
                for summary in match.manual_rack_summaries.all()
            } if match.pk else {}
            for side, entry_id in (("home", match.home_entry_id), ("away", match.away_entry_id)):
                summary = summaries.get(entry_id)
                if summary:
                    for key in ("green_apples", "red_apples", "one_visits"):
                        self.initial.setdefault(f"{side}_{key}", getattr(summary, key))

    def clean(self):
        cleaned_data = super().clean()
        home_score = cleaned_data.get("home_score")
        away_score = cleaned_data.get("away_score")
        target = self.match.race_target
        if home_score is None or away_score is None:
            return cleaned_data
        if home_score == away_score:
            self.add_error(None, "A final Pool result cannot be tied.")
        elif target is None:
            self.add_error(None, "This match needs a positive race target before a final result can be entered.")
        elif max(home_score, away_score) != target:
            self.add_error(
                None,
                f"The winner must reach this fixture's race target of {target}.",
            )
        elif min(home_score, away_score) >= target:
            self.add_error(None, "The losing score must remain below the race target.")
        for side, score in (("home", home_score), ("away", away_score)):
            special_total = sum(cleaned_data.get(f"{side}_{field}") or 0 for field in ("green_apples", "red_apples", "one_visits"))
            if special_total > score:
                self.add_error(
                    None,
                    f"{side.title()} Green Apples, Red Apples, and One Visits cannot exceed that side's final score.",
                )
        return cleaned_data


class PoolMatchTransitionForm(forms.Form):
    """Optional explanatory note for postponed, cancelled, or abandoned matches."""

    reason = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )


class PoolCompetitionAdminForm(forms.Form):
    """Add an existing account as a competition administrator by exact lookup."""

    identifier = forms.CharField(
        label="Administrator email or username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "Enter exact email address or username",
            }
        ),
        help_text="The administrator must already have an active EsportsLive account.",
    )

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"].strip()
        users = (
            User.objects.filter(is_active=True)
            .filter(Q(email__iexact=identifier) | Q(username__iexact=identifier))
            .distinct()
        )
        if not users.exists():
            raise ValidationError(
                "No active EsportsLive account was found with that email or username."
            )
        if users.count() > 1:
            raise ValidationError(
                "More than one account uses this email. Enter the exact username instead."
            )

        user = users.first()
        if user.is_superuser:
            raise ValidationError("A superuser does not need a competition administrator membership.")
        if self.competition.owner_id == user.id:
            raise ValidationError("This user is already the competition owner.")

        membership = PoolCompetitionMembership.objects.filter(
            competition=self.competition,
            user=user,
        ).first()
        if membership and membership.role == PoolCompetitionMembership.Role.OWNER:
            raise ValidationError("This user is already the competition owner.")
        if membership and membership.role == PoolCompetitionMembership.Role.ADMIN and membership.is_active:
            raise ValidationError("This user is already an active competition administrator.")

        self.cleaned_data["user"] = user
        return identifier


class PoolCompetitionStandingsForm(forms.ModelForm):
    """Edit the competition-level values used by derived pool tables.

    Tie-breaker ordering is deliberately handled by
    :class:`PoolCompetitionTieBreakerForm` below.  Splitting the forms keeps a
    change to the point values from accidentally replacing an organiser's
    selected criteria.
    """

    class Meta:
        model = PoolCompetition
        fields = ("points_mode", "points_for_win", "points_for_loss")
        widgets = {
            "points_mode": forms.Select(attrs={"class": "form-select"}),
            "points_for_win": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
            "points_for_loss": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
        }

    def __init__(self, *args, display_mode=None, **kwargs):
        """Bind the compact fixed/margin presentation without changing policy.

        The GET mode picker in the manager is deliberately separate from this
        POST form.  It chooses which small, human-facing editor to show while
        this form continues to validate the eventual policy through the
        standings service.
        """
        super().__init__(*args, **kwargs)
        if display_mode not in PoolCompetition.PointsMode.values:
            display_mode = self.instance.points_mode
        self.display_mode = display_mode
        self.initial["points_mode"] = display_mode
        # A few existing integrations post only the legacy fixed-point fields.
        # Treat a missing selector as the saved mode rather than turning such a
        # request into an invalid cross-version submission.  The central
        # standings service still validates the final submitted policy.
        self.fields["points_mode"].required = False
        self.fields["points_mode"].widget = forms.HiddenInput()
        if display_mode == PoolCompetition.PointsMode.MARGIN_BASED:
            self.fields["points_for_win"].label = "Winner points"
            self.fields["points_for_loss"].label = "Loser points"
            help_text = (
                "Used for walkovers, withdrawals, and disqualifications. "
                "Played matches use the rules below."
            )
            self.fields["points_for_win"].help_text = help_text
            self.fields["points_for_loss"].help_text = help_text
        else:
            self.fields["points_for_win"].label = "Points for a win"
            self.fields["points_for_loss"].label = "Points for a loss"
            self.fields["points_for_win"].help_text = "Applied to every eligible result."
            self.fields["points_for_loss"].help_text = "Applied to every eligible result."

    def clean_points_mode(self):
        return self.cleaned_data.get("points_mode") or self.instance.points_mode

    def _post_clean(self):
        """Let the atomic policy service validate formset bands as one unit.

        Django's normal ``ModelForm`` path validates the proposed competition
        object before the matching submitted margin rows have been installed.
        Mark this short-lived form validation phase explicitly; the manager
        view immediately delegates the complete values to the transactional
        standings service, whose locked final save performs the model-level
        active-policy validation again.
        """
        self.instance._allow_pending_margin_policy = True
        try:
            super()._post_clean()
        finally:
            self.instance.__dict__.pop("_allow_pending_margin_policy", None)


class PoolMarginPointThresholdForm(forms.Form):
    """One human-facing points card, not a persisted range row.

    A manager chooses only each upper threshold.  The formset derives every
    lower bound centrally and requires the last card to finish at the actual
    maximum table winning margin; no card represents a fictional ``or more``
    result beyond the configured race target.
    """

    up_to_margin = forms.IntegerField(
        required=False,
        min_value=1,
        label="Up to",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )
    winner_points = forms.IntegerField(
        required=False,
        min_value=0,
        label="Winner points",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )
    loser_points = forms.IntegerField(
        required=False,
        min_value=0,
        label="Loser points",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 0}),
    )

    def __init__(
        self,
        *args,
        is_final=False,
        range_start=1,
        maximum_margin=1,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.is_final = is_final
        self.range_start = range_start
        self.maximum_margin = maximum_margin
        self.threshold_prefix = (
            "Win by up to" if range_start == 1 else f"Win by {range_start} to"
        )
        self.final_label = f"Win by {range_start} to {maximum_margin} racks"
        self.fields["up_to_margin"].widget.attrs["max"] = maximum_margin
        if is_final:
            # The terminal threshold is a presentation of the applicable
            # race target, not a separate organiser decision.  Read-only
            # controls still submit their value, so this works without JS.
            self.fields["up_to_margin"].widget.attrs["readonly"] = "readonly"


class BasePoolMarginPointThresholdFormSet(BaseFormSet):
    """Validate compact manager cards and derive authoritative stored ranges."""

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        self.derived_rules = ()
        from .standings import maximum_meaningful_pool_margin

        self.maximum_margin = maximum_meaningful_pool_margin(competition)
        super().__init__(*args, **kwargs)

    def _submitted_threshold(self, index):
        if self.is_bound:
            value = self.data.get(f"{self.prefix}-{index}-up_to_margin")
        elif index < len(self.initial):
            value = self.initial[index].get("up_to_margin")
        else:
            value = None
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _submitted_for_deletion(self, index):
        """Read a checkbox before normal form cleaning for display context."""

        if not self.is_bound:
            return False
        value = self.data.get(f"{self.prefix}-{index}-DELETE")
        return value not in (None, "", False, "0", "false", "False")

    def _construct_form(self, index, **kwargs):
        total = self.total_form_count()
        range_start = 1
        for previous_index in range(index):
            if self._submitted_for_deletion(previous_index):
                continue
            threshold = self._submitted_threshold(previous_index)
            if threshold is None or threshold < range_start:
                break
            range_start = threshold + 1
        kwargs["is_final"] = index == total - 1
        kwargs["range_start"] = range_start
        kwargs["maximum_margin"] = self.maximum_margin
        return super()._construct_form(index, **kwargs)

    def clean(self):
        super().clean()
        if any(
            form.errors
            for index, form in enumerate(self.forms)
            if not self._submitted_for_deletion(index)
        ):
            return

        from .standings import derive_pool_margin_point_rules_from_thresholds

        # This is intentionally a non-model formset.  Reject old raw model
        # fields and forged IDs instead of silently accepting a cross-
        # competition object identifier that the compact editor never emits.
        if self.is_bound:
            forbidden_suffixes = ("-id", "-minimum_margin", "-maximum_margin")
            if any(
                key.startswith(f"{self.prefix}-") and key.endswith(forbidden_suffixes)
                for key in self.data
            ):
                raise ValidationError(
                    "Played-match points must be submitted through the simplified rule cards."
                )

        if self.total_form_count() < 1:
            raise ValidationError(
                "Provide at least one played-match points rule."
            )

        thresholds = []
        deleted_count = 0
        final_index = self.total_form_count() - 1
        for index, form in enumerate(self.forms):
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                if index == final_index:
                    form.add_error(
                        "DELETE",
                        "The final convincing-result rule is required.",
                    )
                else:
                    deleted_count += 1
                continue

            up_to_margin = form.cleaned_data.get("up_to_margin")
            winner_points = form.cleaned_data.get("winner_points")
            loser_points = form.cleaned_data.get("loser_points")
            if winner_points is None:
                form.add_error("winner_points", "Enter winner points.")
            if loser_points is None:
                form.add_error("loser_points", "Enter loser points.")
            if up_to_margin is None:
                form.add_error("up_to_margin", "Enter the highest winning margin for this rule.")
            elif index == final_index and up_to_margin != self.maximum_margin:
                form.add_error(
                    "up_to_margin",
                    "The final rule must end at the maximum meaningful winning margin "
                    f"of {self.maximum_margin}.",
                )

            if form.errors:
                continue
            thresholds.append(
                {
                    "up_to_margin": up_to_margin,
                    "winner_points": winner_points,
                    "loser_points": loser_points,
                }
            )

        if any(form.errors for form in self.forms):
            return
        if not thresholds:
            raise ValidationError("Keep at least one played-match points rule.")
        if len(thresholds) != self.total_form_count() - deleted_count:
            raise ValidationError(
                "Every displayed played-match rule must be completed."
            )
        self.derived_rules = derive_pool_margin_point_rules_from_thresholds(
            self.competition,
            thresholds,
        )


_DEFAULT_MARGIN_THRESHOLD_FORM_LIMIT = 1000


def PoolMarginPointThresholdFormSet(*args, competition: PoolCompetition, **kwargs):
    """Build a safely bounded formset without imposing a policy-rule limit.

    The model intentionally has no arbitrary maximum number of margin bands.
    A configured historical policy must therefore remain editable even when it
    contains more rules than a typical new competition.  The generated
    formset permits all existing rules plus one progressive card, while a
    generous bound protects a fresh manager endpoint from an attacker asking
    Django to instantiate unbounded forms.
    """

    from .standings import maximum_meaningful_pool_margin

    initial = kwargs.get("initial") or ()
    persisted_count = PoolCompetitionMarginPointRule.objects.filter(
        competition=competition
    ).count()
    maximum_margin = maximum_meaningful_pool_margin(competition)
    maximum_cards = max(
        min(_DEFAULT_MARGIN_THRESHOLD_FORM_LIMIT, maximum_margin),
        persisted_count + 1,
        len(initial),
    )
    formset_class = formset_factory(
        PoolMarginPointThresholdForm,
        formset=BasePoolMarginPointThresholdFormSet,
        can_delete=True,
        extra=0,
        max_num=maximum_cards,
        validate_max=True,
        absolute_max=maximum_cards,
    )
    return formset_class(*args, competition=competition, **kwargs)


def margin_point_threshold_initial(
    competition: PoolCompetition,
    *,
    requested_rule_count: int | None = None,
) -> list[dict]:
    """Return compact cards from a valid stored policy or a safe two-card draft.

    Existing ranges remain the database source of truth.  A damaged imported
    policy deliberately falls back to a repairable two-card draft so the
    manager page never needs to expose raw ``from``/``to`` internals just to
    recover from bad historical data.
    """

    from .standings import (
        maximum_meaningful_pool_margin,
        validate_pool_margin_point_rules,
    )

    maximum_margin = maximum_meaningful_pool_margin(competition)

    raw_rules = list(
        PoolCompetitionMarginPointRule.objects.filter(competition=competition).order_by(
            "minimum_margin", "id"
        )
    )
    initial: list[dict] = []
    try:
        rules = validate_pool_margin_point_rules(
            competition,
            raw_rules,
            require_complete=False,
            allow_legacy_open_ended=True,
        )
    except ValidationError:
        rules = ()

    if rules:
        initial = [
            {
                "up_to_margin": (
                    maximum_margin
                    if rule.maximum_margin is None
                    else rule.maximum_margin
                ),
                "winner_points": rule.winner_points,
                "loser_points": rule.loser_points,
            }
            for rule in rules
        ]
        last_threshold = initial[-1]["up_to_margin"]
        if last_threshold < maximum_margin:
            # A race target may have changed since a formerly valid finite
            # policy was saved.  Do not invent an allocation for the newly
            # possible margin: render a required, blank repair card instead.
            initial.append(
                {
                    "up_to_margin": maximum_margin,
                    "winner_points": None,
                    "loser_points": None,
                }
            )
    else:
        # A new margin policy starts as a valid, deliberately conservative
        # draft: it mirrors the current fixed allocation until the organiser
        # explicitly changes the card values and saves.
        if maximum_margin == 1:
            initial = [
                {
                    "up_to_margin": 1,
                    "winner_points": competition.points_for_win,
                    "loser_points": competition.points_for_loss,
                }
            ]
        else:
            initial = [
                {
                    "up_to_margin": min(3, maximum_margin - 1),
                    "winner_points": competition.points_for_win,
                    "loser_points": competition.points_for_loss,
                },
                {
                    "up_to_margin": maximum_margin,
                    "winner_points": competition.points_for_win,
                    "loser_points": competition.points_for_loss,
                },
            ]

    requested_rule_count = requested_rule_count or len(initial)
    try:
        requested_rule_count = int(requested_rule_count)
    except (TypeError, ValueError):
        requested_rule_count = len(initial)
    # Existing policies may legitimately contain more bands than a new
    # manager would normally add.  Preserve every stored card and permit one
    # additional server-rendered draft card, while retaining a generous DoS
    # guard for a forged GET count.
    maximum_requested_count = max(
        len(initial),
        min(_DEFAULT_MARGIN_THRESHOLD_FORM_LIMIT, maximum_margin),
    )
    requested_rule_count = max(1, min(requested_rule_count, maximum_requested_count))
    while len(initial) < requested_rule_count:
        # Split the latest available range.  Prefer the range nearest the
        # terminal card, but gracefully use an earlier expandable range when
        # the last two thresholds are consecutive.  This stays deterministic
        # and never creates duplicate or beyond-target thresholds.
        previous_threshold = 0
        insertion_index = None
        insertion_threshold = None
        for index, card in enumerate(initial):
            threshold = card.get("up_to_margin")
            if isinstance(threshold, int) and threshold - previous_threshold > 1:
                insertion_index = index
                insertion_threshold = previous_threshold + 1
            if isinstance(threshold, int):
                previous_threshold = threshold
        if insertion_index is None or insertion_threshold is None:
            break
        split_card = initial[insertion_index]
        initial.insert(
            insertion_index,
            {
                "up_to_margin": insertion_threshold,
                "winner_points": split_card.get(
                    "winner_points", competition.points_for_win
                ),
                "loser_points": split_card.get(
                    "loser_points", competition.points_for_loss
                ),
            },
        )
    return initial


# Backwards-compatible import name for scoped manager integrations.  It now
# represents threshold cards rather than raw model rows.
PoolCompetitionMarginPointRuleFormSet = PoolMarginPointThresholdFormSet


class PoolCompetitionTieBreakerForm(forms.Form):
    """Configure up to four unique criteria after competition table points."""

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)
        configured = list(
            competition.tie_breakers.order_by("order", "id").values_list(
                "criterion", flat=True
            )
        )
        choices = (("", "Not used"), *PoolCompetitionTieBreaker.Criterion.choices)
        for number in range(1, 5):
            self.fields[f"criterion_{number}"] = forms.ChoiceField(
                choices=choices,
                required=False,
                label=f"Tie-breaker {number}",
                initial=configured[number - 1] if len(configured) >= number else "",
                widget=forms.Select(attrs={"class": "form-select"}),
            )

    def clean(self):
        cleaned_data = super().clean()
        criteria = [
            cleaned_data.get(f"criterion_{number}")
            for number in range(1, 5)
            if cleaned_data.get(f"criterion_{number}")
        ]
        if len(criteria) != len(set(criteria)):
            raise ValidationError("Choose each tie-breaker criterion only once.")
        cleaned_data["criteria"] = criteria
        return cleaned_data


class PoolGroupForm(forms.ModelForm):
    """Create or edit a group within one already-authorised group stage."""

    class Meta:
        model = PoolGroup
        fields = ("name", "order", "qualifiers")
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "off"}
            ),
            "order": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "qualifiers": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
        }

    def __init__(self, *args, stage: PoolCompetitionStage, **kwargs):
        self.stage = stage
        super().__init__(*args, **kwargs)
        self.instance.stage = stage
        if not self.instance.pk and not self.is_bound:
            highest_order = stage.groups.aggregate(highest=Max("order"))["highest"]
            self.fields["order"].initial = (highest_order or 0) + 1

    def clean(self):
        cleaned_data = super().clean()
        self.instance.stage = self.stage
        if self.stage.stage_type != PoolCompetitionStage.StageType.GROUP:
            raise ValidationError("Groups can only be configured for a group stage.")
        name = cleaned_data.get("name")
        order = cleaned_data.get("order")
        existing_groups = self.stage.groups.all()
        if self.instance.pk:
            existing_groups = existing_groups.exclude(pk=self.instance.pk)
        if name and existing_groups.filter(name=name).exists():
            self.add_error("name", "A group with this name already exists in this stage.")
        if order is not None and existing_groups.filter(order=order).exists():
            self.add_error("order", "A group with this order already exists in this stage.")
        qualifiers = cleaned_data.get("qualifiers")
        if self.instance.pk and qualifiers is not None:
            highest_rule_position = (
                self.instance.qualification_rules.order_by("-position")
                .values_list("position", flat=True)
                .first()
            )
            if highest_rule_position and qualifiers < highest_rule_position:
                self.add_error(
                    "qualifiers",
                    "This cannot be lower than an existing qualification rule position.",
                )
        return cleaned_data


class PoolGroupEntryForm(forms.ModelForm):
    """Add one competition entry to a selected group without cross-stage reuse."""

    class Meta:
        model = PoolGroupEntry
        fields = ("entry", "sort_order")
        widgets = {
            "entry": forms.Select(attrs={"class": "form-select"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, group: PoolGroup, **kwargs):
        self.group = group
        self.stage = group.stage
        super().__init__(*args, **kwargs)
        self.instance.group = group
        self.instance.stage = self.stage
        assigned_entry_ids = PoolGroupEntry.objects.filter(stage=self.stage)
        if self.instance.pk:
            assigned_entry_ids = assigned_entry_ids.exclude(pk=self.instance.pk)
        self.fields["entry"].queryset = (
            self.stage.competition.entries.exclude(
                pk__in=assigned_entry_ids.values("entry_id")
            )
            .select_related("player", "team")
            .order_by("seed", "id")
        )

    def clean(self):
        cleaned_data = super().clean()
        self.instance.group = self.group
        self.instance.stage = self.stage
        entry = cleaned_data.get("entry")
        if entry and entry.competition_id != self.stage.competition_id:
            self.add_error("entry", "The entry must belong to this competition.")
        return cleaned_data


class PoolGroupBulkEntryForm(forms.Form):
    """Validate a basket of eligible competition entries for one group.

    Group membership is unique across a whole group stage, not merely within
    one group.  The client-side basket improves selection speed, but this
    form remains the first authoritative scope/eligibility check before the
    view repeats it inside its write transaction.
    """

    entry_ids = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, group: PoolGroup, **kwargs):
        self.group = group
        self.stage = group.stage
        self.selected_entries = ()
        super().__init__(*args, **kwargs)

    def _submitted_entry_ids(self):
        """Read the basket, retaining compatibility with the old single form."""

        field_name = self.add_prefix("entry_ids")
        if hasattr(self.data, "getlist"):
            values = self.data.getlist(field_name)
        else:
            raw_values = self.data.get(field_name, [])
            values = (
                list(raw_values)
                if isinstance(raw_values, (list, tuple))
                else [raw_values]
            )
        if not any(str(value).strip() for value in values):
            legacy_value = self.data.get(self.add_prefix("entry"), "")
            if str(legacy_value).strip():
                values = [legacy_value]
        return values

    def clean(self):
        cleaned_data = super().clean()
        submitted_values = self._submitted_entry_ids()
        entry_ids = []
        invalid_values = []
        for value in submitted_values:
            value = str(value).strip()
            if not value:
                continue
            try:
                entry_id = int(value)
            except (TypeError, ValueError):
                invalid_values.append(value)
                continue
            if entry_id < 1:
                invalid_values.append(value)
                continue
            entry_ids.append(entry_id)

        if not entry_ids:
            self.add_error("entry_ids", "Select at least one competition entry.")
            return cleaned_data
        if invalid_values:
            self.add_error("entry_ids", "One or more selected participants are invalid.")
            return cleaned_data
        if len(set(entry_ids)) != len(entry_ids):
            self.add_error("entry_ids", "Select each participant only once.")
            return cleaned_data

        entries_by_id = {
            entry.pk: entry
            for entry in PoolCompetitionEntry.objects.select_related("player", "team").filter(
                pk__in=entry_ids
            )
        }
        missing_entry_ids = [
            entry_id for entry_id in entry_ids if entry_id not in entries_by_id
        ]
        if missing_entry_ids:
            self.add_error("entry_ids", "One or more selected participants no longer exist.")
            return cleaned_data

        selected_entries = [entries_by_id[entry_id] for entry_id in entry_ids]
        self.selected_entries = tuple(selected_entries)
        if any(entry.competition_id != self.stage.competition_id for entry in selected_entries):
            self.add_error(
                "entry_ids",
                "Every selected participant must belong to this competition.",
            )
            return cleaned_data

        assigned_entry_ids = set(
            PoolGroupEntry.objects.filter(
                stage=self.stage,
                entry_id__in=entry_ids,
            ).values_list("entry_id", flat=True)
        )
        if assigned_entry_ids:
            assigned_names = [
                entry.display_name
                for entry in selected_entries
                if entry.pk in assigned_entry_ids
            ]
            self.add_error(
                "entry_ids",
                "These participants are already assigned to this group stage: "
                + ", ".join(assigned_names[:3])
                + ("." if len(assigned_names) <= 3 else ", and others."),
            )
            return cleaned_data

        cleaned_data["entry_ids"] = entry_ids
        cleaned_data["entries"] = selected_entries
        return cleaned_data


class PoolGroupEntryOrderForm(forms.ModelForm):
    """Adjust a group's display order without moving its participant."""

    class Meta:
        model = PoolGroupEntry
        fields = ("sort_order",)
        widgets = {
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }


class PoolGroupQualificationRuleForm(forms.ModelForm):
    """Configure a group finishing position into a same-competition bracket."""

    class Meta:
        model = PoolGroupQualificationRule
        fields = ("group", "knockout_stage", "position", "destination_slot", "keep_groups_apart")
        widgets = {
            "group": forms.Select(attrs={"class": "form-select"}),
            "knockout_stage": forms.Select(attrs={"class": "form-select"}),
            "position": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "destination_slot": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
            "keep_groups_apart": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, competition: PoolCompetition, source_stage: PoolCompetitionStage, **kwargs):
        self.competition = competition
        self.source_stage = source_stage
        super().__init__(*args, **kwargs)
        self.instance.source_stage = source_stage
        self.fields["group"].queryset = source_stage.groups.order_by("order", "name")
        self.fields["knockout_stage"].queryset = competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
            order__gt=source_stage.order,
        ).order_by("order", "name")

    def clean(self):
        cleaned_data = super().clean()
        self.instance.source_stage = self.source_stage
        group = cleaned_data.get("group")
        knockout_stage = cleaned_data.get("knockout_stage")
        position = cleaned_data.get("position")
        if self.source_stage.stage_type != PoolCompetitionStage.StageType.GROUP:
            raise ValidationError("Qualification rules require a group stage.")
        if group and group.stage_id != self.source_stage.id:
            self.add_error("group", "The group must belong to the selected source stage.")
        if knockout_stage:
            if knockout_stage.competition_id != self.competition.id:
                self.add_error(
                    "knockout_stage", "The destination must belong to this competition."
                )
            elif knockout_stage.order <= self.source_stage.order:
                self.add_error(
                    "knockout_stage", "The knockout destination must follow the group stage."
                )
        if group and position and position > group.qualifiers:
            self.add_error(
                "position",
                "This position exceeds the number of qualifiers configured for the group.",
            )
        if knockout_stage and PoolKnockoutBracket.objects.filter(
            competition=self.competition,
            stage=knockout_stage,
        ).exists():
            self.add_error(
                "knockout_stage",
                "Qualification rules cannot be changed after this bracket is generated.",
            )
        return cleaned_data


class PoolBracketDrawForm(forms.Form):
    """Build a server-validated draw-preview request for one knockout stage.

    The individual manual-slot fields are generated from the selected bracket
    size.  Their querysets are always competition scoped, so an altered POST
    cannot introduce an entry from another event.
    """

    BRACKET_SIZES = (2, 4, 8, 16, 32, 64, 128, 256)
    LARGE_MANUAL_DRAW_THRESHOLD = 64

    stage = forms.ModelChoiceField(
        queryset=PoolCompetitionStage.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    draw_method = forms.ChoiceField(
        choices=PoolBracketDrawPreview.DrawMethod.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    bracket_size = forms.TypedChoiceField(
        choices=tuple((size, f"{size}-entry bracket") for size in BRACKET_SIZES),
        coerce=int,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    third_place_enabled = forms.BooleanField(
        required=False,
        label="Include a third-place match",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    confirm_random_draw = forms.BooleanField(
        required=False,
        label="I understand that the random draw is final and cannot be redrawn.",
        help_text=(
            "The entrant positions are generated only after confirmation and "
            "are revealed only after the official bracket is saved."
        ),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        self.life_aware = competition_allows_two_lives(competition)
        self.large_manual_slots = False
        self.large_manual_slot_fields = ()
        self.small_manual_slot_fields = ()
        self.configuration_fields = ()
        self.manual_slot_options = ()
        self.manual_top_life_slot_options = ()
        self.manual_bottom_life_slot_options = ()
        self.manual_half_size = 0
        # Retain the original presentation attribute for existing templates
        # while using the neutral name below for both ordinary entries and
        # tournament-life draws.
        self.life_half_size = 0
        super().__init__(*args, **kwargs)
        knockout_stages = competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        ).order_by("order", "name")
        self.fields["stage"].queryset = knockout_stages

        default_size = self._default_size()
        requested_size = self._requested_size(default_size)
        self.selected_size = requested_size
        requested_draw_method = (
            self.data.get("draw_method")
            if self.is_bound
            else self.initial.get(
                "draw_method", PoolBracketDrawPreview.DrawMethod.MANUAL
            )
        )
        self.is_random_request = (
            requested_draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
        )
        if knockout_stages.count() == 1:
            self.fields["stage"].initial = knockout_stages.first()
        self.fields["draw_method"].initial = PoolBracketDrawPreview.DrawMethod.MANUAL
        self.fields["bracket_size"].initial = default_size
        self.fields["third_place_enabled"].initial = competition.third_place_match_enabled

        entries = competition.entries.select_related("player", "team").order_by(
            "seed", "id"
        )
        self.large_manual_slots = (
            requested_size >= self.LARGE_MANUAL_DRAW_THRESHOLD
        )
        self.manual_half_size = requested_size // 2
        self.life_half_size = self.manual_half_size
        self._life_queryset = PoolCompetitionEntryLife.objects.none()
        if self.life_aware:
            # Group qualification is deliberately life-free and therefore
            # unavailable in this direct singles-knockout mode.  Seeded,
            # manual, and random orders all keep Life 1 in the top half and
            # Life 2 in the bottom half.
            blocked_methods = {
                PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            }
            self.fields["draw_method"].choices = tuple(
                choice
                for choice in PoolBracketDrawPreview.DrawMethod.choices
                if choice[0] not in blocked_methods
            )
            self.fields["draw_method"].help_text = (
                "Life 1 is drawn only in the top half and Life 2 only in the "
                "bottom half."
            )
            self._life_queryset = (
                PoolCompetitionEntryLife.objects.filter(
                    competition_entry__competition=competition,
                )
                .select_related("competition_entry__player")
                .order_by(
                    "competition_entry__seed",
                    "competition_entry_id",
                    "life_number",
                    "id",
                )
            )
            life_options = list(self._life_queryset)
            self.manual_slot_options = tuple(
                {
                    "id": life.pk,
                    "label": life.display_name,
                    "search_label": f"{life.display_name} (#{life.pk})",
                    "life_number": life.life_number,
                }
                for life in life_options
            )
            self.manual_top_life_slot_options = tuple(
                option
                for option in self.manual_slot_options
                if option["life_number"] == 1
            )
            self.manual_bottom_life_slot_options = tuple(
                option
                for option in self.manual_slot_options
                if option["life_number"] == 2
            )
        else:
            # Large ordinary manual draws use the same deliberately selected,
            # hidden-ID workflow as life-aware draws.  Rendering a 64–256
            # item native select for every slot is both unusable and easy to
            # mis-select; ModelChoiceField still validates every posted ID
            # against this competition-scoped queryset on the server.
            self.manual_slot_options = tuple(
                {
                    "id": entry.pk,
                    "label": entry.display_name,
                    "search_label": f"{entry.display_name} (entry #{entry.pk})",
                }
                for entry in entries
            )

        for slot in range(1, requested_size + 1):
            if self.life_aware:
                expected_life_number = 1 if slot <= self.life_half_size else 2
                self.fields[f"slot_{slot}"] = forms.ModelChoiceField(
                    queryset=self._life_queryset.filter(
                        life_number=expected_life_number
                    ),
                    required=False,
                    empty_label="Bye / unused slot",
                    label=f"Tournament life slot {slot}",
                    widget=(
                        forms.HiddenInput()
                        if self.large_manual_slots
                        or requested_draw_method != PoolBracketDrawPreview.DrawMethod.MANUAL
                        else forms.Select(
                            attrs={
                                "class": "form-select",
                                "data-pool-life-slot": str(slot),
                                "data-pool-life-number": str(expected_life_number),
                            }
                        )
                    ),
                )
                continue
            self.fields[f"slot_{slot}"] = forms.ModelChoiceField(
                queryset=entries,
                required=False,
                empty_label="Bye / unused slot",
                label=f"Entry slot {slot}",
                    widget=(
                        forms.HiddenInput()
                        if self.large_manual_slots
                        or requested_draw_method != PoolBracketDrawPreview.DrawMethod.MANUAL
                    else forms.Select(attrs={"class": "form-select"})
                ),
            )

        if self.large_manual_slots:
            self.large_manual_slot_fields = tuple(
                self[f"slot_{slot}"] for slot in range(1, requested_size + 1)
            )
        else:
            self.small_manual_slot_fields = tuple(
                self[f"slot_{slot}"] for slot in range(1, requested_size + 1)
            )

        round_count = self._round_count(requested_size)
        for round_number in range(1, round_count + 1):
            self.fields[f"round_race_target_{round_number}"] = forms.IntegerField(
                min_value=1,
                initial=competition.default_race_target,
                label=f"{self._round_label(round_number, round_count)} race target",
                widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            )
        self.fields["third_place_race_target"] = forms.IntegerField(
            min_value=1,
            required=False,
            initial=competition.default_race_target,
            label="Third-place match race target",
            widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        )
        configuration_field_names = (
            "stage",
            "draw_method",
            "bracket_size",
            "third_place_enabled",
            "confirm_random_draw",
            *(
                f"round_race_target_{round_number}"
                for round_number in range(1, round_count + 1)
            ),
            "third_place_race_target",
        )
        self.configuration_fields = tuple(
            self[field_name] for field_name in configuration_field_names
        )

    def _default_size(self):
        if getattr(self, "life_aware", False):
            life_numbers = list(
                PoolCompetitionEntryLife.objects.filter(
                    competition_entry__competition=self.competition,
                ).values_list("life_number", flat=True)
            )
            # The two paths are structural halves, rather than merely a
            # sibling-separation preference.  Reserve enough slots for the
            # larger numbered-life population in its own half.
            entry_count = max(
                len(life_numbers),
                life_numbers.count(1) * 2,
                life_numbers.count(2) * 2,
            )
        else:
            entry_count = self.competition.entries.count()
        for size in self.BRACKET_SIZES:
            if entry_count <= size:
                return size
        return self.BRACKET_SIZES[-1]

    def _requested_size(self, default_size):
        # An unbound manager GET may deliberately supply initial draw
        # configuration so the page can rebuild its manual slot fields.  Do
        # not bind that GET as a submission: missing required fields must not
        # become validation errors merely because the bracket size changed.
        value = (
            self.data.get("bracket_size")
            if self.is_bound
            else self.initial.get("bracket_size")
        )
        try:
            value = int(value) if value else default_size
        except (TypeError, ValueError):
            value = default_size
        return value if value in self.BRACKET_SIZES else default_size

    @staticmethod
    def _round_count(size):
        return size.bit_length() - 1

    @staticmethod
    def _round_label(round_number, round_count):
        remaining = 2 ** (round_count - round_number + 1)
        if remaining == 2:
            return "Final"
        if remaining == 4:
            return "Semi-final"
        if remaining == 8:
            return "Quarter-final"
        if remaining in {16, 32, 64, 128, 256}:
            return f"Round of {remaining}"
        return f"Round {round_number}"

    def clean(self):
        cleaned_data = super().clean()
        stage = cleaned_data.get("stage")
        draw_method = cleaned_data.get("draw_method")
        bracket_size = cleaned_data.get("bracket_size")
        if stage:
            if stage.competition_id != self.competition.id:
                self.add_error("stage", "The stage must belong to this competition.")
            elif stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
                self.add_error("stage", "Choose a knockout stage for the bracket.")

        if bracket_size not in self.BRACKET_SIZES:
            self.add_error("bracket_size", "Choose a supported bracket size.")
        if (
            draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
            and not cleaned_data.get("confirm_random_draw")
        ):
            self.add_error(
                "confirm_random_draw",
                "Confirm that this one-shot random draw is final before continuing.",
            )
        entrant_count = (
            self._life_queryset.count()
            if self.life_aware
            else self.competition.entries.count()
        )
        required_life_slots = None
        if self.life_aware:
            life_numbers = list(
                self._life_queryset.values_list("life_number", flat=True)
            )
            required_life_slots = max(
                len(life_numbers),
                life_numbers.count(1) * 2,
                life_numbers.count(2) * 2,
            )
        if (
            entrant_count > self.BRACKET_SIZES[-1]
            or (
                required_life_slots is not None
                and required_life_slots > self.BRACKET_SIZES[-1]
            )
        ):
            self.add_error(
                "bracket_size",
                "This competition has more than 256 eligible entrants or tournament-life half slots.",
            )
        elif self.life_aware and bracket_size != self._default_size():
            self.add_error(
                "bracket_size",
                "Use the supported bracket size required to keep Life 1 and Life 2 in separate halves.",
            )
        elif cleaned_data.get("third_place_enabled") and bracket_size < 4:
            self.add_error(
                "third_place_enabled",
                "A third-place match requires a bracket with at least four entrants.",
            )

        draw_order = []
        manual_life_ids = None
        if (
            self.life_aware
            and draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION
        ):
            self.add_error(
                "draw_method",
                "Two-life knockout draws do not support group qualification placement.",
            )
        if draw_method == PoolBracketDrawPreview.DrawMethod.MANUAL:
            if self.life_aware:
                selected_lives = []
                manual_life_ids = []
                for slot in range(1, self.selected_size + 1):
                    life = cleaned_data.get(f"slot_{slot}")
                    manual_life_ids.append(life.pk if life else None)
                    if life:
                        selected_lives.append(life)
                        expected_life_number = (
                            1 if slot <= self.selected_size // 2 else 2
                        )
                        if life.life_number != expected_life_number:
                            self.add_error(
                                f"slot_{slot}",
                                f"This bracket half accepts only Life {expected_life_number}.",
                            )
                selected_ids = [life.pk for life in selected_lives]
                if len(selected_ids) < 2:
                    raise ValidationError("A manual bracket needs at least two tournament lives.")
                if len(selected_ids) != len(set(selected_ids)):
                    raise ValidationError(
                        "A tournament life can appear in only one manual bracket slot."
                    )
                eligible_ids = set(
                    self._life_queryset.values_list("pk", flat=True)
                )
                if set(selected_ids) != eligible_ids:
                    raise ValidationError(
                        "A two-life manual draw must place every eligible tournament life exactly once."
                    )
                draw_order = [
                    life.competition_entry_id if life else None
                    for life in (
                        cleaned_data.get(f"slot_{slot}")
                        for slot in range(1, self.selected_size + 1)
                    )
                ]
            else:
                selected_ids = []
                for slot in range(1, self.selected_size + 1):
                    entry = cleaned_data.get(f"slot_{slot}")
                    draw_order.append(entry.pk if entry else None)
                    if entry:
                        selected_ids.append(entry.pk)
                if len(selected_ids) < 2:
                    raise ValidationError("A manual bracket needs at least two entrants.")
                if len(selected_ids) != len(set(selected_ids)):
                    raise ValidationError("An entry can appear in only one manual bracket slot.")
        elif draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
            if stage and not PoolGroupQualificationRule.objects.filter(
                knockout_stage=stage,
                source_stage__competition=self.competition,
            ).exists():
                raise ValidationError(
                    "Configure at least one group qualification rule before previewing this draw."
                )
        else:
            draw_order = []

        round_race_targets = {}
        for round_number in range(1, self._round_count(self.selected_size) + 1):
            target = cleaned_data.get(f"round_race_target_{round_number}")
            if target:
                round_race_targets[str(round_number)] = target
        if cleaned_data.get("third_place_enabled"):
            third_place_target = cleaned_data.get("third_place_race_target")
            if not third_place_target:
                self.add_error(
                    "third_place_race_target",
                    "Set a race target for the third-place match.",
                )
            else:
                round_race_targets["third_place"] = third_place_target

        cleaned_data["draw_order"] = draw_order
        cleaned_data["manual_life_ids"] = manual_life_ids
        cleaned_data["round_race_targets"] = round_race_targets
        return cleaned_data


class PoolBracketPreviewConfirmForm(forms.Form):
    """Explicit PRG confirmation for a user-scoped saved draw preview."""

    token = forms.UUIDField(widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        label="I have reviewed this draw and want to generate the bracket.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class PoolBracketHandoffForm(forms.Form):
    """Choose the completed manual round that supplies a new bracket field."""

    stage = forms.ModelChoiceField(
        queryset=PoolCompetitionStage.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    source_round = forms.ModelChoiceField(
        label="Completed manual round",
        queryset=PoolKnockoutRoundDefinition.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text=(
            "The selected round must be complete, finalised, and have no later "
            "manual fixtures. Its winners become the bracket field."
        ),
    )
    draw_method = forms.ChoiceField(
        choices=(
            (PoolBracketDrawPreview.DrawMethod.SEEDED, "Seeded draw (review first)"),
            (PoolBracketDrawPreview.DrawMethod.RANDOM, "Random draw (final immediately)"),
        ),
        initial=PoolBracketDrawPreview.DrawMethod.SEEDED,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    confirm_random_draw = forms.BooleanField(
        required=False,
        label="I understand that the random draw is final and cannot be redrawn.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, competition: PoolCompetition, **kwargs):
        self.competition = competition
        super().__init__(*args, **kwargs)
        stages = competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        ).order_by("order", "name", "id")
        self.fields["stage"].queryset = stages
        self.fields["source_round"].queryset = (
            PoolKnockoutRoundDefinition.objects.filter(stage__in=stages)
            .select_related("stage")
            .order_by("stage__order", "order", "id")
        )

    def clean(self):
        cleaned_data = super().clean()
        stage = cleaned_data.get("stage")
        source_round = cleaned_data.get("source_round")
        draw_method = cleaned_data.get("draw_method")
        if source_round and stage and source_round.stage_id != stage.pk:
            self.add_error(
                "source_round",
                "Choose a completed manual round from the selected knockout stage.",
            )
        if (
            draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
            and not cleaned_data.get("confirm_random_draw")
        ):
            self.add_error(
                "confirm_random_draw",
                "Confirm that this one-shot random draw is final before continuing.",
            )
        return cleaned_data


class PoolBracketResetForm(forms.Form):
    """Require an explicit acknowledgement before a permitted bracket reset."""

    confirm = forms.BooleanField(
        label="I understand that this removes the generated bracket and its untouched fixtures.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class PoolBracketRoundTargetsForm(forms.Form):
    """Edit persisted bracket-round targets without exposing arbitrary rounds."""

    def __init__(self, *args, bracket: PoolKnockoutBracket, **kwargs):
        self.bracket = bracket
        super().__init__(*args, **kwargs)
        for rule in bracket.round_rules.order_by("round_number", "is_third_place", "id"):
            label = rule.name
            if rule.is_third_place:
                label = f"{label} (third place)"
            self.fields[f"round_rule_{rule.pk}"] = forms.IntegerField(
                min_value=1,
                label=f"{label} race target",
                initial=rule.race_target,
                widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            )

    def round_targets(self):
        """Return only the persisted bracket rule IDs represented by this form."""
        return {
            int(field_name.removeprefix("round_rule_")): value
            for field_name, value in self.cleaned_data.items()
            if field_name.startswith("round_rule_")
        }
