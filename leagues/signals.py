from django.db.models.signals import post_save
from django.dispatch import receiver
from teams.models import Team
from leagues.models import Competition, Group, Standing, Stage
from leagues.services import ensure_default_tie_breakers
from django.db.models.signals import m2m_changed


@receiver(post_save, sender=Competition)
def create_competition_tie_breakers(sender, instance, created, raw=False, **kwargs):
    """Persist the standard ranking configuration for normal ORM creation."""

    if (
        created
        and not raw
        and not getattr(instance, "_skip_default_tie_breaker_provisioning", False)
    ):
        ensure_default_tie_breakers(instance)


@receiver(post_save, sender=Team)
def create_league_standing(sender, instance, created, **kwargs):
    if not created:
        return

    competition = instance.competition

    # Only auto-create for TABLE competitions
    if competition.format_type != "TABLE":
        return

    league_stage = competition.stages.filter(stage_type="LEAGUE").first()

    if not league_stage:
        return

    Standing.objects.get_or_create(
        competition=competition,
        stage=league_stage,
        team=instance,
        defaults={
            "played": 0,
            "won": 0,
            "drawn": 0,
            "lost": 0,
            "goals_for": 0,
            "goals_against": 0,
            "points": 0,
        }
    )


@receiver(m2m_changed, sender=Group.teams.through)
def create_group_standings(sender, instance, action, pk_set, **kwargs):
    if action != "post_add":
        return

    stage = instance.stage
    competition = stage.competition

    for team_id in pk_set:
        Standing.objects.get_or_create(
            competition=competition,
            stage=stage,
            group=instance,
            team_id=team_id,
            defaults={
                "played": 0,
                "won": 0,
                "drawn": 0,
                "lost": 0,
                "goals_for": 0,
                "goals_against": 0,
                "points": 0,
            }
        )
