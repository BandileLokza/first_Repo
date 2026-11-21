from django.conf import settings
from django.db import models
from leagues.models import Competition
from teams.storage_backends import SupabaseStorage


class Team(models.Model):
    name = models.CharField(max_length=200)
    home_ground = models.CharField(max_length=200, blank=True, default="")
    logo = models.ImageField(
        upload_to="teams/",
        storage=SupabaseStorage(image_profile="logo"),
        blank=True,
        null=True
    )
    competition = models.ForeignKey(
        "leagues.Competition",
        on_delete=models.CASCADE,
        related_name="teams"
    )

    def __str__(self):
        return self.name


class TeamCoachAssignment(models.Model):
    """A team-scoped coaching assignment.

    This deliberately lives outside LeagueMembership: a coach can be assigned
    to more than one team and can also hold an unrelated league role.
    """

    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="coach_assignments",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_coach_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["team__name", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "user"],
                name="unique_team_coach_assignment",
            ),
        ]

    def __str__(self):
        return f"{self.user} - Coach of {self.team.name}"
