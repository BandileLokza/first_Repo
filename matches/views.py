import logging
import uuid
from functools import wraps

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.db import IntegrityError, OperationalError, transaction
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.core.exceptions import PermissionDenied, ValidationError

from .models import Match, MatchLineup, MatchLineupEntry, Goal, YellowCard, Player
from .forms import (
    GoalEntryForm,
    QuickFinalResultForm,
    MatchCreateForm,
    MatchResultForm,
    TeamLineupForm,
    CardEntryForm,
    StartFirstHalfForm,
)
from teams.models import Team
from leagues.models import Competition, Standing
from leagues.standings import rank_standings

from league_manager.models import ActivityLog
from league_manager.permissions import (
    can_correct_finished_result,
    can_control_match,
    can_manage_abandonment_decisions,
    can_manage_team_lineup,
    can_resolve_abandoned_match,
    can_unlock_team_lineups,
)

from .services import (
    end_extra_time,
    end_extra_time_first_half,
    end_regulation,
    finalize_match_result_fast,
    finish_penalty_shootout,
    finish_ready_match,
    save_quick_goal_details as save_quick_goal_details_service,
    validate_result_against_recorded_goals,
    recalculate_card_types,
    resolve_card_player,
    start_extra_time,
    start_extra_time_second_half,
    start_penalty_shootout,
)
from .control_state import get_match_control_state
from .result_corrections import (
    RESULT_CONFLICT,
    is_lock_conflict,
    result_snapshot,
)
from .bracket import competition_has_bracket, record_blocked_bracket_correction
from .knockout import aggregate_display, assign_match_to_knockout_tie

from .result_outbox import RESULT_BUSY, RESULT_SAVED, lock_result_competition, lock_result_match

logger = logging.getLogger(__name__)


def log_match_activity(
    *,
    user,
    match,
    action,
    description="",
    old_values=None,
    new_values=None,
):
    ActivityLog.objects.create(
        actor=user,
        competition=match.competition,
        match=match,
        action=action,
        description=description,
        old_values=old_values or {},
        new_values=new_values or {},
    )

def match_detail(request, pk):
    match = get_object_or_404(
        Match.objects.select_related(
            "competition__series",
            "stage",
            "group",
            "home_team",
            "away_team",
            "knockout_tie",
        ),
        pk=pk,
    )

    show_table = match.stage.stage_type in {
        "LEAGUE",
        "GROUP",
    }
    home_lineup = MatchLineup.objects.prefetch_related(
        "entries__player",
        "players",
    ).filter(
        match=match,
        team=match.home_team
    ).first()

    away_lineup = MatchLineup.objects.prefetch_related(
        "entries__player",
        "players",
    ).filter(
        match=match,
        team=match.away_team
    ).first()

    home_starting_entries = (
        home_lineup.entries.filter(role=MatchLineupEntry.Role.STARTER)
        if home_lineup else []
    )
    home_substitute_entries = (
        home_lineup.entries.filter(role=MatchLineupEntry.Role.SUBSTITUTE)
        if home_lineup else []
    )
    away_starting_entries = (
        away_lineup.entries.filter(role=MatchLineupEntry.Role.STARTER)
        if away_lineup else []
    )
    away_substitute_entries = (
        away_lineup.entries.filter(role=MatchLineupEntry.Role.SUBSTITUTE)
        if away_lineup else []
    )

    goals = Goal.objects.filter(match=match, canceled=False)
    cards = YellowCard.objects.filter(match=match).select_related("player", "team").order_by('minute', 'id')

    # A late agent may enter an earlier event after a newer one. ``id`` keeps
    # events in a deterministic order when they share a minute.
    home_goals = goals.filter(team=match.home_team).order_by('minute', 'id')
    away_goals = goals.filter(team=match.away_team).order_by('minute', 'id')

    standings = rank_standings(
        match.competition,
        Standing.objects.filter(
            competition=match.competition,
            stage=match.stage,
            group=match.group,
        ).select_related("team"),
    )

    can_manage = can_control_match(
        request.user,
        match,
    )
    can_quick_goal_details = (
        match.abandonment_resolution not in {
            Match.AbandonmentResolution.AWARDED,
            Match.AbandonmentResolution.CONFIRMED,
        }
        and (
            can_correct_finished_result(request.user, match)
            or request.session.get("quick_result_match_id") == match.id
        )
    )
    quick_goal_count = match.home_score + match.away_score
    can_manage_home_lineup = can_manage_team_lineup(
        request.user,
        match,
        match.home_team,
    )
    can_manage_away_lineup = can_manage_team_lineup(
        request.user,
        match,
        match.away_team,
    )
    current_abandonment_decision = (
        match.abandonment_decisions.filter(is_current=True)
        .select_related("replay_match")
        .first()
    )
    return render(request, 'matches/match_detail.html', {
        'match': match,
        'league': match.competition,
        'home_lineup': home_lineup,
        'away_lineup': away_lineup,
        'home_starting_entries': home_starting_entries,
        'home_substitute_entries': home_substitute_entries,
        'away_starting_entries': away_starting_entries,
        'away_substitute_entries': away_substitute_entries,
        'home_goals': home_goals,
        'away_goals': away_goals,
        'standings': standings,
        'cards': cards,
        'can_manage': can_manage,
        'can_manage_home_lineup': can_manage_home_lineup,
        'can_manage_away_lineup': can_manage_away_lineup,
        'can_resolve_abandoned_match': not current_abandonment_decision and can_resolve_abandoned_match(request.user, match),
        'current_abandonment_decision': current_abandonment_decision,
        'can_manage_abandoned_decision': bool(
            current_abandonment_decision
            and can_manage_abandonment_decisions(request.user, match.competition)
        ),
        'can_unlock_lineups': can_unlock_team_lineups(
            request.user,
            match,
        ),
        "show_table": show_table,
        "control_state": get_match_control_state(match),
        "aggregate_summary": aggregate_display(match),
        "team_profiles_available": match.competition.format_type == "TABLE",
        "quick_result_form": QuickFinalResultForm(),
        "can_quick_goal_details": can_quick_goal_details,
        "show_quick_goal_details": (
            request.GET.get("quick-goals") == "1"
            and can_quick_goal_details
            and match.status == "FINISHED"
            and quick_goal_count > 0
            and not match.goals.filter(canceled=False).exists()
        ),
        "quick_home_goal_rows": range(match.home_score),
        "quick_away_goal_rows": range(match.away_score),
        "quick_home_players": Player.objects.filter(team=match.home_team).order_by("name", "id"),
        "quick_away_players": Player.objects.filter(team=match.away_team).order_by("name", "id"),
    })


