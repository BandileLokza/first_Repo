from django import forms
from .models import UserProfile


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ["display_name", "phone_number"]


# ===== CHANGED =====
# Old long account-creation form removed
class JoinPredictionsForm(forms.Form):
    display_name = forms.CharField(max_length=50)
    phone_number = forms.CharField(max_length=20)

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()

    def clean_phone_number(self):
        return self.cleaned_data["phone_number"].strip()