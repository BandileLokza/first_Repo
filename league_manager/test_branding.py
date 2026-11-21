"""Football league/season branding management coverage."""

from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from league_manager.models import LeagueMembership
from leagues.models import Competition, CompetitionSeries, Region


GIF_BYTES = (
    b"GIF87a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def uploaded_gif(name):
    return SimpleUploadedFile(name, GIF_BYTES, content_type="image/gif")


class LocalBrandStorageMixin:
    """Keep upload tests fully local instead of contacting Supabase."""

    def setUp(self):
        super().setUp()
        self._brand_directory = TemporaryDirectory()
        self._fields = [
            Region._meta.get_field("logo"),
            CompetitionSeries._meta.get_field("logo"),
            Competition._meta.get_field("sponsor_logo"),
        ]
        self._original_storages = [field.storage for field in self._fields]
        for field in self._fields:
            field.storage = FileSystemStorage(
                location=self._brand_directory.name,
                base_url="/test-brand-media/",
            )

    def tearDown(self):
        for field, storage in zip(self._fields, self._original_storages):
            field.storage = storage
        self._brand_directory.cleanup()
        super().tearDown()


class LeagueBrandingManagementTests(LocalBrandStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.league = Competition.objects.create(
            name="Brand League", slug="brand-league", season="2026"
        )
        self.owner = get_user_model().objects.create_user("brand-owner", password="pass")
        self.agent = get_user_model().objects.create_user("brand-agent", password="pass")
        LeagueMembership.objects.create(
            user=self.owner, competition=self.league, role=LeagueMembership.Role.OWNER
        )
        LeagueMembership.objects.create(
            user=self.agent, competition=self.league, role=LeagueMembership.Role.AGENT
        )
        self.url = reverse("league_manager:update_league_branding", args=[self.league.slug])

    def test_owner_can_update_permanent_and_current_season_branding(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url,
            {
                "league_logo": uploaded_gif("league.gif"),
                "sponsor_name": "Season Sponsor",
                "sponsor_logo": uploaded_gif("sponsor.gif"),
            },
        )
        self.assertRedirects(
            response, reverse("league_manager:league_dashboard", args=[self.league.slug])
        )
        self.league.refresh_from_db()
        self.league.series.refresh_from_db()
        self.assertTrue(self.league.series.logo.name)
        self.assertEqual(self.league.sponsor_name, "Season Sponsor")
        self.assertTrue(self.league.sponsor_logo.name)

    def test_match_agent_cannot_update_branding(self):
        self.client.force_login(self.agent)
        response = self.client.post(self.url, {"sponsor_name": "Not allowed"})
        self.assertEqual(response.status_code, 403)
        self.league.refresh_from_db()
        self.assertFalse(self.league.sponsor_name)
