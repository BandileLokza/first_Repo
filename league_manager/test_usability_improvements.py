"""Regression coverage for the focused site-usability improvements."""

from datetime import timedelta
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import ActivityLog, LeagueMembership
from leagues.models import Competition, Group, Stage
from matches.models import Match, Player
from teams.models import Team


User = get_user_model()


# A valid one-pixel GIF.  It keeps ImageField validation realistic without
# depending on an external image file or the configured remote storage.
GIF_BYTES = (
    b"GIF87a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def uploaded_gif(name):
    return SimpleUploadedFile(name, GIF_BYTES, content_type="image/gif")


class LocalPlayerPhotoStorageMixin:
    """Prevent photo tests from contacting the explicit Supabase storage."""

    def setUp(self):
        super().setUp()
        self._photo_directory = TemporaryDirectory()
        self._photo_field = Player._meta.get_field("photo")
        self._original_photo_storage = self._photo_field.storage
        self._photo_storage = FileSystemStorage(
            location=self._photo_directory.name,
            base_url="/test-player-media/",
        )
        self._photo_field.storage = self._photo_storage

    def tearDown(self):
        self._photo_field.storage = self._original_photo_storage
        self._photo_directory.cleanup()
        super().tearDown()


class PlayerPhotoManagementTests(LocalPlayerPhotoStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.league = Competition.objects.create(
            name="Player Photo League",
            slug="player-photo-league",
            season="2026",
            format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.league,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        self.team = Team.objects.create(
            competition=self.league,
            name="Photo United",
        )
        self.manager = User.objects.create_user(
            username="photo-manager",
            password="pass",
        )
        LeagueMembership.objects.create(
            user=self.manager,
            competition=self.league,
            role=LeagueMembership.Role.OWNER,
        )
        self.client.force_login(self.manager)

    @property
    def create_url(self):
        return reverse("league_manager:player_create", args=[self.league.slug])

    def player_data(self, **overrides):
        data = {
            "name": "Player One",
            "team": str(self.team.pk),
            "jersey_number": "7",
            "starting_goals": "0",
        }
        data.update(overrides)
        return data

    def assert_message(self, response, text):
        self.assertIn(text, [str(message) for message in get_messages(response.wsgi_request)])

    def test_player_creation_accepts_no_photo_and_secondary_save_keeps_team_context(self):
        normal_response = self.client.post(self.create_url, self.player_data())
        self.assert_message(normal_response, "Player One was added successfully.")
        self.assertRedirects(
            normal_response,
            reverse("league_manager:player_list", args=[self.league.slug]),
        )

        without_photo = Player.objects.get(name="Player One")
        self.assertFalse(without_photo.photo)
        self.assertTrue(
            ActivityLog.objects.filter(
                competition=self.league,
                action=ActivityLog.Action.PLAYER_CREATED,
                new_values__player_id=without_photo.id,
            ).exists()
        )

        secondary_response = self.client.post(
            self.create_url,
            self.player_data(
                name="Player Two",
                jersey_number="8",
                photo=uploaded_gif("player-two.gif"),
                save_and_add_another="1",
            ),
        )
        expected_url = f"{self.create_url}?team={self.team.id}"
        self.assert_message(secondary_response, "Player Two was added successfully.")
        self.assertRedirects(secondary_response, expected_url)

        with_photo = Player.objects.get(name="Player Two")
        self.assertTrue(with_photo.photo.name.startswith("players/"))
        self.assertTrue(self._photo_storage.exists(with_photo.photo.name))

        response = self.client.get(expected_url)
        self.assertEqual(str(response.context["form"]["team"].value()), str(self.team.id))
        self.assertContains(response, 'name="save_and_add_another"')

    def test_player_photo_can_be_uploaded_replaced_and_cleared(self):
        create_response = self.client.post(
            self.create_url,
            self.player_data(photo=uploaded_gif("original.gif")),
        )
        self.assertRedirects(
            create_response,
            reverse("league_manager:player_list", args=[self.league.slug]),
        )
        player = Player.objects.get(name="Player One")
        original_name = player.photo.name
        self.assertTrue(self._photo_storage.exists(original_name))

        update_url = reverse(
            "league_manager:player_update",
            args=[self.league.slug, player.id],
        )
        replace_response = self.client.post(
            update_url,
            self.player_data(
                photo=uploaded_gif("replacement.gif"),
            ),
        )
        self.assertRedirects(
            replace_response,
            reverse("league_manager:player_list", args=[self.league.slug]),
        )
        player.refresh_from_db()
        self.assertNotEqual(player.photo.name, original_name)
        self.assertTrue(player.photo.name.startswith("players/"))
        self.assertTrue(self._photo_storage.exists(player.photo.name))

        clear_response = self.client.post(
            update_url,
            self.player_data(**{"photo-clear": "on"}),
        )
        self.assertRedirects(
            clear_response,
            reverse("league_manager:player_list", args=[self.league.slug]),
        )
        player.refresh_from_db()
        self.assertFalse(player.photo)

    def test_active_legacy_player_route_uses_the_same_upload_aware_form(self):
        staff_user = User.objects.create_user(
            username="legacy-player-staff",
            password="pass",
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.post(
            reverse("add_player", args=[self.team.id]),
            {
                "name": "Legacy Photo Player",
                "jersey_number": "12",
                "starting_goals": "0",
                "photo": uploaded_gif("legacy-player.gif"),
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard"),
            fetch_redirect_response=False,
        )
        player = Player.objects.get(name="Legacy Photo Player")
        self.assertEqual(player.team_id, self.team.id)
        self.assertTrue(self._photo_storage.exists(player.photo.name))


class SaveAndAddAnotherWorkflowTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="workflow-manager",
            password="pass",
        )
        self.agent = User.objects.create_user(
            username="workflow-agent",
            password="pass",
        )
        self.table_league = Competition.objects.create(
            name="Workflow Table League",
            slug="workflow-table-league",
            season="2026",
            format_type="TABLE",
        )
        self.table_stage = Stage.objects.create(
            competition=self.table_league,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        self.table_teams = [
            Team.objects.create(
                competition=self.table_league,
                name=f"Table Team {number}",
            )
            for number in range(1, 4)
        ]
        self.group_league = Competition.objects.create(
            name="Workflow Group League",
            slug="workflow-group-league",
            season="2026",
            format_type="GROUP_KNOCKOUT",
        )
        self.group_stage = Stage.objects.create(
            competition=self.group_league,
            name="Group Stage",
            stage_type="GROUP",
            order=1,
        )
        self.group_team = Team.objects.create(
            competition=self.group_league,
            name="Group Team 1",
        )
        for league in (self.table_league, self.group_league):
            LeagueMembership.objects.create(
                user=self.manager,
                competition=league,
                role=LeagueMembership.Role.ADMIN,
            )
            LeagueMembership.objects.create(
                user=self.agent,
                competition=league,
                role=LeagueMembership.Role.AGENT,
            )
        self.client.force_login(self.manager)

    def table_url(self, name, *args):
        return reverse(name, args=[self.table_league.slug, *args])

    def group_url(self, name, *args):
        return reverse(name, args=[self.group_league.slug, *args])

    def fixture_data(self, home, away):
        return {
            "stage": str(self.table_stage.id),
            "home_team": str(home.id),
            "away_team": str(away.id),
            "match_date": timezone.localtime(
                timezone.now() + timedelta(days=1)
            ).strftime("%Y-%m-%dT%H:%M"),
            "location": "Test Arena",
            "leg": "1",
        }

    def assert_message(self, response, text):
        self.assertIn(text, [str(message) for message in get_messages(response.wsgi_request)])

    def test_secondary_saves_keep_the_user_on_each_creation_form(self):
        team_create_url = self.table_url("league_manager:team_create")
        player_create_url = self.table_url("league_manager:player_create")
        fixture_create_url = self.table_url("league_manager:fixture_create")
        group_create_url = self.group_url("league_manager:group_create")

        cases = [
            (
                team_create_url,
                {"name": "Secondary Team", "save_and_add_another": "1"},
                team_create_url,
                "Secondary Team was added successfully.",
            ),
            (
                player_create_url,
                {
                    "name": "Secondary Player",
                    "team": str(self.table_teams[0].id),
                    "jersey_number": "9",
                    "starting_goals": "0",
                    "save_and_add_another": "1",
                },
                f"{player_create_url}?team={self.table_teams[0].id}",
                "Secondary Player was added successfully.",
            ),
            (
                fixture_create_url,
                {
                    **self.fixture_data(self.table_teams[0], self.table_teams[1]),
                    "save_and_add_another": "1",
                },
                fixture_create_url,
                "Table Team 1 vs Table Team 2 was created.",
            ),
            (
                group_create_url,
                {
                    "name": "Group A",
                    "teams": [str(self.group_team.id)],
                    "save_and_add_another": "1",
                },
                group_create_url,
                "Group A was created successfully.",
            ),
        ]

        for url, data, expected_url, message in cases:
            with self.subTest(url=url):
                response = self.client.post(url, data)
                self.assert_message(response, message)
                self.assertRedirects(response, expected_url)

    def test_normal_saves_return_to_the_existing_list_pages(self):
        team_create_url = self.table_url("league_manager:team_create")
        player_create_url = self.table_url("league_manager:player_create")
        fixture_create_url = self.table_url("league_manager:fixture_create")
        group_create_url = self.group_url("league_manager:group_create")

        cases = [
            (
                team_create_url,
                {"name": "Normal Team"},
                self.table_url("league_manager:team_list"),
            ),
            (
                player_create_url,
                {
                    "name": "Normal Player",
                    "team": str(self.table_teams[0].id),
                    "jersey_number": "10",
                    "starting_goals": "0",
                },
                self.table_url("league_manager:player_list"),
            ),
            (
                fixture_create_url,
                self.fixture_data(self.table_teams[1], self.table_teams[2]),
                self.table_url("league_manager:fixture_list"),
            ),
            (
                group_create_url,
                {"name": "Group B", "teams": [str(self.group_team.id)]},
                self.group_url("league_manager:group_list"),
            ),
        ]

        for url, data, expected_url in cases:
            with self.subTest(url=url):
                response = self.client.post(url, data)
                self.assertRedirects(response, expected_url)

        self.assertTrue(Match.objects.filter(competition=self.table_league).exists())
        self.assertTrue(Group.objects.filter(stage=self.group_stage, name="Group B").exists())

    def test_secondary_button_is_limited_to_creation_forms_and_agents_are_rejected(self):
        fixture = Match.objects.create(
            competition=self.table_league,
            stage=self.table_stage,
            home_team=self.table_teams[0],
            away_team=self.table_teams[1],
            match_date=timezone.now() + timedelta(days=2),
        )
        group = Group.objects.create(stage=self.group_stage, name="Existing Group")
        player = Player.objects.create(name="Existing Player", team=self.table_teams[0])

        create_urls = [
            self.table_url("league_manager:team_create"),
            self.table_url("league_manager:player_create"),
            self.table_url("league_manager:fixture_create"),
            self.group_url("league_manager:group_create"),
        ]
        update_urls = [
            self.table_url("league_manager:team_update", self.table_teams[0].id),
            self.table_url("league_manager:player_update", player.id),
            self.table_url("league_manager:fixture_update", fixture.id),
            self.group_url("league_manager:group_update", group.id),
        ]

        for url in create_urls:
            with self.subTest(create_url=url):
                self.assertContains(
                    self.client.get(url),
                    'name="save_and_add_another"',
                )

        for url in update_urls:
            with self.subTest(update_url=url):
                self.assertNotContains(
                    self.client.get(url),
                    'name="save_and_add_another"',
                )

        self.client.force_login(self.agent)
        for url in create_urls:
            with self.subTest(agent_url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