def _retry_busy_result(view):
    @wraps(view)
    def wrapped(request, match_id, *args, **kwargs):
        try:
            return view(request, match_id, *args, **kwargs)
        except OperationalError as error:
            if not is_lock_conflict(error):
                raise
            messages.warning(request, RESULT_BUSY)
            return redirect("match_detail", pk=match_id)
    return wrapped


def _locked_result_or_404(match_id):
    try:
        return lock_result_match(match_id, nowait=True)
    except Match.DoesNotExist as error:
        raise Http404("Match not found.") from error


@login_required
@require_POST
@_retry_busy_result
@transaction.atomic
def save_quick_result(request, match_id):
    match = _locked_result_or_404(match_id)
    if not can_control_match(request.user, match):
        return HttpResponseForbidden("You cannot control this match.")

    form = QuickFinalResultForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter whole-number final scores of zero or more.")
        return redirect("match_detail", pk=match.id)
    old_values = {
        "status": match.status,
        "home_score": match.home_score,
        "away_score": match.away_score,
    }
    try:
        validate_result_against_recorded_goals(
            match,
            home_score=form.cleaned_data["home_score"],
            away_score=form.cleaned_data["away_score"],
        )
        # This is the same official finalisation service used by League
        # Manager's existing Update Results form.
        match = finalize_match_result_fast(
            match,
            home_score=form.cleaned_data["home_score"],
            away_score=form.cleaned_data["away_score"],
            regulation_home_score=form.cleaned_data["home_score"],
            regulation_away_score=form.cleaned_data["away_score"],
            actor=request.user,
        )
    except ValidationError as error:
        record_blocked_bracket_correction(error, actor=request.user)
        messages.error(request, " ".join(error.messages))
        return redirect("match_detail", pk=match.id)

    log_match_activity(
        user=request.user,
        match=match,
        action=ActivityLog.Action.MATCH_FINISHED,
        description=(f"Quick final result saved: {match.home_team} {match.home_score}-"
                     f"{match.away_score} {match.away_team}."),
        old_values=old_values,
        new_values={"status": match.status, "home_score": match.home_score,
                    "away_score": match.away_score, "entry_method": "QUICK_RESULT"},
    )
    request.session["quick_result_match_id"] = match.id
    messages.success(request, RESULT_SAVED)
    destination = redirect("match_detail", pk=match.id).url
    if match.home_score + match.away_score:
        destination += "?quick-goals=1"
    return redirect(destination)


@login_required
@require_POST
@transaction.atomic
def save_quick_goal_details(request, match_id):
    match = get_object_or_404(
        Match.objects.select_for_update(of=("self",)).select_related(
            "competition", "stage", "home_team", "away_team"
        ),
        pk=match_id,
    )
    if not (
        can_correct_finished_result(request.user, match)
        or request.session.get("quick_result_match_id") == match.id
    ):
        return HttpResponseForbidden("You cannot add goal details for this match.")

    rows = []
    for prefix, team_id, count in (
        ("home", match.home_team_id, match.home_score),
        ("away", match.away_team_id, match.away_score),
    ):
        for index in range(count):
            raw_player_id = request.POST.get(f"{prefix}-player-{index}", "").strip()
            new_player_name = request.POST.get(
                f"{prefix}-new-player-{index}", ""
            ).strip()
            if raw_player_id == "__new__":
                if not new_player_name:
                    messages.error(request, "Enter the new player's name.")
                    return redirect(
                        f"{redirect('match_detail', pk=match.id).url}?quick-goals=1"
                    )
                raw_player_id = ""
            try:
                player_id = int(raw_player_id) if raw_player_id else None
            except ValueError:
                messages.error(request, "Invalid scorer selected.")
                return redirect(f"{redirect('match_detail', pk=match.id).url}?quick-goals=1")
            rows.append({
                "team_id": team_id,
                "player_id": player_id,
                "new_player_name": new_player_name,
                "goal_type": request.POST.get(
                    f"{prefix}-goal-type-{index}", Goal.GoalType.NORMAL
                ),
            })
    try:
        save_quick_goal_details_service(match, rows, actor=request.user)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect(f"{redirect('match_detail', pk=match.id).url}?quick-goals=1")

    request.session.pop("quick_result_match_id", None)
    messages.success(request, "Goal details saved.")
    return redirect("match_detail", pk=match.id)


@login_required
def dashboard(request):
    return redirect("league_list")


