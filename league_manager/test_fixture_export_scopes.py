"""Regression tests for additive complete-round fixture exports."""

from datetime import date, datetime
from html import unescape
from io import BytesIO
from unittest.mock import patch
from zoneinfo import ZoneInfo
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import RegionalAdministrator, RegionalReportPreference
from league_manager.test_fixture_letterheads import pdf_text
from league_manager.regional_reports import (
    FIXTURE_SCOPE_FIRST_ROUND,
    FIXTURE_SCOPE_FULL_SEASON,
    FIXTURE_SCOPE_NEXT_UPCOMING,
    FIXTURE_SCOPE_SECOND_ROUND,
    WEEKLY_SELECTION_MANUAL,
    _fixture_selection,
    _groups,
    build_regional_report_data,
    render_docx,
    render_pdf,
    report_text_lines,
)
from leagues.fixtures import build_generation_preview, confirm_generation_preview
from leagues.models import Competition, CompetitionSeries, Region, Stage
from matches.models import Match
from teams.models import Team


User = get_user_model()
ESWATINI = ZoneInfo("Africa/Mbabane")


class FixtureExportScopeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="fixture-report-admin", password="pass")
        self.region = Region.objects.create(name="Hhohho", slug="fixture-report-hhohho")
        RegionalAdministrator.objects.create(user=self.admin, region=self.region)
        self.competition, self.stage, self.teams = self.make_competition(
            "Hhohho Premier", "fixture-report-hhohho-premier"
        )
        self.preference = RegionalReportPreference.objects.create(
            user=self.admin,
            region=self.region,
            report_kind=RegionalReportPreference.ReportKind.FIXTURES,
        )
        self.client.force_login(self.admin)

    def make_competition(self, name, slug, *, region=None):
        series = CompetitionSeries.objects.create(
            name=f"{name} Series",
            slug=f"{slug}-series",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=region or self.region,
        )
        competition = Competition.objects.create(
            name=name,
            slug=slug,
            season="2026/2027",
            series=series,
            format_type="TABLE",
        )
        stage = Stage.objects.create(
            competition=competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        teams = [
            Team.objects.create(competition=competition, name=f"{name} Team {letter}")
            for letter in "ABCD"
        ]
        return competition, stage, teams

    def add_fixture(
        self,
        *,
        home,
        away,
        gameweek,
        leg,
        status="UPCOMING",
        match_date=None,
        location="Mavuso Ground",
        generated=False,
        competition=None,
        stage=None,
    ):
        return Match.objects.create(
            competition=competition or self.competition,
            stage=stage or self.stage,
            home_team=home,
            away_team=away,
            gameweek=gameweek,
            leg=leg,
            status=status,
            match_date=match_date,
            location=location,
            generated_by_system=generated,
        )

    def add_complete_even_schedule(self):
        a, b, c, d = self.teams
        first_round = (
            (1, a, b), (1, c, d),
            (2, a, c), (2, b, d),
            (3, a, d), (3, b, c),
        )
        for gameweek, home, away in first_round:
            self.add_fixture(
                home=home,
                away=away,
                gameweek=gameweek,
                leg=1,
                match_date=timezone.make_aware(datetime(2026, 8, gameweek, 15, 0)),
                generated=True,
            )
        for gameweek, home, away in first_round:
            self.add_fixture(
                home=away,
                away=home,
                gameweek=gameweek + 3,
                leg=2,
                status="FINISHED",
                match_date=timezone.make_aware(datetime(2026, 9, gameweek, 15, 0)),
                generated=True,
            )

    def report(
        self,
        fixture_scope=FIXTURE_SCOPE_NEXT_UPCOMING,
        report_date=None,
        fixture_gameweek_mode="AUTOMATIC",
        manual_gameweeks=None,
    ):
        return build_regional_report_data(
            region=self.region,
            preference=self.preference,
            fixture_scope=fixture_scope,
            report_date=report_date,
            fixture_gameweek_mode=fixture_gameweek_mode,
            manual_gameweeks=manual_gameweeks,
        )

    def test_omitted_scope_uses_the_date_aware_weekly_default(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 18, 15, tzinfo=ESWATINI),
        )
        self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )

        default_matches, default_label = _fixture_selection(
            self.competition, report_date=report_date
        )
        default_data = self.report(report_date=report_date)
        explicit_default_data = self.report(
            FIXTURE_SCOPE_NEXT_UPCOMING, report_date=report_date
        )

        self.assertEqual(default_data, explicit_default_data)
        self.assertEqual(default_label, "Gameweek 5")
        self.assertEqual(default_data["entries"][0]["round_label"], default_label)
        self.assertEqual(
            default_data["entries"][0]["sections"],
            _groups(
                list(default_matches),
                report_kind="FIXTURES",
                report_date=default_data["report_date"],
            ),
        )

        export_url = reverse(
            "league_manager:regional_report_export", args=[self.region.slug, "fixtures"]
        )
        response = self.client.get(export_url)
        self.assertContains(response, 'value="NEXT_UPCOMING" checked')
        self.assertContains(response, "Next Upcoming Gameweek")

        # A POST with no fixture_scope value must still use the corrected
        # default weekly branch rather than one of the complete-round scopes.
        with patch("league_manager.regional_reports.timezone.localdate", return_value=report_date):
            response = self.client.post(export_url, {
                "selection_mode": "ALL",
                "output_format": "DOCX",
                "action": "download",
            })
        self.assertTrue(response.content.startswith(b"PK"))
        self.assertIn("_WEEKEND_FIXTURES_", response["Content-Disposition"])
        with ZipFile(BytesIO(response.content)) as document:
            text = unescape(document.read("word/document.xml").decode("utf-8"))
        self.assertIn(c.name, text)
        self.assertNotIn(a.name, text)

    def test_overdue_gameweek_does_not_displace_the_next_confirmed_programme(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        overdue = self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 18, 15, tzinfo=ESWATINI),
        )
        self.add_fixture(
            home=c,
            away=d,
            gameweek=4,
            leg=1,
            status="FINISHED",
            match_date=datetime(2026, 10, 18, 17, tzinfo=ESWATINI),
        )
        gw5_first = self.add_fixture(
            home=a,
            away=c,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        gw5_second = self.add_fixture(
            home=b,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 25, 15, tzinfo=ESWATINI),
        )

        data = self.report(report_date=report_date)
        lines = "\n".join(report_text_lines(data))

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn(gw5_first.home_team.name, lines)
        self.assertIn(gw5_second.home_team.name, lines)
        self.assertNotIn(overdue.home_team.name + "\tvs\t" + overdue.away_team.name, lines)
        self.assertEqual(data["entries"][0]["additional_sections"], [])

    def test_rescheduled_earlier_gameweek_is_added_below_the_main_programme(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        rescheduled = self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 24, 12, tzinfo=ESWATINI),
        )
        rescheduled_second = self.add_fixture(
            home=b,
            away=d,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 25, 12, tzinfo=ESWATINI),
        )
        gw5_first = self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )

        data = self.report(report_date=report_date)
        lines = report_text_lines(data)
        text = "\n".join(lines)

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn(gw5_first.home_team.name + "\tvs\t" + gw5_first.away_team.name, text)
        self.assertIn("Additional Fixtures from Earlier Gameweeks", lines)
        self.assertIn(rescheduled.home_team.name + "\tvs\t" + rescheduled.away_team.name, text)
        self.assertIn(
            rescheduled_second.home_team.name + "\tvs\t" + rescheduled_second.away_team.name,
            text,
        )
        self.assertTrue(
            any("24 October 2026" in line and line.endswith("GW4") for line in lines)
        )

    def test_finished_gameweek_is_ignored_in_favour_of_the_next_upcoming_gameweek(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        previous = self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            status="FINISHED",
            match_date=datetime(2026, 10, 18, 15, tzinfo=ESWATINI),
        )
        future = self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )

        data = self.report(report_date=report_date)
        text = "\n".join(report_text_lines(data))

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn(future.home_team.name + "\tvs\t" + future.away_team.name, text)
        self.assertNotIn(previous.home_team.name + "\tvs\t" + previous.away_team.name, text)

    def test_tba_fixtures_do_not_outrank_confirmed_fixtures_and_remain_in_main_gameweek(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        previous_tba = self.add_fixture(home=a, away=b, gameweek=4, leg=1)
        confirmed = self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        current_tba = self.add_fixture(home=a, away=c, gameweek=5, leg=1)

        data = self.report(report_date=report_date)
        text = "\n".join(report_text_lines(data))

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn(confirmed.home_team.name + "\tvs\t" + confirmed.away_team.name, text)
        self.assertIn(current_tba.home_team.name + "\tvs\t" + current_tba.away_team.name, text)
        self.assertIn("Date to be confirmed \u2014 GW5", text)
        self.assertNotIn(previous_tba.home_team.name + "\tvs\t" + previous_tba.away_team.name, text)

    def test_all_tba_fixtures_use_an_explicit_fallback_warning(self):
        a, b, c, d = self.teams
        self.add_fixture(home=a, away=b, gameweek=4, leg=1)
        self.add_fixture(home=c, away=d, gameweek=5, leg=1)
        self.add_fixture(home=a, away=c, gameweek=5, leg=1)

        data = self.report(report_date=date(2026, 10, 20))

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn("no confirmed future fixture dates", data["warnings"][0].lower())
        self.assertIn("no confirmed future fixture dates", "\n".join(report_text_lines(data)).lower())

    def test_multiple_gameweeks_in_one_reporting_week_have_a_deterministic_tie_break(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        earlier = self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        selected = self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 25, 15, tzinfo=ESWATINI),
        )

        data = self.report(report_date=report_date)
        text = "\n".join(report_text_lines(data))

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn("span GW4, GW5", "\n".join(data["entries"][0]["notes"]))
        self.assertIn(selected.home_team.name + "\tvs\t" + selected.away_team.name, text)
        self.assertIn(earlier.home_team.name + "\tvs\t" + earlier.away_team.name, text)

    def test_selected_gameweek_keeps_its_future_fixtures_across_reporting_week_boundary(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 19)  # Monday
        saturday = self.add_fixture(
            home=a,
            away=b,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        next_monday = self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 26, 15, tzinfo=ESWATINI),
        )

        data = self.report(report_date=report_date)
        text = "\n".join(report_text_lines(data))

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 5")
        self.assertIn(saturday.home_team.name + "\tvs\t" + saturday.away_team.name, text)
        self.assertIn(next_monday.home_team.name + "\tvs\t" + next_monday.away_team.name, text)

    def test_administrator_can_manually_select_a_gameweek_with_every_status(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        override_match = self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 24, 12, tzinfo=ESWATINI),
        )
        finished_match = self.add_fixture(
            home=b,
            away=d,
            gameweek=4,
            leg=1,
            status="FINISHED",
            match_date=datetime(2026, 10, 18, 12, tzinfo=ESWATINI),
        )
        live_match = self.add_fixture(
            home=a,
            away=d,
            gameweek=4,
            leg=1,
            status="LIVE",
            match_date=datetime(2026, 10, 18, 15, tzinfo=ESWATINI),
        )
        automatic_match = self.add_fixture(
            home=c,
            away=d,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        self.add_fixture(
            home=a,
            away=c,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 25, 15, tzinfo=ESWATINI),
        )

        data = self.report(
            report_date=report_date,
            fixture_gameweek_mode=WEEKLY_SELECTION_MANUAL,
            manual_gameweeks={self.competition.id: 4},
        )
        text = "\n".join(report_text_lines(data))
        export_url = reverse(
            "league_manager:regional_report_export", args=[self.region.slug, "fixtures"]
        )
        response = self.client.get(export_url)
        self.assertContains(response, 'name="fixture_gameweek_mode"')
        self.assertContains(response, f'name="manual_gameweek_{self.competition.id}"')
        with patch("league_manager.regional_reports.timezone.localdate", return_value=report_date):
            response = self.client.post(export_url, {
                "selection_mode": "ALL",
                "output_format": "DOCX",
                "fixture_scope": FIXTURE_SCOPE_NEXT_UPCOMING,
                "fixture_gameweek_mode": "MANUAL",
                f"manual_gameweek_{self.competition.id}": "4",
                "action": "download",
            })

        self.assertEqual(data["entries"][0]["round_label"], "Gameweek 4")
        self.assertIn(override_match.home_team.name + "\tvs\t" + override_match.away_team.name, text)
        self.assertIn(finished_match.home_team.name + "\tvs\t" + finished_match.away_team.name, text)
        self.assertIn(live_match.home_team.name + "\tvs\t" + live_match.away_team.name, text)
        self.assertIn("Status: Upcoming", text)
        self.assertIn("Status: Finished", text)
        self.assertIn("Status: Live", text)
        self.assertNotIn(automatic_match.home_team.name + "\tvs\t" + automatic_match.away_team.name, text)
        self.assertTrue(render_docx(data).startswith(b"PK"))
        self.assertTrue(render_pdf(data).startswith(b"%PDF"))
        self.assertTrue(response.content.startswith(b"PK"))

    def test_manual_selection_uses_a_separate_gameweek_for_each_selected_competition(self):
        a, b, _, _ = self.teams
        report_date = date(2026, 10, 20)
        self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            status="FINISHED",
            match_date=datetime(2026, 10, 18, 15, tzinfo=ESWATINI),
        )
        other, other_stage, other_teams = self.make_competition(
            "Hhohho Division One", "fixture-report-hhohho-division-one"
        )
        other_match = self.add_fixture(
            competition=other,
            stage=other_stage,
            home=other_teams[0],
            away=other_teams[1],
            gameweek=7,
            leg=1,
            match_date=datetime(2026, 10, 25, 15, tzinfo=ESWATINI),
        )

        data = self.report(
            report_date=report_date,
            fixture_gameweek_mode=WEEKLY_SELECTION_MANUAL,
            manual_gameweeks={self.competition.id: 4, other.id: 7},
        )

        self.assertEqual(
            {entry["competition"]: entry["round_label"] for entry in data["entries"]},
            {self.competition.name: "Gameweek 4", other.name: "Gameweek 7"},
        )
        self.assertIn(other_match.home_team.name, "\n".join(report_text_lines(data)))

    def test_manual_selection_rejects_invalid_gameweeks_and_ignores_unauthorised_competitions(self):
        a, b, _, _ = self.teams
        report_date = date(2026, 10, 20)
        allowed_match = self.add_fixture(
            home=a,
            away=b,
            gameweek=4,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        foreign_region = Region.objects.create(name="Manzini", slug="fixture-report-manzini")
        foreign, foreign_stage, foreign_teams = self.make_competition(
            "Manzini Premier", "fixture-report-manzini-premier", region=foreign_region
        )
        foreign_match = self.add_fixture(
            competition=foreign,
            stage=foreign_stage,
            home=foreign_teams[0],
            away=foreign_teams[1],
            gameweek=3,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        export_url = reverse(
            "league_manager:regional_report_export", args=[self.region.slug, "fixtures"]
        )
        base_post = {
            "selection_mode": "ALL",
            "output_format": "DOCX",
            "fixture_scope": FIXTURE_SCOPE_NEXT_UPCOMING,
            "fixture_gameweek_mode": "MANUAL",
            f"manual_gameweek_{self.competition.id}": "4",
            "action": "download",
        }

        invalid = self.client.post(
            export_url,
            {**base_post, f"manual_gameweek_{self.competition.id}": "999"},
        )
        response = self.client.post(
            export_url,
            {**base_post, f"manual_gameweek_{foreign.id}": "3"},
        )

        self.assertEqual(invalid.status_code, 200)
        self.assertNotIn("Content-Disposition", invalid.headers)
        self.assertTrue(response.content.startswith(b"PK"))
        with ZipFile(BytesIO(response.content)) as document:
            text = unescape(document.read("word/document.xml").decode("utf-8"))
        self.assertIn(allowed_match.home_team.name, text)
        self.assertNotIn(foreign_match.home_team.name, text)

    def test_each_competition_selects_its_own_next_gameweek_and_weekly_report_renders_both_formats(self):
        a, b, c, d = self.teams
        report_date = date(2026, 10, 20)
        self.add_fixture(
            home=a,
            away=b,
            gameweek=5,
            leg=1,
            match_date=datetime(2026, 10, 24, 15, tzinfo=ESWATINI),
        )
        other, other_stage, other_teams = self.make_competition(
            "Hhohho Division One", "fixture-report-hhohho-division-one"
        )
        other_fixture = self.add_fixture(
            competition=other,
            stage=other_stage,
            home=other_teams[0],
            away=other_teams[1],
            gameweek=7,
            leg=1,
            match_date=datetime(2026, 10, 25, 15, tzinfo=ESWATINI),
        )

        data = self.report(report_date=report_date)

        labels = {entry["competition"]: entry["round_label"] for entry in data["entries"]}
        self.assertEqual(labels, {
            self.competition.name: "Gameweek 5",
            other.name: "Gameweek 7",
        })
        self.assertIn(other_fixture.home_team.name, "\n".join(report_text_lines(data)))
        self.assertTrue(render_docx(data).startswith(b"PK"))
        self.assertTrue(render_pdf(data).startswith(b"%PDF"))

    def test_even_team_first_second_and_full_season_exports_use_persisted_legs(self):
        self.add_complete_even_schedule()

        first = self.report(FIXTURE_SCOPE_FIRST_ROUND)
        second = self.report(FIXTURE_SCOPE_SECOND_ROUND)
        season = self.report(FIXTURE_SCOPE_FULL_SEASON)

        self.assertEqual(
            [section["date_label"] for section in first["entries"][0]["sections"]],
            ["Gameweek 1", "Gameweek 2", "Gameweek 3"],
        )
        self.assertEqual(
            [section["date_label"] for section in second["entries"][0]["sections"]],
            ["Gameweek 4", "Gameweek 5", "Gameweek 6"],
        )
        self.assertEqual(sum(line.count("\tvs\t") for line in report_text_lines(first)), 6)
        self.assertIn("FIRST ROUND FIXTURES", report_text_lines(first))
        self.assertIn("SEASON: 2026/2027", report_text_lines(first))
        self.assertIn("Hhohho Premier Team B\tvs\tHhohho Premier Team A", "\n".join(report_text_lines(second)))
        self.assertIn("FULL SEASON FIXTURES", report_text_lines(season))
        self.assertEqual(sum(line.count("\tvs\t") for line in report_text_lines(season)), 12)

        for report, title, match_count in (
            (first, "FIRST ROUND FIXTURES", 6),
            (second, "SECOND ROUND FIXTURES", 6),
            (season, "FULL SEASON FIXTURES", 12),
        ):
            with self.subTest(title=title):
                text = pdf_text(render_pdf(report))
                self.assertIn(title, text)
                self.assertEqual(text.split().count("VS"), match_count)
                self.assertIn("KICKOFF", text)
                self.assertIn("3:00 PM", text)

    def test_odd_team_round_boundaries_are_not_hard_coded(self):
        self.competition.teams.all().delete()
        teams = [
            Team.objects.create(competition=self.competition, name=f"Odd Team {letter}")
            for letter in "ABC"
        ]
        a, b, c = teams
        for gameweek, home, away in ((1, a, b), (2, a, c), (3, b, c)):
            self.add_fixture(home=home, away=away, gameweek=gameweek, leg=1)
            self.add_fixture(home=away, away=home, gameweek=gameweek + 3, leg=2)

        first = self.report(FIXTURE_SCOPE_FIRST_ROUND)
        second = self.report(FIXTURE_SCOPE_SECOND_ROUND)
        self.assertEqual(
            [section["date_label"] for section in first["entries"][0]["sections"]],
            ["Gameweek 1", "Gameweek 2", "Gameweek 3"],
        )
        self.assertEqual(
            [section["date_label"] for section in second["entries"][0]["sections"]],
            ["Gameweek 4", "Gameweek 5", "Gameweek 6"],
        )

    def test_manual_tba_postponed_and_incomplete_fixtures_are_exported_without_invention(self):
        a, b, c, d = self.teams
        self.add_fixture(home=a, away=b, gameweek=None, leg=1, match_date=None)
        self.add_fixture(
            home=c,
            away=d,
            gameweek=2,
            leg=1,
            status="FINISHED",
            match_date=timezone.make_aware(datetime(2026, 12, 4, 18, 30)),
        )

        first = self.report(FIXTURE_SCOPE_FIRST_ROUND)
        lines = report_text_lines(first)
        self.assertIn("Unassigned gameweek", lines)
        self.assertIn("TBA", "\n".join(lines))
        self.assertIn("Some recorded fixtures have no gameweek", "\n".join(lines))
        self.assertEqual(sum(line.count("\tvs\t") for line in lines), 2)
        self.assertEqual(first["warnings"], [])

        second = self.report(FIXTURE_SCOPE_SECOND_ROUND)
        self.assertEqual(second["entries"], [])
        self.assertIn("no recorded second round fixtures", second["warnings"][0].lower())

    def test_continuation_generated_fixtures_remain_in_the_first_round(self):
        a, b, _, _ = self.teams
        self.add_fixture(home=a, away=b, gameweek=1, leg=1, match_date=None)

        preview = build_generation_preview(self.competition, "remaining_first")
        confirm_generation_preview(
            self.competition,
            preview["token"],
            action="remaining_first",
        )

        first = self.report(FIXTURE_SCOPE_FIRST_ROUND)
        self.assertEqual(sum(line.count("\tvs\t") for line in report_text_lines(first)), 6)
        self.assertTrue(Match.objects.filter(
            competition=self.competition,
            leg=1,
            generated_by_system=True,
        ).exists())

    def test_all_and_custom_selection_remain_independent_of_fixture_scope(self):
        a, b, _, _ = self.teams
        self.add_fixture(home=a, away=b, gameweek=1, leg=1)
        other, other_stage, other_teams = self.make_competition(
            "Hhohho Division One", "fixture-report-hhohho-division-one"
        )
        self.add_fixture(
            competition=other,
            stage=other_stage,
            home=other_teams[0],
            away=other_teams[1],
            gameweek=1,
            leg=1,
        )

        all_data = self.report(FIXTURE_SCOPE_FIRST_ROUND)
        self.assertEqual(len(all_data["entries"]), 2)
        self.preference.selection_mode = RegionalReportPreference.SelectionMode.CUSTOM
        self.preference.save(update_fields=["selection_mode"])
        self.preference.selected_series.add(self.competition.series)
        custom_data = self.report(FIXTURE_SCOPE_FIRST_ROUND)
        self.assertEqual([entry["competition"] for entry in custom_data["entries"]], [self.competition.name])

    def test_scoped_downloads_render_pdf_and_docx_and_reject_unsupported_data(self):
        a, b, _, _ = self.teams
        self.add_fixture(home=a, away=b, gameweek=1, leg=1)
        export_url = reverse(
            "league_manager:regional_report_export", args=[self.region.slug, "fixtures"]
        )
        for output_format, magic in (("PDF", b"%PDF"), ("DOCX", b"PK")):
            with self.subTest(output_format=output_format):
                response = self.client.post(export_url, {
                    "selection_mode": "ALL",
                    "output_format": output_format,
                    "fixture_scope": FIXTURE_SCOPE_FIRST_ROUND,
                    "action": "download",
                })
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content.startswith(magic))

        self.competition.format_type = "KNOCKOUT"
        self.competition.save(update_fields=["format_type"])
        response = self.client.post(export_url, {
            "selection_mode": "ALL",
            "output_format": "DOCX",
            "fixture_scope": FIXTURE_SCOPE_FIRST_ROUND,
            "action": "download",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fixture export warning")
        self.assertNotIn("Content-Disposition", response.headers)
