from .forms import LoginForm, SignUpForm
from league_manager.models import LeagueMembership

def auth_modals(request):
    if request.user.is_authenticated:
        return {
            "has_league_management_access": (
                request.user.is_superuser
                or LeagueMembership.objects.filter(
                    user=request.user,
                    role__in=[
                        LeagueMembership.Role.OWNER,
                        LeagueMembership.Role.ADMIN,
                    ],
                    is_active=True,
                ).exists()
            ),
        }
    return {
        "login_form": LoginForm(),
        "signup_form": SignUpForm(),
    }
