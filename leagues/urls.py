from django.urls import path
from . import views

urlpatterns = [
    path('', views.league_list, name='league_list'),
    path('regions/<slug:slug>/', views.region_detail, name='region_detail'),

    # Overview (default)
    path('<slug:slug>/', views.league_detail, name='league_detail'),

    # Tabs
    path('<slug:slug>/fixtures/', views.league_fixtures, name='league_fixtures'),
    path('<slug:slug>/teams/<int:team_id>/', views.team_profile, name='team_profile'),
    path('<slug:slug>/results/', views.league_results, name='league_results'),
    path('<slug:slug>/standings/', views.league_standings, name='league_standings'),
    path('<slug:slug>/stats/', views.league_stats, name='league_stats'),
]
