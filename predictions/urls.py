from django.urls import path
from . import views

urlpatterns = [
    path("league/<slug:slug>/join/", views.join_predictions, name="join_predictions"),
    path("league/<slug:slug>/join/google/", views.google_login_for_predictions, name="google_login_for_predictions"),
    path("league/<slug:slug>/complete-profile/", views.complete_profile, name="complete_profile"),
    path("league/<slug:slug>/predictions/", views.league_predictions, name="league_predictions"),
    path("league/<slug:slug>/reset/", views.reset_predictions, name="reset_predictions"),
]
