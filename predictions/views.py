from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q, Min
from leagues.models import Competition
from .models import PredictionMatch, Prediction
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import reverse
from .models import PredictionHistory
from django.utils import timezone
from .models import UserProfile
from .forms import UserProfileForm, JoinPredictionsForm
from django.contrib import messages
from django.db import transaction
from matches.models import MatchResultProcessingOutbox

def create_profile_from_pending_session(request):
    """
    After Google login, create the UserProfile using the data
    the user entered before continuing with Google.
    """

    if not request.user.is_authenticated:
        return None

    # If profile already exists, use it
    existing_profile = UserProfile.objects.filter(user=request.user).first()
    if existing_profile:
        # Clean old pending session data if it exists
        request.session.pop("pending_display_name", None)
        request.session.pop("pending_phone_number", None)
        return existing_profile

    pending_display_name = request.session.get("pending_display_name")
    pending_phone_number = request.session.get("pending_phone_number")

    # No pending data means user did not come from the join form
    if not pending_display_name or not pending_phone_number:
        return None

    profile = UserProfile.objects.create(
        user=request.user,
        display_name=pending_display_name,
        phone_number=pending_phone_number,
    )

    # Very important: remove temporary session data after using it
    request.session.pop("pending_display_name", None)
    request.session.pop("pending_phone_number", None)
    request.session.pop("prediction_return_slug", None)

    return profile

# ===== CHANGED =====
# Removed @login_required so everyone can browse predictions
def league_predictions(request, slug):
    competition = get_object_or_404(Competition.objects.select_related("series"), slug=slug)

    if not competition.predictions_enabled:
        return redirect("league_detail", slug=slug)

    now = timezone.now()

    profile = None
    if request.user.is_authenticated:
        profile = UserProfile.objects.filter(user=request.user).first()
        
    base_prediction_matches = PredictionMatch.objects.filter(
        competition=competition,
        is_active=True,
        archived=False
    ).select_related(
        "match",
        "match__home_team",
        "match__away_team"
    )

    # =====================================================
    # POST: bulk submit many predictions at once
    # =====================================================
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect("join_predictions", slug=slug)

        if not profile:
            return redirect("complete_profile", slug=slug)

        open_prediction_matches = base_prediction_matches.filter(
            match__match_date__gt=now
        )

        created_count = 0
        incomplete_count = 0

        with transaction.atomic():
            for pm in open_prediction_matches:
                # Do not allow duplicate prediction for the same user and match
                already_predicted = Prediction.objects.filter(
                    user=request.user,
                    prediction_match=pm
                ).exists()

                if already_predicted:
                    continue

                predicted_result = request.POST.get(f"predicted_result_{pm.id}")
                home_goals = request.POST.get(f"home_goals_{pm.id}")
                away_goals = request.POST.get(f"away_goals_{pm.id}")

                # Completely blank card: ignore it
                if not predicted_result and home_goals == "" and away_goals == "":
                    continue

                # Partially filled card: do not save it
                if not predicted_result or home_goals == "" or away_goals == "":
                    incomplete_count += 1
                    continue

                try:
                    home_goals = int(home_goals)
                    away_goals = int(away_goals)
                except ValueError:
                    incomplete_count += 1
                    continue

                if home_goals < 0 or away_goals < 0:
                    incomplete_count += 1
                    continue

                Prediction.objects.create(
                    user=request.user,
                    prediction_match=pm,
                    predicted_result=predicted_result,
                    predicted_home_goals=home_goals,
                    predicted_away_goals=away_goals,
                )

                created_count += 1

        if created_count:
            messages.success(
                request,
                f"{created_count} prediction(s) submitted successfully."
            )

        if incomplete_count:
            messages.warning(
                request,
                f"{incomplete_count} incomplete prediction(s) were not saved."
            )

        if not created_count and not incomplete_count:
            messages.info(request, "No new predictions were submitted.")

        return redirect("league_predictions", slug=slug)

    # =====================================================
    # GET: show only open matches OR matches predicted by user
    # =====================================================
    if request.user.is_authenticated:
        prediction_matches = base_prediction_matches.filter(
            Q(match__match_date__gt=now) |
            Q(predictions__user=request.user)
        ).distinct().order_by("match__match_date")
    else:
        prediction_matches = base_prediction_matches.filter(
            match__match_date__gt=now
        ).order_by("match__match_date")

    # Attach current user's prediction to each prediction match
    user_predictions_map = {}

    if request.user.is_authenticated:
        user_predictions = Prediction.objects.filter(
            user=request.user,
            prediction_match__in=prediction_matches
        ).select_related("prediction_match")

        user_predictions_map = {
            prediction.prediction_match_id: prediction
            for prediction in user_predictions
        }

    has_open_unpredicted_matches = False

    for pm in prediction_matches:
        pm.user_prediction = user_predictions_map.get(pm.id)

        if pm.is_open and not pm.user_prediction:
            has_open_unpredicted_matches = True

    standings = (
        Prediction.objects
        .filter(
            prediction_match__competition=competition,
            prediction_match__is_active=True,
            prediction_match__archived=False
        )
        .values(
            "user__id",
            "user__userprofile__display_name"
        )
        .annotate(
            total_points=Sum("points_awarded"),
            perfect_predictions=Count("id", filter=Q(points_awarded=20)),
            first_prediction_time=Min("created_at")
        )
        .order_by(
            "-total_points",
            "-perfect_predictions",
            "first_prediction_time"
        )
    )

    history = []

    if request.user.is_authenticated:
        history = PredictionHistory.objects.filter(
            user=request.user,
            competition=competition
        ).order_by("-saved_at")

    sponsor = competition.prediction_sponsors.filter(
        page="predictions",
        is_active=True
    ).first()

    return render(request, "predictions/league_predictions.html", {
        "competition": competition,
        "prediction_matches": prediction_matches,
        "standings": standings,
        "history": history,
        "sponsor": sponsor,
        "profile": profile,
        "has_open_unpredicted_matches": has_open_unpredicted_matches,
    })