@login_required
def add_player(request, team_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Not allowed")

    team = get_object_or_404(Team, pk=team_id)
    if team.competition.is_archived and not request.user.is_superuser:
        return HttpResponseForbidden(
            "Archived season — historical data is read-only."
        )

    # This legacy route remains URL-active, so keep it on the same upload-aware
    # form as the league-manager workflow instead of leaving a second player
    # creation implementation behind.
    from league_manager.forms import PlayerForm

    form = PlayerForm(
        request.POST or None,
        request.FILES or None,
        league=team.competition,
        initial={"team": team, "starting_goals": 0},
    )
    form.fields["team"].disabled = True

    if request.method == "POST" and form.is_valid():
        player = form.save(commit=False)
        player.team = team
        player.save()
        return redirect('dashboard')

    return render(
        request,
        'matches/add_player.html',
        {
            'team': team,
            'form': form,
        },
    )


@login_required
def add_lineup(request, match_id, team_id):
    """Submit, edit or view a lineup for the current coach's own team."""
    match = get_object_or_404(
        Match.objects.select_related(
            "competition",
            "home_team",
            "away_team",
        ),
        pk=match_id,
    )

    if team_id not in {
        match.home_team_id,
        match.away_team_id,
    }:
        return HttpResponseForbidden(
            "This team is not part of this match."
        )

    team = get_object_or_404(
        Team,
        pk=team_id,
        competition=match.competition,
    )

    if not can_manage_team_lineup(request.user, match, team):
        return HttpResponseForbidden(
            "You are not assigned as a coach for this team."
        )

    lineup = (
        MatchLineup.objects
        .prefetch_related("entries__player", "players")
        .filter(match=match, team=team)
        .first()
    )

    if match.lineups_locked:
        if request.method == "POST":
            return HttpResponseForbidden("Lineups are locked.")

        starting_entries = (
            lineup.entries.filter(role=MatchLineupEntry.Role.STARTER)
            if lineup else []
        )
        substitute_entries = (
            lineup.entries.filter(role=MatchLineupEntry.Role.SUBSTITUTE)
            if lineup else []
        )

        return render(
            request,
            "matches/add_lineup.html",
            {
                "match": match,
                "team": team,
                "lineup": lineup,
                "starting_entries": starting_entries,
                "substitute_entries": substitute_entries,
                "is_locked": True,
            },
        )

    initial = {}
    if lineup:
        entries = list(lineup.entries.all())
        if entries:
            initial = {
                "starting_players": [
                    entry.player_id
                    for entry in entries
                    if entry.role == MatchLineupEntry.Role.STARTER
                ],
                "substitute_players": [
                    entry.player_id
                    for entry in entries
                    if entry.role == MatchLineupEntry.Role.SUBSTITUTE
                ],
            }
        else:
            # Existing pre-feature lineups are treated as a legacy starting XI
            # until the data migration has represented them as entries.
            initial["starting_players"] = list(
                lineup.players.values_list("id", flat=True)[:11]
            )

    form = TeamLineupForm(
        request.POST or None,
        team=team,
        initial=initial,
    )

    if request.method == "POST" and form.is_valid():
        starters = list(form.cleaned_data["starting_players"])
        substitutes = list(form.cleaned_data["substitute_players"])

        with transaction.atomic():
            lineup, created = MatchLineup.objects.get_or_create(
                match=match,
                team=team,
                defaults={
                    "submitted_by": request.user,
                    "submitted_at": timezone.now(),
                    "updated_by": request.user,
                },
            )

            if not created:
                update_fields = ["updated_by", "updated_at"]
                lineup.updated_by = request.user
                if lineup.submitted_at is None:
                    lineup.submitted_at = timezone.now()
                    lineup.submitted_by = request.user
                    update_fields.extend(["submitted_at", "submitted_by"])
                lineup.save(update_fields=update_fields)

            lineup.entries.all().delete()
            entries = [
                MatchLineupEntry(
                    lineup=lineup,
                    player=player,
                    role=MatchLineupEntry.Role.STARTER,
                    sort_order=index,
                )
                for index, player in enumerate(starters, start=1)
            ]
            entries.extend(
                MatchLineupEntry(
                    lineup=lineup,
                    player=player,
                    role=MatchLineupEntry.Role.SUBSTITUTE,
                    sort_order=index,
                )
                for index, player in enumerate(substitutes, start=1)
            )
            MatchLineupEntry.objects.bulk_create(entries)
            # Keep the legacy relationship populated for existing integrations.
            lineup.players.set([*starters, *substitutes])

        messages.success(
            request,
            (
                f"{team.name} lineup submitted."
                if created
                else f"{team.name} lineup updated."
            ),
        )

        return redirect(
            "match_detail",
            pk=match.id,
        )

    return render(
        request,
        "matches/add_lineup.html",
        {
            "match": match,
            "team": team,
            "lineup": lineup,
            "form": form,
            "is_locked": False,
        },
    )


@login_required
@require_POST
def unlock_lineups(request, match_id):
    match = get_object_or_404(
        Match.objects.select_related("competition"),
        pk=match_id,
    )

    if not can_unlock_team_lineups(request.user, match):
        return HttpResponseForbidden("You cannot unlock lineups for this match.")

    if match.lineups_locked:
        match.unlock_lineups()
        messages.success(request, "Lineups unlocked. Team coaches can edit again.")
    else:
        messages.info(request, "Lineups are already unlocked.")

    return redirect("match_detail", pk=match.id)

def get_teams_by_league(request):
    league_id = request.GET.get("league_id")

    if not league_id:
        return JsonResponse([], safe=False)

    teams = Team.objects.filter(competition_id=league_id).values("id", "name")

    return JsonResponse(list(teams), safe=False)


@login_required
@transaction.atomic
def add_goal(request, match_id, team_id):
    match = get_object_or_404(
        Match.objects.select_for_update().select_related(
            "competition",
            "home_team",
            "away_team",
        ),
        pk=match_id,
    )

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if not get_match_control_state(match).can_add_goal:
        return HttpResponseBadRequest(
            "Goals can only be added while a football period is running."
        )

    if team_id not in {
        match.home_team_id,
        match.away_team_id,
    }:
        return HttpResponseForbidden(
            "This team is not part of this match."
        )

    team = get_object_or_404(
        Team,
        pk=team_id,
        competition=match.competition,
    )

    data = request.POST.copy() if request.method == "POST" else None
    # Forms rendered by this release always carry a stable key.  Supplying one
    # at the edge also keeps old server-rendered integrations functional.
    if data is not None and not data.get("idempotency_key"):
        data["idempotency_key"] = str(uuid.uuid4())
    form = GoalEntryForm(
        data,
        match=match,
        benefiting_team=team,
    )

    if request.method == "POST" and form.is_valid():
        # Check before resolving/creating an unregistered scorer.  The locked
        # match serialises same-match retries; the unique database constraint
        # is the authoritative backstop for every deployment database.
        idempotency_key = form.cleaned_data["idempotency_key"]
        replay = Goal.objects.filter(idempotency_key=idempotency_key).first()
        if replay:
            if replay.match_id != match.id:
                return HttpResponseBadRequest("This submission token belongs to another match.")
            return redirect("match_detail", pk=match.id)
        player = form.resolve_player()
        player_name = player.name if player else ""
        goal_type = form.cleaned_data["goal_type"]

        old_score = {
            "home_score": match.home_score,
            "away_score": match.away_score,
            "regulation_home_score": match.regulation_home_score,
            "regulation_away_score": match.regulation_away_score,
        }

        goal = Goal.objects.create(
            match=match,
            team=team,
            player=player,
            player_name=player_name,
            goal_type=goal_type,
            minute=form.cleaned_data["minute"],
            idempotency_key=idempotency_key,
        )

        is_regulation_goal = goal.period in {
            Goal.Period.FIRST_HALF,
            Goal.Period.SECOND_HALF,
        }
        update_fields = {"home_score", "away_score"}
        if team_id == match.home_team_id:
            match.home_score += 1
            if is_regulation_goal and match.regulation_home_score is not None:
                match.regulation_home_score += 1
                update_fields.add("regulation_home_score")
        else:
            match.away_score += 1
            if is_regulation_goal and match.regulation_away_score is not None:
                match.regulation_away_score += 1
                update_fields.add("regulation_away_score")

        match.save(update_fields=update_fields)

        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.GOAL_ADDED,
            description=(
                f"{goal.get_goal_type_display()} added for {team.name}: "
                f"{player_name or 'Scorer not recorded'} at {goal.minute}'"
            ),
            old_values=old_score,
            new_values={
                "home_score": match.home_score,
                "away_score": match.away_score,
                "regulation_home_score": match.regulation_home_score,
                "regulation_away_score": match.regulation_away_score,
                "team": team.name,
                "player": player_name,
                "player_id": goal.player_id,
                "player_team_id": player.team_id if player else None,
                "goal_type": goal.goal_type,
                "minute": goal.minute,
                "period": goal.period,
            },
        )

        return redirect(
            "match_detail",
            pk=match.id,
        )

    return render(
        request,
        "matches/add_goal.html",
        {
            "match": match,
            "team": team,
            "opposing_team": form.opposing_team,
            "form": form,
        },
    )
