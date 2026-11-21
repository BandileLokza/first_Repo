from django.db import models
from django.contrib.auth.models import User
from matches.models import Match
from leagues.models import Competition
from django.utils import timezone
from teams.storage_backends import SupabaseStorage


class PredictionMatch(models.Model):
    """
    Admin selects which matches are open for prediction
    """

    match = models.OneToOneField(
        Match,
        on_delete=models.CASCADE,
        related_name="prediction_match"
    )

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="prediction_matches"
    )

    is_active = models.BooleanField(default=True)
    archived = models.BooleanField(default=False)

    @property
    def is_open(self):
        return self.match.match_date > timezone.now()
    
    def __str__(self):
        return f"Prediction: {self.match}"


class Prediction(models.Model):

    RESULT_CHOICES = [
        ('H', 'Home Win'),
        ('D', 'Draw'),
        ('A', 'Away Win'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="predictions"
    )

    prediction_match = models.ForeignKey(
        PredictionMatch,
        on_delete=models.CASCADE,
        related_name="predictions"
    )

    predicted_result = models.CharField(
        max_length=1,
        choices=RESULT_CHOICES
    )

    predicted_home_goals = models.IntegerField()
    predicted_away_goals = models.IntegerField()

    points_awarded = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'prediction_match')

    def __str__(self):
        return f"{self.user.username} - {self.prediction_match.match}"
    

class PredictionHistory(models.Model):

    user = models.ForeignKey(User, on_delete=models.CASCADE)

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE)

    home_team = models.CharField(max_length=100)
    away_team = models.CharField(max_length=100)

    predicted_home_goals = models.IntegerField()
    predicted_away_goals = models.IntegerField()

    predicted_result = models.CharField(max_length=1)

    points_awarded = models.IntegerField(default=0)

    saved_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} history {self.home_team} vs {self.away_team}"

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    display_name = models.CharField(max_length=50)
    phone_number = models.CharField(max_length=20)

    def __str__(self):
        return f"{self.display_name} ({self.user.email})"
    

class PredictionSponsor(models.Model):
    PAGE_CHOICES = [
        ("predictions", "Predictions Page"),
    ]

    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="prediction_sponsors"
    )

    page = models.CharField(
        max_length=30,
        choices=PAGE_CHOICES,
        default="predictions"
    )

    sponsor_name = models.CharField(max_length=100)
    sponsor_logo = models.ImageField(
        upload_to="sponsers/",
        storage=SupabaseStorage(folder="sponsers", image_profile="sponsor")
    )
    sponsor_link = models.URLField(blank=True, null=True)

    sponsor_text = models.CharField(
        max_length=150,
        default="Predictions brought to you by"
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.competition.name} - {self.sponsor_name}"
