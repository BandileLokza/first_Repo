from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.test import TestCase
from django.urls import reverse


class SocialProviderTemplateTests(TestCase):
    """Regression coverage for local databases without a Google SocialApp."""

    def setUp(self):
        self.site = Site.objects.get_current()
        SocialApp.objects.all().delete()

    def configure_google_for_current_site(self):
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-google-client-id",
            secret="test-google-secret",
        )
        social_app.sites.add(self.site)
        return social_app

    def test_pool_home_loads_without_a_social_app(self):
        response = self.client.get(reverse("pool:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="signInForm"')
        self.assertNotContains(response, "Continue with Google")

    def test_manual_login_page_loads_without_a_social_app(self):
        response = self.client.get(reverse("accounts:login_page"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="signInForm"')
        self.assertNotContains(response, "Continue with Google")

    def test_allauth_login_page_loads_without_a_social_app(self):
        response = self.client.get(reverse("account_login"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Continue with Google")

    def test_google_controls_render_when_google_is_configured_for_current_site(self):
        self.configure_google_for_current_site()

        response = self.client.get(reverse("pool:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with Google", count=2)
        self.assertContains(
            response,
            'href="/accounts/google/login/?next=/pool/"',
            count=2,
        )
