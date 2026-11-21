from django.db.models.signals import post_save
from django.dispatch import receiver
from matches.models import Match
from .models import PredictionMatch


@receiver(post_save, sender=Match)
def create_prediction_match(sender, instance, created, **kwargs):
    if created:
        prediction_match, was_created = PredictionMatch.objects.get_or_create(
            match=instance,
            defaults={
                "competition": instance.competition,
                "is_active": True,
                "archived": False,
            }
        )

        if not was_created:
            prediction_match.competition = instance.competition
            prediction_match.is_active = True
            prediction_match.archived = False
            prediction_match.save()