@login_required
@transaction.atomic
def add_card(request, match_id, card_kind, initial_team_id=None):
    """Add a yellow or direct-red event through one match-derived form."""
    match = get_object_or_404(
        Match.objects.select_for_update().select_related(
            "competition",
            "home_team",
            "away_team",
        ),
        pk=match_id,
    )

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if not get_match_control_state(match).can_add_goal:
        return HttpResponseBadRequest(
            "Cards can only be added while a football period is running."
        )

    if card_kind not in {"yellow", "red"}:
        return HttpResponseBadRequest("Unknown card type.")

    if initial_team_id and initial_team_id not in {match.home_team_id, match.away_team_id}:
        return HttpResponseForbidden("This team is not part of this match.")

    data = request.POST.copy() if request.method == "POST" else None
    if data is not None and initial_team_id and not data.get("team"):
        data["team"] = str(initial_team_id)
    if data is not None and not data.get("idempotency_key"):
        data["idempotency_key"] = str(uuid.uuid4())
    form = CardEntryForm(
        data,
        match=match,
        initial={"team": initial_team_id} if initial_team_id else None,
    )

    if request.method == "POST" and form.is_valid():
        idempotency_key = form.cleaned_data["idempotency_key"]
        replay = YellowCard.objects.filter(idempotency_key=idempotency_key).first()
        if replay:
            if replay.match_id != match.id:
                return HttpResponseBadRequest("This submission token belongs to another match.")
            return redirect("match_detail", pk=match.id)

        team = form.cleaned_data["team"]
        player = resolve_card_player(
            match=match,
            team_id=team.id,
            player=form.cleaned_data.get("player"),
            new_player_name=form.cleaned_data.get("player_name", ""),
        )
        card = YellowCard.objects.create(
            match=match,
            team=team,
            player=player,
            minute=form.cleaned_data["minute"] if form.cleaned_data["minute"] is not None else match.get_current_minute(),
            card_type=(
                YellowCard.CardType.DIRECT_RED
                if card_kind == "red"
                else YellowCard.CardType.YELLOW
            ),
            idempotency_key=idempotency_key,
        )
        if card_kind == "yellow":
            recalculate_card_types(match, player.id)
            card.refresh_from_db()

        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.CARD_ADDED,
            description=(
                f"{card.get_card_type_display()} added for "
                f"{card.player.name} ({team.name})"
            ),
            new_values={
                "team": team.name,
                "player": card.player.name,
                "minute": card.minute,
                "card_type": card.card_type,
            },
        )

        return redirect(
            "match_detail",
            pk=match.id,
        )

    return render(
        request,
        "matches/add_yellow_card.html",
        {
            "form": form,
            "match": match,
            "card_kind": card_kind,
            "legacy_team_id": initial_team_id,
        },
    )


