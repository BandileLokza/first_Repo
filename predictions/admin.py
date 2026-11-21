from django.contrib import admin
from .models import PredictionMatch, Prediction, PredictionHistory, UserProfile, PredictionSponsor

admin.site.register(PredictionMatch)
admin.site.register(Prediction)
admin.site.register(PredictionHistory)
admin.site.register(UserProfile)


@admin.register(PredictionSponsor)
class PredictionSponsorAdmin(admin.ModelAdmin):
    list_display = ("competition", "sponsor_name", "page", "is_active", "created_at")
    list_filter = ("competition", "page", "is_active")
    search_fields = ("competition__name", "sponsor_name")