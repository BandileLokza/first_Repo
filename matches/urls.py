from django.urls import path
from . import views

urlpatterns = [
    
    path('dashboard/', views.dashboard, name='dashboard'),
    path('add-player/<int:team_id>/', views.add_player, name='add_player'),
    path('add-lineup/<int:match_id>/<int:team_id>/', views.add_lineup, name='add_lineup'),
    path('unlock-lineups/<int:match_id>/', views.unlock_lineups, name='unlock_lineups'),
    path('add-goal/<int:match_id>/<int:team_id>/', views.add_goal, name='add_goal'),
    path('quick-result/<int:match_id>/', views.save_quick_result, name='save_quick_result'),
    path('quick-goal-details/<int:match_id>/', views.save_quick_goal_details, name='save_quick_goal_details'),

    path('ajax/get-teams/', views.get_teams_by_league, name='get_teams_by_league'),
    path('<int:pk>/', views.match_detail, name='match_detail'),
    path('edit-goal/<int:goal_id>/', views.edit_goal, name='edit_goal'),
    path('cancel-goal/<int:goal_id>/', views.cancel_goal, name='cancel_goal'),
    path('match/<int:match_id>/cards/yellow/', views.add_card, {"card_kind": "yellow"}, name='add_yellow_card_control'),
    path('match/<int:match_id>/cards/red/', views.add_card, {"card_kind": "red"}, name='add_red_card'),
    path('cards/<int:card_id>/edit/', views.edit_card, name='edit_card'),
    path('cards/<int:card_id>/cancel/', views.cancel_card, name='cancel_card'),
    path(
    'match/<int:match_id>/yellow-card/<int:team_id>/',
    views.add_yellow_card,
    name='add_yellow_card'
    ),
    path('start-first-half/<int:match_id>/', views.start_first_half, name='start_first_half'),
    path('end-first-half/<int:match_id>/', views.end_first_half, name='end_first_half'),
    path('start-second-half/<int:match_id>/', views.start_second_half, name='start_second_half'),
    path('end-match/<int:match_id>/', views.end_match, name='end_match'),
    path(
        'start-extra-time/<int:match_id>/',
        views.start_extra_time_view,
        name='start_extra_time',
    ),
    path(
        'end-extra-time-first-half/<int:match_id>/',
        views.end_extra_time_first_half_view,
        name='end_extra_time_first_half',
    ),
    path(
        'start-extra-time-second-half/<int:match_id>/',
        views.start_extra_time_second_half_view,
        name='start_extra_time_second_half',
    ),
    path(
        'end-extra-time/<int:match_id>/',
        views.end_extra_time_view,
        name='end_extra_time',
    ),
    path(
        'start-penalties/<int:match_id>/',
        views.start_penalties_view,
        name='start_penalties',
    ),
    path(
        'finish-penalties/<int:match_id>/',
        views.finish_penalties_view,
        name='finish_penalties',
    ),
    path('abandon-match/<int:match_id>/', views.abandon_match, name='abandon_match'),
    path("league/<slug:slug>/add-match/", views.add_match_for_league, name="add_match_for_league"),
    path("league/<slug:slug>/update-results/", views.update_results_for_league, name="update_results_for_league"),
]
