"""Read-model helpers for the public Pool discovery home page.

The homepage is competition-first: each card receives a small, ordered set of
matches without asking the template to recreate lifecycle or date rules.  The
prefetches intentionally cap each candidate bucket per competition, avoiding
one query per card and avoiding a full fixture-history load.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.db.models import F, Prefetch, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from django.utils.formats import date_format

from .models import PoolMatch


HOMEPAGE_MATCH_LIMIT = 5


def _home_match_queryset():
    """Return the participant-complete queryset used by every card bucket."""

    return PoolMatch.objects.select_related(
        "stage",
        "group",
        "knockout_round",
        "home_entry__player",
        "home_entry__team",
        "away_entry__player",
        "away_entry__team",
        "home_entry_life",
        "away_entry_life",
    )


def _limited_per_competition(queryset, *, ordering):
    """Keep only the useful candidates for each competition in one query."""

    return queryset.annotate(
        _homepage_match_rank=Window(
            expression=RowNumber(),
            partition_by=[F("competition_id")],
            order_by=ordering,
        )
    ).filter(_homepage_match_rank__lte=HOMEPAGE_MATCH_LIMIT)


def homepage_match_prefetches(today=None):
    """Return bounded, relation-complete candidate buckets for home cards.

    ``today`` is injectable for deterministic tests; production uses the
    configured Django timezone.  Date ranges are used rather than a database
    ``__date`` transform so the matchday boundary is unambiguously local.
    """

    today = today or timezone.localdate()
    day_start = timezone.make_aware(
        datetime.combine(today, time.min), timezone.get_current_timezone()
    )
    next_day_start = day_start + timedelta(days=1)

    live_matches = _limited_per_competition(
        _home_match_queryset().filter(status=PoolMatch.Status.LIVE),
        ordering=[F("scheduled_at").asc(nulls_last=True), "pk"],
    )
    today_upcoming_matches = _limited_per_competition(
        _home_match_queryset()
        .filter(
            status=PoolMatch.Status.UPCOMING,
            scheduled_at__gte=day_start,
            scheduled_at__lt=next_day_start,
        ),
        ordering=["scheduled_at", "pk"],
    )
    today_completed_matches = _limited_per_competition(
        _home_match_queryset().filter(
            status__in=(PoolMatch.Status.FINISHED, PoolMatch.Status.ABANDONED),
            scheduled_at__gte=day_start,
            scheduled_at__lt=next_day_start,
        ),
        ordering=[
            F("completed_at").desc(nulls_last=True),
            F("scheduled_at").desc(nulls_last=True),
            F("pk").desc(),
        ],
    )
    today_other_matches = _limited_per_competition(
        _home_match_queryset().filter(
            status__in=(PoolMatch.Status.POSTPONED, PoolMatch.Status.CANCELLED),
            scheduled_at__gte=day_start,
            scheduled_at__lt=next_day_start,
        ),
        ordering=["scheduled_at", "pk"],
    )
    future_matches = _limited_per_competition(
        _home_match_queryset().filter(
            status__in=(PoolMatch.Status.UPCOMING, PoolMatch.Status.POSTPONED),
        ).filter(
            Q(scheduled_at__gte=next_day_start) | Q(scheduled_at__isnull=True),
        ),
        ordering=[F("scheduled_at").asc(nulls_last=True), "pk"],
    )
    recent_results = _limited_per_competition(
        _home_match_queryset().filter(
            status__in=(PoolMatch.Status.FINISHED, PoolMatch.Status.ABANDONED)
        ),
        ordering=[
            F("completed_at").desc(nulls_last=True),
            F("scheduled_at").desc(nulls_last=True),
            F("pk").desc(),
        ],
    )

    return (
        Prefetch("matches", queryset=live_matches, to_attr="homepage_live_matches"),
        Prefetch(
            "matches",
            queryset=today_upcoming_matches,
            to_attr="homepage_today_upcoming_matches",
        ),
        Prefetch(
            "matches",
            queryset=today_completed_matches,
            to_attr="homepage_today_completed_matches",
        ),
        Prefetch(
            "matches",
            queryset=today_other_matches,
            to_attr="homepage_today_other_matches",
        ),
        Prefetch("matches", queryset=future_matches, to_attr="homepage_future_matches"),
        Prefetch(
            "matches",
            queryset=recent_results,
            to_attr="homepage_recent_results",
        ),
    )


def homepage_matches_for_competition(competition):
    """Select at most five preloaded fixtures using the public priority rules."""

    live = getattr(competition, "homepage_live_matches", ())
    today_upcoming = getattr(competition, "homepage_today_upcoming_matches", ())
    today_completed = getattr(competition, "homepage_today_completed_matches", ())
    today_other = getattr(competition, "homepage_today_other_matches", ())
    future = getattr(competition, "homepage_future_matches", ())
    results = getattr(competition, "homepage_recent_results", ())

    # A matchday remains a matchday after its final rack: today's completed
    # scores therefore stay ahead of the next fixture on a later date.
    is_matchday = bool(today_upcoming or today_completed or today_other)
    buckets = (
        live,
        today_upcoming,
        today_completed,
        today_other,
        future,
        results,
    ) if is_matchday else (
        live,
        future,
        results,
    )
    selected = []
    seen_ids = set()
    for bucket in buckets:
        for pool_match in bucket:
            if pool_match.pk in seen_ids:
                continue
            selected.append(pool_match)
            seen_ids.add(pool_match.pk)
            if len(selected) == HOMEPAGE_MATCH_LIMIT:
                return selected
    return selected


def _homepage_match_state(match):
    """Return one honest compact state label for a selected match."""

    if match.status == PoolMatch.Status.LIVE:
        return "LIVE", "live"
    if match.status == PoolMatch.Status.FINISHED:
        if match.result_method == PoolMatch.ResultMethod.PLAYED:
            return "FT", "finished"
        return match.get_result_method_display(), "finished"
    if match.status == PoolMatch.Status.ABANDONED:
        return match.get_status_display(), "abandoned"
    if match.status == PoolMatch.Status.POSTPONED:
        return match.get_status_display(), "postponed"
    if match.status == PoolMatch.Status.CANCELLED:
        return match.get_status_display(), "cancelled"
    if match.scheduled_at:
        local_scheduled_at = timezone.localtime(match.scheduled_at)
        if local_scheduled_at.date() == timezone.localdate():
            return date_format(local_scheduled_at, "H:i"), "upcoming"
        return date_format(local_scheduled_at, "M j · H:i"), "upcoming"
    return "TBA", "upcoming"


def homepage_match_rows(competition):
    """Return template-ready compact rows for one preloaded competition card."""

    rows = []
    for pool_match in homepage_matches_for_competition(competition):
        state_label, state_kind = _homepage_match_state(pool_match)
        played_result = (
            pool_match.status == PoolMatch.Status.FINISHED
            and pool_match.result_method == PoolMatch.ResultMethod.PLAYED
        )
        rows.append(
            {
                "match": pool_match,
                "state_label": state_label,
                "state_kind": state_kind,
                "show_score": pool_match.status
                in (PoolMatch.Status.LIVE, PoolMatch.Status.ABANDONED)
                or played_result,
                "centre_label": (
                    pool_match.get_result_method_display()
                    if pool_match.status == PoolMatch.Status.FINISHED
                    and not played_result
                    else "vs"
                ),
            }
        )
    return rows
