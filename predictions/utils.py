from django.db.models import Case, When

from .models import Prediction


def calculate_predictions_for_match(match):
    """
    Recalculate prediction points for one match from scratch using the score
    after regulation. Extra-time goals and shootout kicks do not change the
    existing prediction type.
    """
    predictions = Prediction.objects.filter(
        prediction_match__match=match
    )

    actual_home = match.prediction_home_score
    actual_away = match.prediction_away_score

    if actual_home > actual_away:
        actual_result = 'H'
    elif actual_home < actual_away:
        actual_result = 'A'
    else:
        actual_result = 'D'

    points = (
        Case(When(predicted_result=actual_result, then=5), default=0)
        + Case(When(predicted_home_goals=actual_home, then=5), default=0)
        + Case(When(predicted_away_goals=actual_away, then=5), default=0)
        + Case(
            When(predicted_home_goals=actual_home, predicted_away_goals=actual_away, then=5),
            default=0,
        )
    )
    predictions.update(points_awarded=points)
