from .models import ActivityLog


def record_activity(
    *,
    actor,
    competition,
    action,
    description="",
    match=None,
    old_values=None,
    new_values=None,
):
    """
    Create an audit record for league-management activity.

    Values passed into old_values and new_values must be
    JSON-compatible: strings, numbers, booleans, lists or dictionaries.
    """
    return ActivityLog.objects.create(
        actor=actor,
        competition=competition,
        match=match,
        action=action,
        description=description,
        old_values=old_values or {},
        new_values=new_values or {},
    )