@login_required
@transaction.atomic
def add_yellow_card(request, match_id, team_id):
    """Compatibility route for existing bookmarks and integrations."""
    return add_card(request, match_id, "yellow", initial_team_id=team_id)


@login_required
@transaction.atomic
def edit_card(request, card_id):
    card = get_object_or_404(
        YellowCard.objects.select_for_update().select_related(
            "match__competition", "match__home_team", "match__away_team", "team", "player"
        ),
        pk=card_id,
    )
    match = card.match
    if not (can_control_match(request.user, match) or can_correct_finished_result(request.user, match)):
        return HttpResponseForbidden("You cannot correct cards for this match.")

    form = CardEntryForm(
        request.POST or None,
        match=match,
        initial={"team": card.team_id, "player": card.player_id, "minute": card.minute},
    )
    if request.method == "POST" and form.is_valid():
        old_player_id = card.player_id
        team = form.cleaned_data["team"]
        player = resolve_card_player(
            match=match,
            team_id=team.id,
            player=form.cleaned_data.get("player"),
            new_player_name=form.cleaned_data.get("player_name", ""),
        )
        card.team = team
        card.player = player
        card.minute = form.cleaned_data["minute"] if form.cleaned_data["minute"] is not None else card.minute
        card.save(update_fields=["team", "player", "minute"])
        if card.card_type != YellowCard.CardType.DIRECT_RED:
            recalculate_card_types(match, old_player_id)
            if old_player_id != player.id:
                recalculate_card_types(match, player.id)
        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.CARD_EDITED,
            description=f"Card corrected for {player.name} ({team.name})",
            new_values={"card_id": card.id, "player_id": player.id, "team_id": team.id, "minute": card.minute},
        )
        return redirect("match_detail", pk=match.id)
    return render(request, "matches/add_yellow_card.html", {
        "form": form, "match": match, "card_kind": "red" if card.card_type == YellowCard.CardType.DIRECT_RED else "yellow", "editing_card": card,
    })


@login_required
@require_POST
@transaction.atomic
def cancel_card(request, card_id):
    card = get_object_or_404(
        YellowCard.objects.select_for_update().select_related("match__competition"),
        pk=card_id,
    )
    match = card.match
    if not (can_control_match(request.user, match) or can_correct_finished_result(request.user, match)):
        return HttpResponseForbidden("You cannot correct cards for this match.")
    if not card.canceled:
        card.canceled = True
        card.save(update_fields=["canceled"])
        if card.card_type != YellowCard.CardType.DIRECT_RED:
            recalculate_card_types(match, card.player_id)
        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.CARD_CANCELLED,
            description=f"Card cancelled for {card.player.name} ({card.team.name})",
            new_values={"card_id": card.id, "card_type": card.card_type},
        )
    return redirect("match_detail", pk=match.id)

@login_required
@require_POST
@transaction.atomic
def cancel_goal(request, goal_id):
    goal = get_object_or_404(
        Goal.objects.select_for_update().select_related(
            "match",
            "match__competition",
            "match__home_team",
            "match__away_team",
            "team",
        ),
        pk=goal_id,
    )

    match = goal.match

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if match.phase == "PENALTIES":
        return HttpResponseBadRequest(
            "Goals cannot be changed while a penalty shootout is in progress."
        )

    if not goal.canceled:
        old_score = {
            "home_score": match.home_score,
            "away_score": match.away_score,
            "regulation_home_score": match.regulation_home_score,
            "regulation_away_score": match.regulation_away_score,
        }

        goal.canceled = True
        goal.save(update_fields=["canceled"])

        is_regulation_goal = goal.period in {
            Goal.Period.FIRST_HALF,
            Goal.Period.SECOND_HALF,
        }
        update_fields = {"home_score", "away_score"}
        if goal.team_id == match.home_team_id:
            match.home_score = max(
                match.home_score - 1,
                0,
            )
            if is_regulation_goal and match.regulation_home_score is not None:
                match.regulation_home_score = max(
                    match.regulation_home_score - 1,
                    0,
                )
                update_fields.add("regulation_home_score")
        else:
            match.away_score = max(
                match.away_score - 1,
                0,
            )
            if is_regulation_goal and match.regulation_away_score is not None:
                match.regulation_away_score = max(
                    match.regulation_away_score - 1,
                    0,
                )
                update_fields.add("regulation_away_score")

        match.save(update_fields=update_fields)

        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.GOAL_CANCELLED,
            description=(
                f"{goal.get_goal_type_display()} cancelled: "
                f"{goal.player_name} ({goal.team.name})"
            ),
            old_values=old_score,
            new_values={
                "home_score": match.home_score,
                "away_score": match.away_score,
                "regulation_home_score": match.regulation_home_score,
                "regulation_away_score": match.regulation_away_score,
                "goal_id": goal.id,
                "goal_type": goal.goal_type,
            },
        )

    return redirect(
        "match_detail",
        pk=match.id,
    )

