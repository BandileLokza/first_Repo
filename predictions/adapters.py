from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from .models import UserProfile


class PredictionsSocialAccountAdapter(DefaultSocialAccountAdapter):
    # ===== NEW =====
    # After Google login succeeds, attach pending prediction profile data
    def pre_social_login(self, request, sociallogin):
        display_name = request.session.get("pending_display_name")
        phone_number = request.session.get("pending_phone_number")

        if not display_name or not phone_number:
            return

        user = sociallogin.user

        # If this is an existing account, user may already be linked
        if user.pk:
            profile, created = UserProfile.objects.get_or_create(
                user=user,
                defaults={
                    "display_name": display_name,
                    "phone_number": phone_number,
                }
            )

            if not created:
                if not profile.display_name:
                    profile.display_name = display_name
                if not profile.phone_number:
                    profile.phone_number = phone_number
                profile.save()

            request.session.pop("pending_display_name", None)
            request.session.pop("pending_phone_number", None)

    # ===== NEW =====
    # For brand new Google users not saved yet at pre_social_login stage
    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)

        display_name = request.session.get("pending_display_name")
        phone_number = request.session.get("pending_phone_number")

        if display_name and phone_number:
            profile, created = UserProfile.objects.get_or_create(
                user=user,
                defaults={
                    "display_name": display_name,
                    "phone_number": phone_number,
                }
            )

            if not created:
                profile.display_name = profile.display_name or display_name
                profile.phone_number = profile.phone_number or phone_number
                profile.save()

            request.session.pop("pending_display_name", None)
            request.session.pop("pending_phone_number", None)

        return user