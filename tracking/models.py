from django.db import models
from django.contrib.auth.models import User


class PageVisit(models.Model):
    session_key = models.CharField(max_length=100, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    path = models.CharField(max_length=255, db_index=True)
    referrer = models.TextField(blank=True, null=True)
    user_agent = models.TextField(blank=True, null=True)
    visited_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.path} - {self.visited_at:%Y-%m-%d %H:%M}"