@login_required
@transaction.atomic
def edit_goal(request, goal_id):
    goal = get_object_or_404(
        Goal.objects.select_for_update().select_related(
            "match",
            "match__competition",
            "team",
        ),
        pk=goal_id,
    )

    match = goal.match

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if match.phase == "PENALTIES":
        return HttpResponseBadRequest(
            "Goals cannot be changed while a penalty shootout is in progress."
        )

    data = request.POST.copy() if request.method == "POST" else None
    if data is not None and not data.get("idempotency_key"):
        data["idempotency_key"] = str(uuid.uuid4())
    form = GoalEntryForm(
        data,
        match=match,
        benefiting_team=goal.team,
        goal=goal,
    )

    if request.method == "POST" and form.is_valid():
        player = form.resolve_player()
        old_values = {
            "player_id": goal.player_id,
            "player_name": goal.player_name,
            "goal_type": goal.goal_type,
        }

        goal.player = player
        goal.player_name = player.name if player else ""
        goal.goal_type = form.cleaned_data["goal_type"]
        if form.cleaned_data["minute"] is not None:
            goal.minute = form.cleaned_data["minute"]
        goal.save(update_fields=["player", "player_name", "goal_type", "minute"])

        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.GOAL_EDITED,
            description=(
                f"Goal corrected to {goal.get_goal_type_display().lower()}: "
                f"{goal.player_name}"
            ),
            old_values=old_values,
            new_values={
                "player_id": goal.player_id,
                "player_name": goal.player_name,
                "goal_type": goal.goal_type,
                "minute": goal.minute,
            },
        )

        return redirect(
            "match_detail",
            pk=match.id,
        )

    return render(
        request,
        "matches/edit_goal.html",
        {
            "goal": goal,
            "match": match,
            "team": goal.team,
            "opposing_team": form.opposing_team,
            "form": form,
        },
    )

@login_required
@require_POST
@transaction.atomic
def start_first_half(request, match_id):
    match = get_object_or_404(
        Match.objects.select_for_update(of=("self",)).select_related("competition"),
        pk=match_id,
    )

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if not get_match_control_state(match).can_start_first_half:
        return HttpResponseBadRequest("The first half cannot start now.")

    form = StartFirstHalfForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Enter a valid first-half match minute.")

    were_lineups_locked = match.lineups_locked
    current_minute = form.cleaned_data["current_minute"]
    match.start_first_half(current_minute=current_minute)

    if match.lineups_locked and not were_lineups_locked:
        messages.info(
            request,
            "Match is LIVE. Team lineups are now locked.",
        )

    log_match_activity(
        user=request.user,
        match=match,
        action=ActivityLog.Action.FIRST_HALF_STARTED,
        description="First half started.",
        new_values={
            "status": match.status,
            "phase": match.phase,
            "current_minute": current_minute,
        },
    )

    return redirect(
        "match_detail",
        pk=match.id,
    )


@login_required
@require_POST
def end_first_half(request, match_id):
    match = get_object_or_404(
        Match.objects.select_related("competition"),
        pk=match_id,
    )

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if not get_match_control_state(match).can_end_first_half:
        return HttpResponseBadRequest("The first half is not running.")

    match.end_first_half()

    log_match_activity(
        user=request.user,
        match=match,
        action=ActivityLog.Action.FIRST_HALF_ENDED,
        description="First half ended.",
        new_values={
            "status": match.status,
            "phase": match.phase,
            "minute": match.minute,
        },
    )

    return redirect(
        "match_detail",
        pk=match.id,
    )


@login_required
@require_POST
def start_second_half(request, match_id):
    match = get_object_or_404(
        Match.objects.select_related("competition"),
        pk=match_id,
    )

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if not get_match_control_state(match).can_start_second_half:
        return HttpResponseBadRequest("The second half cannot start now.")

    match.start_second_half()

    log_match_activity(
        user=request.user,
        match=match,
        action=ActivityLog.Action.SECOND_HALF_STARTED,
        description="Second half started.",
        new_values={
            "status": match.status,
            "phase": match.phase,
        },
    )

    return redirect(
        "match_detail",
        pk=match.id,
    )


@login_required
@require_POST
@_retry_busy_result
@transaction.atomic
def end_match(request, match_id):
    match = _locked_result_or_404(match_id)

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    old_values = {
        "status": match.status,
        "phase": match.phase,
        "home_score": match.home_score,
        "away_score": match.away_score,
    }

    state = get_match_control_state(match)
    try:
        if state.can_end_regulation:
            match = end_regulation(match, actor=request.user)
        elif state.can_finish_match:
            match = finish_ready_match(match, actor=request.user)
        else:
            return HttpResponseBadRequest(
                state.unavailable_reason or "This match cannot be finished now."
            )
    except ValidationError as error:
        record_blocked_bracket_correction(error, actor=request.user)
        return HttpResponseBadRequest(" ".join(error.messages))

    log_match_activity(
        user=request.user,
        match=match,
        action=(
            ActivityLog.Action.MATCH_FINISHED
            if match.status == "FINISHED"
            else ActivityLog.Action.REGULATION_ENDED
        ),
        description=(
            f"{'Match finished' if match.status == 'FINISHED' else 'Regulation ended'}: "
            f"{match.home_team} "
            f"{match.home_score}-{match.away_score} "
            f"{match.away_team}"
        ),
        old_values=old_values,
        new_values={
            "status": match.status,
            "phase": match.phase,
            "home_score": match.home_score,
            "away_score": match.away_score,
            "regulation_home_score": match.regulation_home_score,
            "regulation_away_score": match.regulation_away_score,
        },
    )
    if match.status == "FINISHED":
        messages.success(request, RESULT_SAVED)


    return redirect(
        "match_detail",
        pk=match.id,
    )


def _get_controlled_match(request, match_id):
    match = _locked_result_or_404(match_id)
    if not can_control_match(request.user, match):
        return None, HttpResponseForbidden("You cannot control this match.")
    return match, None


