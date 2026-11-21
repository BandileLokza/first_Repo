"""Public rendering regression coverage for player-photo usability."""

from tempfile import TemporaryDirectory

from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import Competition, Stage
from matches.models import Match, MatchLineup, MatchLineupEntry, Player
from teams.models import Team


GIF_BYTES = (
    b"GIF87a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def uploaded_gif(name):
    return SimpleUploadedFile(name, GIF_BYTES, content_type="image/gif")


class PublicLineupPlayerPhotoTests(TestCase):
    def setUp(self):
        self._photo_directory = TemporaryDirectory()
        self._photo_field = Player._meta.get_field("photo")
        self._original_photo_storage = self._photo_field.storage
        self._photo_storage = FileSystemStorage(
            location=self._photo_directory.name,
            base_url="/test-player-media/",
        )
        self._photo_field.storage = self._photo_storage
        super().setUp()

        # Anonymous pages include the authentication modal, which asks allauth
        # for the configured Google provider.  This local test registration is
        # only for template rendering; it never leaves the test database.
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-client",
            secret="test-secret",
        )
        social_app.sites.add(Site.objects.get_current())

        self.league = Competition.objects.create(
            name="Public Lineup League",
            slug="public-lineup-league",
            season="2026",
            format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.league,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        self.home_team = Team.objects.create(
            competition=self.league,
            name="Home Photos",
        )
        self.away_team = Team.objects.create(
            competition=self.league,
            name="Away Photos",
        )
        self.match = Match.objects.create(
            competition=self.league,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
        )
        self.player_with_photo = Player.objects.create(
            name="Photo Player",
            team=self.home_team,
            starting_goals=1,
            photo=uploaded_gif("photo-player.gif"),
        )
        self.player_without_photo = Player.objects.create(
            name="Fallback Player",
            team=self.home_team,
        )
        self.away_player = Player.objects.create(
            name="Away Player",
            team=self.away_team,
        )

        home_lineup = MatchLineup.objects.create(
            match=self.match,
            team=self.home_team,
        )
        MatchLineupEntry.objects.create(
            lineup=home_lineup,
            player=self.player_with_photo,
            role=MatchLineupEntry.Role.STARTER,
            sort_order=1,
        )
        MatchLineupEntry.objects.create(
            lineup=home_lineup,
            player=self.player_without_photo,
            role=MatchLineupEntry.Role.STARTER,
            sort_order=2,
        )
        away_lineup = MatchLineup.objects.create(
            match=self.match,
            team=self.away_team,
        )
        MatchLineupEntry.objects.create(
            lineup=away_lineup,
            player=self.away_player,
            role=MatchLineupEntry.Role.STARTER,
            sort_order=1,
        )

    def tearDown(self):
        self._photo_field.storage = self._original_photo_storage
        self._photo_directory.cleanup()
        super().tearDown()

    def test_public_match_lineups_show_photo_thumbnails_and_name_fallbacks(self):
        response = self.client.get(reverse("match_detail", args=[self.match.id]))

        self.assertContains(response, 'class="player-photo-thumb"')
        self.assertContains(response, 'class="player-photo-fallback"')
        self.assertContains(response, "Photo Player")
        self.assertContains(response, "Fallback Player")
        self.assertContains(response, "Away Player")
        self.assertContains(response, "Photo Player profile photo")
        self.assertContains(response, ">F</span>")

    def test_public_stats_do_not_render_player_photo_thumbnails(self):
        response = self.client.get(reverse("league_stats", args=[self.league.slug]))

        self.assertContains(response, "Photo Player")
        self.assertNotContains(response, "player-photo-thumb")
        self.assertNotContains(response, "player-photo-fallback")
