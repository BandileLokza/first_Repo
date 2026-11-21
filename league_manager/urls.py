from django.urls import path

from . import views
from .finished_scorer_views import finished_goal_scorers


app_name = "league_manager"


urlpatterns = [
    path(
        "",
        views.dashboard,
        name="dashboard",
    ),
    path(
        "regions/<slug:slug>/league-owners/",
        views.regional_league_owners,
        name="regional_league_owners",
    ),
    path(
        "regions/<slug:slug>/league-owners/<int:series_id>/assign/",
        views.regional_league_owner_assign,
        name="regional_league_owner_assign",
    ),
    path(
        "regions/<slug:slug>/league-owners/<int:series_id>/owners/<int:user_id>/remove/",
        views.regional_league_owner_remove,
        name="regional_league_owner_remove",
    ),
    path(
        "regions/<slug:slug>/reports/<str:report_kind>/",
        views.regional_report_export,
        name="regional_report_export",
    ),
    path(
        "organisations/<str:organisation>/league-owners/",
        views.organisation_league_owners,
        name="organisation_league_owners",
    ),
    path(
        "organisations/<str:organisation>/league-owners/<int:series_id>/assign/",
        views.organisation_league_owner_assign,
        name="organisation_league_owner_assign",
    ),
    path(
        "organisations/<str:organisation>/league-owners/<int:series_id>/owners/<int:user_id>/remove/",
        views.organisation_league_owner_remove,
        name="organisation_league_owner_remove",
    ),
    path(
        "organisations/<str:organisation>/reports/<str:report_kind>/",
        views.organisation_report_export,
        name="organisation_report_export",
    ),

    path(
        "regions/<slug:slug>/fixture-letterhead/",
        views.regional_fixture_letterhead,
        name="regional_fixture_letterhead",
    ),
    path(
        "regions/<slug:slug>/leagues/<int:series_id>/fixture-letterhead/",
        views.regional_league_fixture_letterhead,
        name="regional_league_fixture_letterhead",
    ),
    path(
        "organisations/<str:organisation>/fixture-letterhead/",
        views.organisation_fixture_letterhead,
        name="organisation_fixture_letterhead",
    ),
    path(
        "zonal/leagues/<int:series_id>/fixture-letterhead/",
        views.zonal_fixture_letterhead,
        name="zonal_fixture_letterhead",
    ),

    path(
        "<slug:slug>/reports/<str:report_kind>/",
        views.league_report_export,
        name="league_report_export",
    ),

    # Football league season archive workflow (superuser only in the view).
    path(
        "<slug:slug>/archive-season/",
        views.archive_season,
        name="archive_season",
    ),
    path(
        "<slug:slug>/next-season/",
        views.create_next_season,
        name="create_next_season",
    ),

    # Teams
    path(
        "<slug:slug>/teams/",
        views.team_list,
        name="team_list",
    ),
    path(
        "<slug:slug>/teams/add/",
        views.team_create,
        name="team_create",
    ),
    path(
        "<slug:slug>/teams/<int:team_id>/edit/",
        views.team_update,
        name="team_update",
    ),
    path(
        "<slug:slug>/teams/<int:team_id>/coaches/",
        views.team_coach_list,
        name="team_coach_list",
    ),
    path(
        "<slug:slug>/teams/<int:team_id>/coaches/assign/",
        views.team_coach_assign,
        name="team_coach_assign",
    ),
    path(
        "<slug:slug>/teams/<int:team_id>/coaches/<int:assignment_id>/remove/",
        views.team_coach_remove,
        name="team_coach_remove",
    ),

    # Players
    path(
        "<slug:slug>/players/",
        views.player_list,
        name="player_list",
    ),
    path(
        "<slug:slug>/players/add/",
        views.player_create,
        name="player_create",
    ),
    path(
        "<slug:slug>/players/<int:player_id>/edit/",
        views.player_update,
        name="player_update",
    ),

    # Fixtures
    path(
        "<slug:slug>/fixtures/",
        views.fixture_list,
        name="fixture_list",
    ),
    path(
        "<slug:slug>/schedule/lock/",
        views.schedule_lock,
        name="schedule_lock",
    ),
    path(
        "<slug:slug>/schedule/unlock/",
        views.schedule_unlock,
        name="schedule_unlock",
    ),
    path(
        "<slug:slug>/fixtures/add/",
        views.fixture_create,
        name="fixture_create",
    ),
    path(
        "<slug:slug>/fixtures/preview/<str:action>/",
        views.fixture_generation_preview,
        name="fixture_generation_preview",
    ),
    path(
        "<slug:slug>/fixtures/generate-first-round/",
        views.generate_first_round,
        name="generate_first_round",
    ),
    path(
        "<slug:slug>/fixtures/generate-remaining-first-round/",
        views.generate_remaining_first_round,
        name="generate_remaining_first_round",
    ),
    path(
        "<slug:slug>/fixtures/generate-second-round/",
        views.generate_second_round,
        name="generate_second_round",
    ),
    path(
        "<slug:slug>/fixtures/<int:match_id>/edit/",
        views.fixture_update,
        name="fixture_update",
    ),
    path(
        "<slug:slug>/fixtures/<int:match_id>/delete/",
        views.fixture_delete,
        name="fixture_delete",
    ),
    path(
        "<slug:slug>/fixtures/<int:match_id>/resolve-abandoned/",
        views.resolve_abandoned_match,
        name="resolve_abandoned_match",
    ),

    # Match agents
    path(
        "<slug:slug>/agents/",
        views.agent_list,
        name="agent_list",
    ),
    path(
        "<slug:slug>/agents/register/",
        views.agent_register,
        name="agent_register",
    ),
    path(
        "<slug:slug>/agents/<int:membership_id>/toggle/",
        views.agent_toggle_active,
        name="agent_toggle_active",
    ),
    path(
        "<slug:slug>/matches/<int:match_id>/assign-agent/",
        views.assign_agent,
        name="assign_agent",
    ),
    path(
        "<slug:slug>/matches/<int:match_id>/unassign-agent/",
        views.unassign_agent,
        name="unassign_agent",
    ),
    # League administrators
    path(
        "<slug:slug>/staff/",
        views.staff_list,
        name="staff_list",
    ),
    path(
        "<slug:slug>/staff/register/",
        views.staff_register,
        name="staff_register",
    ),
    path(
        "<slug:slug>/staff/<int:membership_id>/toggle/",
        views.staff_toggle_active,
        name="staff_toggle_active",
    ),

    # Activity history
    path(
        "<slug:slug>/activity/",
        views.activity_list,
        name="activity_list",
    ),
    # Result entry and correction
    path(
        "<slug:slug>/results/update/",
        views.result_update,
        name="result_update",
    ),
    path(
        "<slug:slug>/results/<int:match_id>/goal-scorers/",
        finished_goal_scorers,
        name="finished_goal_scorers",
    ),
    path(
        "<slug:slug>/results/<int:match_id>/edit/",
        finished_goal_scorers,
        {"edit_result": True},
        name="edit_finished_match",
    ),

    # Groups
    path(
        "<slug:slug>/groups/",
        views.group_list,
        name="group_list",
    ),
    path(
        "<slug:slug>/groups/add/",
        views.group_create,
        name="group_create",
    ),
    path(
        "<slug:slug>/groups/<int:group_id>/edit/",
        views.group_update,
        name="group_update",
    ),

    # Knockout rounds
    path(
        "<slug:slug>/knockout-rounds/",
        views.knockout_rounds,
        name="knockout_rounds",
    ),
    path(
        "<slug:slug>/knockout-bracket/",
        views.knockout_bracket,
        name="knockout_bracket",
    ),

    path(
        "<slug:slug>/region/",
        views.update_league_region,
        name="update_league_region",
    ),
    path(
        "<slug:slug>/branding/",
        views.update_league_branding,
        name="update_league_branding",
    ),
    # Keep this final because it is the broad slug route
    path(
        "<slug:slug>/",
        views.league_dashboard,
        name="league_dashboard",
    ),
]