@_retry_busy_result
@transaction.atomic
def _run_knockout_transition(request, match_id, transition, action, description):
    match, denied = _get_controlled_match(request, match_id)
    if denied:
        return denied
    old_values = {
        "status": match.status,
        "phase": match.phase,
        "home_score": match.home_score,
        "away_score": match.away_score,
    }
    try:
        match = transition(match, actor=request.user)
    except ValidationError as error:
        record_blocked_bracket_correction(error, actor=request.user)
        return HttpResponseBadRequest(" ".join(error.messages))

    log_match_activity(
        user=request.user,
        match=match,
        action=action,
        description=description,
        old_values=old_values,
        new_values={
            "status": match.status,
            "phase": match.phase,
            "home_score": match.home_score,
            "away_score": match.away_score,
        },
    )
    if match.status == "FINISHED":
        messages.success(request, RESULT_SAVED)
    return redirect("match_detail", pk=match.id)


@login_required
@require_POST
def start_extra_time_view(request, match_id):
    return _run_knockout_transition(
        request,
        match_id,
        start_extra_time,
        ActivityLog.Action.EXTRA_TIME_STARTED,
        "Extra time started.",
    )


@login_required
@require_POST
def end_extra_time_first_half_view(request, match_id):
    return _run_knockout_transition(
        request,
        match_id,
        end_extra_time_first_half,
        ActivityLog.Action.EXTRA_TIME_FIRST_HALF_ENDED,
        "The first half of extra time ended.",
    )


@login_required
@require_POST
def start_extra_time_second_half_view(request, match_id):
    return _run_knockout_transition(
        request,
        match_id,
        start_extra_time_second_half,
        ActivityLog.Action.EXTRA_TIME_SECOND_HALF_STARTED,
        "The second half of extra time started.",
    )


@login_required
@require_POST
def end_extra_time_view(request, match_id):
    return _run_knockout_transition(
        request,
        match_id,
        end_extra_time,
        ActivityLog.Action.EXTRA_TIME_ENDED,
        "Extra time ended.",
    )


@login_required
@require_POST
def start_penalties_view(request, match_id):
    return _run_knockout_transition(
        request,
        match_id,
        start_penalty_shootout,
        ActivityLog.Action.PENALTIES_STARTED,
        "Penalty shootout started.",
    )