@staff_member_required
@transaction.atomic
def reset_predictions(request, slug):
    competition = get_object_or_404(
        Competition.objects.select_for_update(of=("self",), no_key=True), slug=slug,
    )

    old_prediction_matches = PredictionMatch.objects.filter(
        competition=competition,
        is_active=True,
        archived=False,
        match__status="FINISHED"
    ).select_related("match")

    if MatchResultProcessingOutbox.objects.filter(
        match_id__in=old_prediction_matches.values("match_id"),
        state=MatchResultProcessingOutbox.State.PENDING,
    ).exists():
        messages.warning(request, "Wait for pending result updates before resetting predictions.")
        return redirect("league_predictions", slug=slug)

    old_predictions = Prediction.objects.filter(
        prediction_match__in=old_prediction_matches
    ).select_related("prediction_match__match", "user")

    # Save only old predictions to history
    for p in old_predictions:
        PredictionHistory.objects.create(
            user=p.user,
            competition=competition,
            home_team=p.prediction_match.match.home_team.name,
            away_team=p.prediction_match.match.away_team.name,
            predicted_home_goals=p.predicted_home_goals,
            predicted_away_goals=p.predicted_away_goals,
            predicted_result=p.predicted_result,
            points_awarded=p.points_awarded
        )

    # Delete only old predictions
    old_predictions.delete()

    # Archive only old finished/abandoned prediction matches
    old_prediction_matches.update(
        is_active=False,
        archived=True
    )

    return redirect("league_predictions", slug=slug)


def join_predictions(request, slug):
    competition = get_object_or_404(Competition.objects.select_related("series"), slug=slug)

    if not competition.predictions_enabled:
        return redirect("league_detail", slug=slug)

    # If user is not logged in, send them to Google first
    if not request.user.is_authenticated:
        return redirect("google_login_for_predictions", slug=slug)

    # If logged in and already has profile, go straight to predictions
    profile = UserProfile.objects.filter(user=request.user).first()
    if profile:
        return redirect("league_predictions", slug=slug)

    # If logged in but no prediction profile, send to final step
    return redirect("complete_profile", slug=slug)


def google_login_for_predictions(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    if not competition.predictions_enabled:
        return redirect("league_detail", slug=slug)

    next_url = reverse("complete_profile", kwargs={"slug": slug})

    return redirect(f"/accounts/google/login/?next={next_url}")


@login_required
def complete_profile(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    if not competition.predictions_enabled:
        return redirect("league_detail", slug=slug)

    # If profile already exists, no need to complete again
    existing_profile = UserProfile.objects.filter(user=request.user).first()
    if existing_profile:
        return redirect("league_predictions", slug=slug)

    if request.method == "POST":
        form = UserProfileForm(request.POST)

        if form.is_valid():
            profile = form.save(commit=False)
            profile.user = request.user
            profile.save()

            return redirect("league_predictions", slug=slug)

    else:
        form = UserProfileForm()

    return render(request, "predictions/complete_profile.html", {
        "form": form,
        "competition": competition,
    })
