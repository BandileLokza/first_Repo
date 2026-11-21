"""Regression coverage for homepage-banner usability."""

from tempfile import TemporaryDirectory

from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from leagues.models import HomepageBanner


GIF_BYTES = (
    b"GIF87a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def uploaded_gif(name):
    return SimpleUploadedFile(name, GIF_BYTES, content_type="image/gif")


class HomepageBannerUsabilityTests(TestCase):
    def setUp(self):
        self._banner_directory = TemporaryDirectory()
        self._image_field = HomepageBanner._meta.get_field("image")
        self._original_storage = self._image_field.storage
        self._image_field.storage = FileSystemStorage(
            location=self._banner_directory.name,
            base_url="/test-banner-media/",
        )
        super().setUp()

        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-client",
            secret="test-secret",
        )
        social_app.sites.add(Site.objects.get_current())

    def tearDown(self):
        self._image_field.storage = self._original_storage
        self._banner_directory.cleanup()
        super().tearDown()

    def test_image_only_banner_is_valid_and_renders_without_empty_copy_or_overlay(self):
        banner = HomepageBanner(image=uploaded_gif("image-only.gif"))
        banner.full_clean()
        banner.save()

        self.assertEqual(str(banner), f"Banner #{banner.id}")
        self.assertTrue(self._image_field.storage.exists(banner.image.name))

        response = self.client.get(reverse("league_list"))
        self.assertContains(response, 'alt="Homepage banner"')
        self.assertNotContains(response, "homepage-hero-title")
        self.assertNotContains(response, "homepage-hero-overlay")
        self.assertNotContains(response, "homepage-hero-content")

    def test_titled_banner_keeps_its_existing_hero_copy(self):
        HomepageBanner.objects.create(
            title="Finals Weekend",
            subtitle="Watch every match live.",
            image=uploaded_gif("titled-banner.gif"),
        )

        response = self.client.get(reverse("league_list"))
        self.assertContains(response, "Finals Weekend")
        self.assertContains(response, "Watch every match live.")
        self.assertContains(response, "homepage-hero-title")
        self.assertContains(response, "homepage-hero-overlay")