@login_required
@require_POST
@_retry_busy_result
@transaction.atomic
def finish_penalties_view(request, match_id):
    match, denied = _get_controlled_match(request, match_id)
    if denied:
        return denied
    try:
        home_penalties = int(request.POST.get("penalty_home_score", ""))
        away_penalties = int(request.POST.get("penalty_away_score", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Enter both penalty shootout scores.")

    old_values = {
        "status": match.status,
        "phase": match.phase,
        "penalty_home_score": match.penalty_home_score,
        "penalty_away_score": match.penalty_away_score,
    }
    try:
        match = finish_penalty_shootout(
            match,
            home_penalties,
            away_penalties,
            actor=request.user,
        )
    except ValidationError as error:
        record_blocked_bracket_correction(error, actor=request.user)
        return HttpResponseBadRequest(" ".join(error.messages))

    log_match_activity(
        user=request.user,
        match=match,
        action=ActivityLog.Action.PENALTIES_RECORDED,
        description=(
            f"Penalty shootout recorded: {match.home_team} "
            f"{match.penalty_home_score}-{match.penalty_away_score} "
            f"{match.away_team}."
        ),
        old_values=old_values,
        new_values={
            "status": match.status,
            "resolution_method": match.resolution_method,
            "penalty_home_score": match.penalty_home_score,
            "penalty_away_score": match.penalty_away_score,
        },
    )
    messages.success(request, RESULT_SAVED)
    return redirect("match_detail", pk=match.id)

@login_required
@_retry_busy_result
@transaction.atomic
def abandon_match(request, match_id):
    if request.method == "POST":
        match = _locked_result_or_404(match_id)
    else:
        match = get_object_or_404(Match.objects.select_related("competition"), pk=match_id)

    if not can_control_match(request.user, match):
        return HttpResponseForbidden(
            "You cannot control this match."
        )

    if not get_match_control_state(match).can_abandon:
        return HttpResponseBadRequest("This match cannot be abandoned now.")

    if request.method == "POST":
        reason = request.POST.get(
            "reason",
            "",
        ).strip()

        if not reason:
            messages.error(
                request,
                "Please provide a reason for abandoning the match.",
            )

            return render(
                request,
                "matches/abandon_match.html",
                {"match": match},
            )

        old_values = {
            "status": match.status,
            "phase": match.phase,
        }

        match.abandon_match(reason)

        log_match_activity(
            user=request.user,
            match=match,
            action=ActivityLog.Action.MATCH_ABANDONED,
            description=(
                f"Match abandoned. Reason: {reason}"
            ),
            old_values=old_values,
            new_values={
                "status": match.status,
                "reason": reason,
            },
        )

        return redirect(
            "match_detail",
            pk=match.id,
        )

    return render(
        request,
        "matches/abandon_match.html",
        {
            "match": match,
        },
    )

@user_passes_test(lambda u: u.is_superuser)
def add_match_for_league(request, slug):
    league = get_object_or_404(Competition, slug=slug)

    if request.method == "POST":
        form = MatchCreateForm(request.POST, league=league)
        if form.is_valid():
            match = form.save(commit=False)
            match.competition = league
            match.status = "UPCOMING"

            if match.stage.stage_type != "GROUP":
                match.group = None


            if (
                match.stage.stage_type == "KNOCKOUT"
                and competition_has_bracket(league)
            ):
                form.add_error(
                    None,
                    "Connected bracket fixtures are created only through bracket progression.",
                )
            else:
                try:
                    assign_match_to_knockout_tie(match)
                    match.save()

                    if (
                        "save_and_add_another" in request.POST
                        or "save_and_new" in request.POST
                    ):
                        return redirect("add_match_for_league", slug=league.slug)

                    return redirect("league_detail", slug=league.slug)

                except IntegrityError:
                    form.add_error(
                        None,
                        "This match already exists for this stage and leg.",
                    )
                except ValidationError as error:
                    form.add_error(None, error)

    else:
        form = MatchCreateForm(league=league)

    return render(request, "matches/add_match.html", {
        "league": league,
        "form": form,
    })


def _submitted_result_match_lock_queryset(match_ids):
    """Lock only submitted result rows, in deterministic order.

    A non-waiting lock makes a concurrent Quick Result, task, or manager
    update retryable instead of allowing the following Match UPDATE to hold a
    web worker indefinitely. ``of=(\"self\",)`` avoids locking nullable joined
    group and knockout rows.
    """

    return (
        Match.objects.select_for_update(of=("self",), nowait=True)
        .select_related(
            "competition", "stage", "group", "home_team", "away_team", "knockout_tie"
        )
        .filter(pk__in=match_ids)
        .order_by("id")
    )


def _lock_submitted_result_matches(match_ids):
    return list(_submitted_result_match_lock_queryset(match_ids))


@user_passes_test(lambda u: u.is_superuser)
def update_results_for_league(request, slug):
    league = get_object_or_404(Competition, slug=slug)

    if request.GET.get("filter") == "finished":
        if request.method == "POST":
            messages.info(request, "Choose Edit Result next to the finished match you want to correct.")
        return redirect(reverse("league_manager:result_update", args=[slug]) + "?tab=finished")

    current_filter = "pending"
    matches = Match.objects.filter(competition=league).exclude(
        status__in=["FINISHED", "ABANDONED"],
    ).select_related(
        "stage", "group", "home_team", "away_team", "knockout_tie",
    ).order_by("match_date")

    if request.method == "POST":
        updated_matches = []
        form_error = False

        for match in matches:
            home_score_raw = request.POST.get(f"home_score_{match.id}", "").strip()
            away_score_raw = request.POST.get(f"away_score_{match.id}", "").strip()
            regulation_home_raw = request.POST.get(
                f"regulation_home_score_{match.id}", ""
            ).strip()
            regulation_away_raw = request.POST.get(
                f"regulation_away_score_{match.id}", ""
            ).strip()
            penalty_home_raw = request.POST.get(
                f"penalty_home_score_{match.id}", ""
            ).strip()
            penalty_away_raw = request.POST.get(
                f"penalty_away_score_{match.id}", ""
            ).strip()

            score_data = {
                "home_score": home_score_raw,
                "away_score": away_score_raw,
                "regulation_home_score": regulation_home_raw,
                "regulation_away_score": regulation_away_raw,
                "penalty_home_score": penalty_home_raw,
                "penalty_away_score": penalty_away_raw,
            }
            if not any(score_data.values()):
                continue
            previous_result = result_snapshot(match)
            form = MatchResultForm(score_data, instance=match)

            if not form.is_valid():
                form_error = True
                messages.error(
                    request,
                    f"Invalid score for {match.home_team} vs {match.away_team}."
                )
                continue

            normalized_result = form.normalized_result
            if previous_result == {"status": "FINISHED", **normalized_result}:
                continue

            updated_matches.append({
                "match": match,
                "result": normalized_result,
                "old_values": previous_result,
            })

        if form_error:
            messages.error(request, "Some rows could not be saved. Please fix them and try again.")
        elif updated_matches:
            try:
                # Page rendering and validation happen outside a transaction.
                # Lock only changed rows immediately before finalisation.
                with transaction.atomic():
                    lock_result_competition(league.pk, nowait=True)
                    updates_by_match_id = {
                        update["match"].pk: update for update in updated_matches
                    }
                    locked_matches = _lock_submitted_result_matches(
                        updates_by_match_id
                    )
                    if len(locked_matches) != len(updates_by_match_id):
                        raise ValidationError(
                            "A submitted match changed before it could be saved. Please retry."
                        )
                    for match in locked_matches:
                        update = updates_by_match_id[match.pk]
                        if match.competition_id != league.pk or result_snapshot(match) != update["old_values"]:
                            raise ValidationError(RESULT_CONFLICT)
                        normalized_result = update["result"]
                        finalize_match_result_fast(
                            match=match,
                            home_score=normalized_result["home_score"],
                            away_score=normalized_result["away_score"],
                            regulation_home_score=(
                                normalized_result["regulation_home_score"]
                            ),
                            regulation_away_score=(
                                normalized_result["regulation_away_score"]
                            ),
                            penalty_home_score=(
                                normalized_result["penalty_home_score"]
                            ),
                            penalty_away_score=(
                                normalized_result["penalty_away_score"]
                            ),
                            actor=request.user,
                        )
                        log_match_activity(
                            user=request.user,
                            match=match,
                            action=ActivityLog.Action.MATCH_FINISHED,
                            description="Final result saved.",
                            old_values=update["old_values"],
                            new_values=result_snapshot(match),
                        )
            except OperationalError:
                messages.error(
                    request,
                    "Another result update is in progress for one of these matches. Please retry."
                )
                return redirect(f"{request.path}?filter={current_filter}")
            except ValidationError as error:
                record_blocked_bracket_correction(error, actor=request.user)
                messages.error(request, " ".join(error.messages))
                return render(request, "matches/update_results.html", {
                    "league": league,
                    "result_forms": [
                        {"match": item, "form": MatchResultForm(instance=item)}
                        for item in matches
                    ],
                    "current_filter": current_filter,
                })

            messages.success(request, RESULT_SAVED)
            return redirect(f"{request.path}?filter={current_filter}")
        else:
            messages.info(request, "No result changes were submitted.")

    result_forms = []
    for match in matches:
        result_forms.append({
            "match": match,
            "form": MatchResultForm(instance=match),
        })

    return render(request, "matches/update_results.html", {
        "league": league,
        "result_forms": result_forms,
        "current_filter": current_filter,
    })
