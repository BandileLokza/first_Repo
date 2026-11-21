from datetime import date, datetime
from io import BytesIO
from zipfile import ZipFile

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from docx import Document
from PIL import Image

from league_manager import test_fixture_letterheads as fixtures
from league_manager.models import LeagueMembership, OrganisationAdministrator, RegionalReportPreference
from league_manager.regional_reports import build_regional_report_data, render_docx, render_pdf
from league_manager.test_fixture_letterheads import image_refs, pdf_pages, pdf_text
from league_manager.test_word_report_exports import body_text, native_template
from leagues.models import ReportCommitteeMember, ReportDocumentSettings


def artwork(name, color):
    output = BytesIO()
    Image.new("RGBA", (240, 90), color).save(output, "PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


class OfficialDocumentTestCase(TestCase):
    def setUp(self):
        self.f = fixtures.FixtureLetterheadTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def settings(self, owner, **changes):
        return ReportDocumentSettings.objects.create(
            **ReportDocumentSettings.owner_filter(owner),
            **{"signoff_enabled": True, "signatory_name": "Test Signatory", "signatory_position": "Secretary", **changes},
        )

    def rows(self, data):
        return data["entries"][0]["sections"][0]["streams"][0]["venues"][0]["groups"][0]["rows"]


class OfficialPDFTests(OfficialDocumentTestCase):
    def test_match_columns_use_printable_width_and_keep_wrapped_names_in_their_cell(self):
        data = self.f.data([self.f.regional])
        rows = self.rows(data)
        rows[0].update(home="Mahlanya International Development and Community Football Club", away="Away Athletic", pdf_time="1:00 PM")
        rows.append({**rows[0], "home": "Mahlanya United", "away": "Away United", "pdf_time": "3:00 PM"})
        pdf = render_pdf(data)
        page = pdf_pages(pdf)[0]
        found = {}

        def capture(text, graphics, position, font, size):
            if text.strip():
                found[text.strip()] = (position[4] + graphics[4], position[5] + graphics[5])

        page.extract_text(visitor_text=capture)
        name_lines = [text for text in found if text.startswith("Mahlanya International") or text.endswith("Football Club")]
        self.assertGreaterEqual(len(name_lines), 2, found)
        self.assertTrue(all(found["Mahlanya United"][0] == found[text][0] for text in name_lines))
        self.assertEqual(found["Away Athletic"][0], found["Away United"][0])
        self.assertEqual(found["1:00 PM"][0], found["3:00 PM"][0])
        self.assertGreater(found["1:00 PM"][0] - found["Mahlanya United"][0], 450)
        self.assertTrue(found["Mahlanya United"][0] < found["VS"][0] < found["Away United"][0] < found["3:00 PM"][0])
        self.assertIn("and Community", " ".join(page.extract_text().split()))

    def test_continuation_pages_preserve_row_integrity_venue_header_and_link(self):
        data = self.f.data([self.f.regional])
        rows = self.rows(data)
        rows[:] = [{**rows[0], "home": f"Home {i:03}", "away": f"Away {i:03}"} for i in range(88)]
        pages = pdf_pages(render_pdf(data))
        self.assertGreater(len(pages), 2)
        texts = [page.extract_text() for page in pages]
        for page, text in zip(pages, texts):
            self.assertIn("Mavuso Ground", text)
            self.assertIn("KICKOFF", text)
            self.assertIn("Date to be confirmed", text)
            self.assertIn("TBA", text)
            self.assertEqual(text.count("Home "), text.count("Away "))
            self.assertIn("Follow live scores, fixtures & standings", text)
            self.assertEqual(page["/Annots"][0].get_object()["/A"]["/URI"], "https://www.esportslive.site")
        for i in range(88):
            matching = [text for text in texts if f"Home {i:03}" in text]
            self.assertEqual(len(matching), 1)
            self.assertIn(f"Away {i:03}", matching[0])
        self.assertEqual(len(set.union(*(image_refs(page) for page in pages))), 1)

    def test_results_align_score_and_preserve_penalties_and_notes(self):
        self.f.regional.matches.update(status="FINISHED", home_score=2, away_score=2, penalty_home_score=4, penalty_away_score=5, abandonment_resolution="CONFIRMED")
        self.f.regional.teams.filter(name="Regional A").update(name="Étoile & United")
        pdf = render_pdf(self.f.data([self.f.regional], "RESULTS"))
        for value in ("SCORE", "Étoile & United", "(4)2-2(5)", "Regional B", "Score confirmed after abandonment"):
            self.assertIn(value, pdf_text(pdf))

    def test_region_has_one_unsigned_closing_after_final_match_in_both_formats(self):
        another = self.f.make_competition("Another Regional", "REGIONAL", self.f.region)
        self.settings(self.f.region, signature_image=artwork("signature.png", "red").read(), stamp_image=artwork("stamp.png", "green").read())
        data = self.f.data([self.f.regional, another])
        self.rows(data)[:] *= 60
        pages = pdf_pages(render_pdf(data))
        self.assertGreater(len(pages), 2)
        self.assertNotIn("Yours Faithfully", "\n".join(page.extract_text() for page in pages[:-1]))
        final = pages[-1].extract_text()
        self.assertEqual(final.count("Yours Faithfully"), 1)
        self.assertLess(final.index("Another Regional B"), final.index("Yours Faithfully"))
        self.assertIn("Test Signatory", final)
        self.assertEqual(len(image_refs(pages[-1])), 1)
        word = body_text(render_docx(data))
        self.assertEqual(word.count("Test Signatory"), 1)
        self.assertLess(word.index("Another Regional B"), word.index("Test Signatory"))

    def test_stamp_and_signature_are_separately_opted_into(self):
        self.settings(self.f.region, signature_image=artwork("signature.png", "red").read(), stamp_image=artwork("stamp.png", "green").read())
        data = self.f.data([self.f.regional])
        for signature, stamp, count in ((False, False, 1), (True, False, 2), (False, True, 2), (True, True, 3)):
            with self.subTest(signature=signature, stamp=stamp):
                page = pdf_pages(render_pdf(data, include_signature=signature, include_stamp=stamp))[0]
                self.assertEqual(len(image_refs(page)), count)

    def test_word_reuses_closing_settings_and_only_embeds_approved_artwork(self):
        signature = artwork("signature.png", "red").read()
        stamp = artwork("stamp.png", "green").read()
        settings = self.settings(self.f.region, signature_image=signature, stamp_image=stamp, committee_enabled=True)
        ReportCommitteeMember.objects.create(settings=settings, position="Member", name="Second Member", display_order=20)
        ReportCommitteeMember.objects.create(settings=settings, position="Chair", name="First Member", contact="+268 7000 0000", display_order=10)
        data = self.f.data([self.f.regional])
        for templated in (False, True):
            if templated:
                storage = InMemoryStorage()
                template = self.f.region.report_template
                template.storage = storage
                template.save("official.docx", ContentFile(native_template("Test native letterhead")), save=False)
                data["entries"][0]["report_template"] = template
            for signed, stamped in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(templated=templated, signed=signed, stamped=stamped):
                    word = render_docx(data, include_signature=signed, include_stamp=stamped)
                    text = body_text(word)
                    self.assertLess(text.index("Regional B"), text.index("Yours Faithfully"))
                    self.assertLess(text.index("Test Signatory"), text.index("EXECUTIVE COMMITTEE"))
                    self.assertLess(text.index("First Member"), text.index("Second Member"))
                    self.assertIn("+268 7000 0000", text)
                    with ZipFile(BytesIO(word)) as archive:
                        assets = {archive.read(name) for name in archive.namelist() if name.endswith(".png")}
                        self.assertEqual(signature in assets, signed)
                        self.assertEqual(stamp in assets, stamped)

    def test_signoff_disabled_and_missing_artwork(self):
        settings = self.settings(self.f.region, signoff_enabled=False, signature_image=artwork("signature.png", "red").read(), committee_enabled=True)
        data = self.f.data([self.f.regional])
        pdf = render_pdf(data, include_signature=True, include_stamp=True)
        self.assertNotIn("Yours Faithfully", pdf_text(pdf))
        self.assertNotIn("EXECUTIVE COMMITTEE", pdf_text(pdf))
        self.assertEqual(len(image_refs(pdf_pages(pdf)[0])), 1)
        settings.signoff_enabled = True
        settings.signatory_name = ""
        settings.signature_image = None
        settings.save()
        pdf = render_pdf(data, include_signature=True, include_stamp=True)
        self.assertIn("Yours Faithfully", pdf_text(pdf))
        self.assertNotIn("Secretary", pdf_text(pdf))
        self.assertEqual(len(image_refs(pdf_pages(pdf)[0])), 1)

    def test_committee_visibility_order_and_optional_contact(self):
        settings = self.settings(self.f.region, committee_enabled=False)
        first = ReportCommitteeMember.objects.create(settings=settings, position="Chair", name="First Person", contact="+268 7654 3210", display_order=10)
        data = self.f.data([self.f.regional])
        self.assertNotIn("First Person", pdf_text(render_pdf(data)))
        settings.committee_enabled = True
        settings.save()
        text = pdf_text(render_pdf(data))
        self.assertIn("EXECUTIVE COMMITTEE", text)
        self.assertIn("Chair · First Person · +268 7654 3210", text)
        ReportCommitteeMember.objects.create(settings=settings, position="Member", name="Second Person", display_order=20)
        first.display_order = 30
        first.save()
        text = pdf_text(render_pdf(data))
        self.assertLess(text.index("Second Person"), text.index("First Person"))
        self.assertIn("Member · Second Person\n", text)

    def test_large_committee_wraps_and_paginates_without_extra_signoff(self):
        settings = self.settings(self.f.region, committee_enabled=True)
        ReportCommitteeMember.objects.bulk_create([
            ReportCommitteeMember(settings=settings, position="Committee Representative for Football Development", name=f"Member {i:02} with a long designation for a district football development and administration committee", display_order=i)
            for i in range(60)
        ])
        pages = pdf_pages(render_pdf(self.f.data([self.f.regional])))
        texts = [page.extract_text() for page in pages]
        self.assertGreater(len(pages), 2)
        self.assertEqual("\n".join(texts).count("Yours Faithfully"), 1)
        self.assertTrue(all("EXECUTIVE COMMITTEE" in text for text in texts))
        for i in range(60):
            self.assertEqual("\n".join(texts).count(f"Member {i:02}"), 1)
        self.assertIn("Member 59", texts[-1])

    def test_closing_moves_to_continuation_page_as_one_block_when_short(self):
        settings = self.settings(self.f.region, committee_enabled=True)
        ReportCommitteeMember.objects.create(settings=settings, name="Closing Member", position="Chair")
        data = self.f.data([self.f.regional])
        rows = self.rows(data)
        rows[:] = [{**rows[0], "home": f"Home {i}"} for i in range(23)]
        pages = pdf_pages(render_pdf(data))
        self.assertEqual(len(pages), 2)
        self.assertIn("Home 22", pages[0].extract_text())
        self.assertNotIn("Yours Faithfully", pages[0].extract_text())
        self.assertIn("Yours Faithfully", pages[1].extract_text())
        self.assertIn("Closing Member", pages[1].extract_text())

    def test_organisation_and_zonal_sections_do_not_share_closing_or_artwork(self):
        for owner, name in ((self.f.ewfa_org, "EWFA Signatory"), (self.f.ple_org, "PLE Signatory"),
                            (self.f.zonal.series, "Zonal Signatory"), (self.f.unbranded.series, "Other Zonal Signatory")):
            self.settings(owner, signatory_name=name)
        for competition, expected in ((self.f.ewfa, "EWFA Signatory"), (self.f.ple, "PLE Signatory")):
            text = pdf_text(render_pdf(self.f.data([competition])))
            self.assertIn(expected, text)
            self.assertEqual(text.count("Signatory"), 1)
        pages = pdf_pages(render_pdf(self.f.data([self.f.zonal, self.f.unbranded])))
        self.assertEqual(len(pages), 2)
        self.assertIn("Zonal Signatory", pages[0].extract_text())
        self.assertNotIn("Other Zonal Signatory", pages[0].extract_text())
        self.assertIn("Other Zonal Signatory", pages[1].extract_text())
        self.assertFalse(image_refs(pages[1]))

    def test_zonal_word_sections_keep_separate_signatories_and_unique_artwork_ids(self):
        data = self.f.data([self.f.zonal, self.f.unbranded])
        for entry, name, color in zip(data["entries"], ("First Zonal Signer", "Second Zonal Signer"), ("red", "green")):
            owner = entry["stationery_owner"]
            self.settings(owner, signatory_name=name, signature_image=artwork("sign.png", color).read())
            template = owner.report_template
            template.storage = InMemoryStorage()
            template.save("official.docx", ContentFile(native_template(name + " header")), save=False)
            entry["report_template"] = template
        word = render_docx(data, include_signature=True)
        text = body_text(word)
        self.assertLess(text.index("First Zonal Signer"), text.index("ZONAL WITHOUT HEADER"))
        self.assertGreater(text.index("Second Zonal Signer"), text.index("ZONAL WITHOUT HEADER"))
        document = Document(BytesIO(word))
        self.assertEqual(len(document.sections), 2)
        drawings = document.element.body.xpath(".//wp:docPr")
        self.assertEqual(len(drawings), 2)
        self.assertEqual(len({drawing.get("id") for drawing in drawings}), 2)

    def test_missing_image_and_empty_regional_export_are_safe(self):
        self.f.region.fixture_letterhead.storage.delete(self.f.region.fixture_letterhead.name)
        self.f.regional.matches.all().delete()
        self.settings(self.f.region)
        preference = RegionalReportPreference(report_kind="FIXTURES")
        data = build_regional_report_data(region=self.f.region, preference=preference, report_date=date(2026, 11, 4))
        with self.assertLogs("league_manager.regional_reports", level="WARNING"):
            pages = pdf_pages(render_pdf(data))
        self.assertEqual(len(pages), 1)
        self.assertIn("No fixtures/results", pages[0].extract_text())
        self.assertIn("Test Signatory", pages[0].extract_text())
        self.assertFalse(image_refs(pages[0]))


class OfficialDocumentManagementTests(OfficialDocumentTestCase):
    def stationery(self):
        return reverse("league_manager:regional_fixture_letterhead", args=[self.f.region.slug])

    def post_settings(self, **changes):
        return {
            "official_settings": "1", "signoff_enabled": "on", "signoff_heading": "Yours Faithfully",
            "signatory_name": "Alex Example", "signatory_position": "Secretary", "committee_enabled": "on",
            "committee-TOTAL_FORMS": "1", "committee-INITIAL_FORMS": "0", "committee-0-position": "Chair",
            "committee-0-name": "Member One", "committee-0-contact": "+268 7654 3210", "committee-0-display_order": "10",
            **changes,
        }

    def test_stationery_upload_edit_delete_members_and_remove_artwork(self):
        self.client.force_login(self.f.admin)
        self.assertContains(self.client.get(self.stationery()), "Official Document Settings")
        upload = artwork("secret-signature.png", "red")
        response = self.client.post(self.stationery(), self.post_settings(signature_upload=upload))
        self.assertEqual(response.status_code, 302)
        settings = ReportDocumentSettings.objects.get(region=self.f.region)
        self.assertTrue(settings.signature_image)
        self.assertEqual(settings.signatory_name, "Alex Example")
        self.assertNotContains(self.client.get(self.stationery()), "secret-signature.png")
        member = settings.committee_members.get()
        response = self.client.post(self.stationery(), self.post_settings(**{
            "committee-TOTAL_FORMS": "2", "committee-INITIAL_FORMS": "1", "committee-0-id": member.pk,
            "committee-0-name": "Updated Member", "committee-0-display_order": "20",
            "committee-1-position": "Deputy", "committee-1-name": "Second Member", "committee-1-display_order": "5",
            "remove_signature": "on",
        }))
        self.assertEqual(response.status_code, 302)
        settings.refresh_from_db()
        self.assertIsNone(settings.signature_image)
        members = list(settings.committee_members.all())
        self.assertEqual([member.name for member in members], ["Second Member", "Updated Member"])
        response = self.client.post(self.stationery(), self.post_settings(**{
            "committee-TOTAL_FORMS": "2", "committee-INITIAL_FORMS": "2", "committee-0-id": members[0].pk,
            "committee-0-position": "Deputy", "committee-0-name": "Second Member", "committee-0-display_order": "5",
            "committee-1-id": members[1].pk, "committee-1-DELETE": "on",
        }))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(settings.committee_members.values_list("name", flat=True)), ["Second Member"])

    def test_invalid_artwork_and_member_data_do_not_partially_save(self):
        self.client.force_login(self.f.admin)
        response = self.client.post(self.stationery(), self.post_settings(
            signature_upload=SimpleUploadedFile("fake.png", b"invalid"), **{"committee-0-name": ""},
        ))
        self.assertContains(response, "Upload a valid PNG or JPEG image.")
        self.assertContains(response, "This field is required.")
        self.assertFalse(ReportDocumentSettings.objects.filter(region=self.f.region).exists())

    def test_permissions_allow_only_own_stationery_and_approval_scope(self):
        self.client.force_login(self.f.owner)
        self.assertEqual(self.client.post(self.stationery(), self.post_settings()).status_code, 403)
        self.assertEqual(self.client.post(reverse("league_manager:zonal_fixture_letterhead", args=[self.f.unbranded.series_id]), self.post_settings()).status_code, 403)
        self.assertEqual(self.client.post(reverse("league_manager:zonal_fixture_letterhead", args=[self.f.zonal.series_id]), self.post_settings()).status_code, 302)
        OrganisationAdministrator.objects.create(user=self.f.admin, organisation="EWFA")
        self.client.force_login(self.f.admin)
        self.assertEqual(self.client.post(reverse("league_manager:organisation_fixture_letterhead", args=["ewfa"]), self.post_settings()).status_code, 302)
        self.assertEqual(self.client.post(reverse("league_manager:organisation_fixture_letterhead", args=["ple"]), self.post_settings()).status_code, 403)

    def test_preview_is_unsigned_and_does_not_save_default_preferences(self):
        self.settings(self.f.region, signature_image=artwork("signature.png", "red").read())
        self.client.force_login(self.f.admin)
        url = reverse("league_manager:regional_report_export", args=[self.f.region.slug, "fixtures"])
        preference = RegionalReportPreference.objects.create(user=self.f.admin, region=self.f.region, report_kind="FIXTURES", output_format="DOCX")
        response = self.client.post(url, {"action": "preview", "output_format": "PDF", "include_signature": "1", "selection_mode": "CUSTOM", "series": [self.f.regional.series_id]})
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(len(image_refs(pdf_pages(response.content)[0])), 1)
        self.assertIn("Regional A", pdf_text(response.content))
        preference.refresh_from_db()
        self.assertEqual(preference.output_format, "DOCX")
        self.assertEqual(preference.selection_mode, "ALL")

    def test_forged_signature_option_is_ignored_for_regional_league_owner(self):
        self.settings(self.f.region, signature_image=artwork("signature.png", "red").read())
        LeagueMembership.objects.create(user=self.f.owner, competition=self.f.regional, role="OWNER")
        self.client.force_login(self.f.owner)
        url = reverse("league_manager:league_report_export", args=[self.f.regional.slug, "fixtures"])
        self.assertNotContains(self.client.get(url), "Include approved signature")
        response = self.client.post(url, {"output_format": "PDF", "include_signature": "1"})
        self.assertEqual(len(image_refs(pdf_pages(response.content)[0])), 1)
        self.client.force_login(self.f.admin)
        url = reverse("league_manager:regional_report_export", args=[self.f.region.slug, "fixtures"])
        response = self.client.post(url, {"output_format": "PDF", "include_signature": "1"})
        self.assertEqual(len(image_refs(pdf_pages(response.content)[0])), 2)
        response = self.client.post(url, {"output_format": "PDF"})
        self.assertEqual(len(image_refs(pdf_pages(response.content)[0])), 1)


    def test_word_download_requires_scoped_approval_for_signature_and_stamp(self):
        signature = artwork("signature.png", "red").read()
        stamp = artwork("stamp.png", "green").read()
        self.settings(self.f.region, signature_image=signature, stamp_image=stamp)
        LeagueMembership.objects.create(user=self.f.owner, competition=self.f.regional, role="OWNER")
        self.client.force_login(self.f.owner)
        league_url = reverse("league_manager:league_report_export", args=[self.f.regional.slug, "fixtures"])
        self.assertNotContains(self.client.get(league_url), "Include approved signature")
        approval = {"output_format": "DOCX", "include_signature": "1", "include_stamp": "1"}
        response = self.client.post(league_url, approval)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertFalse(any("closingImage" in name for name in archive.namelist()))
        self.assertIn("Test Signatory", body_text(response.content))

        self.client.force_login(self.f.admin)
        regional_url = reverse("league_manager:regional_report_export", args=[self.f.region.slug, "fixtures"])
        self.assertContains(self.client.get(regional_url), "Include approved signature")
        response = self.client.post(regional_url, approval)
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(sum("closingImage" in name for name in archive.namelist()), 2)
        response = self.client.post(regional_url, {"output_format": "DOCX"})
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertFalse(any("closingImage" in name for name in archive.namelist()))
