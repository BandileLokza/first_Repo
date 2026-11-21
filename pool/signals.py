"""Safe, local lifecycle signals for pool competition setup."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import PoolCompetition
from .services import ensure_competition_owner_membership, ensure_default_stages


@receiver(
    post_save,
    sender=PoolCompetition,
    dispatch_uid="pool.provision_competition_structure",
)
def provision_pool_competition(sender, instance, created, raw=False, **kwargs):
    """Provision explicit default stages and the owner membership safely."""
    if raw:
        return
    if created:
        ensure_default_stages(instance)
        # Import lazily so model loading remains independent of read-only
        # standings helpers.  This also gives objects created through the ORM,
        # fixtures, or Django admin the same approved ranking defaults.
        if not instance.is_money_match:
            from .standings import ensure_default_pool_tie_breakers

            ensure_default_pool_tie_breakers(instance)
    ensure_competition_owner_membership(instance)
