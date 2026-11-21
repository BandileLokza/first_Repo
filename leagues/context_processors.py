# leagues/context_processors.py

from .models import SiteIdentity

def site_identity(request):
    return {
        "site_identity": SiteIdentity.objects.filter(is_active=True).first()
    }