"""Reusable Word stationery validation, storage and administrative boundaries."""

from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from league_manager.forms import FixtureLetterheadForm
from league_manager.models import LeagueMembership, OrganisationAdministrator, RegionalAdministrator
from leagues.models import Competition, CompetitionSeries, FootballOrganisation, Region
from leagues.storage import SupabaseReportTemplateStorage
from leagues.validators import validate_report_template, validate_report_template_bytes


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def stationery_bytes(*, header=True, extra_parts=None, extra_relations="", body_extra="", main_type=None):
    """A minimal real OOXML header package without third-party authoring tools."""
    reference = '<w:headerReference w:type="default" r:id="header1"/>' if header else ""
    header_relation = f'<Relationship Id="header1" Type="{R}/header" Target="header1.xml"/>' if header else ""
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{main_type or DOCX_MIME + ".main+xml"}"/>'
            '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
            '</Types>',
        "_rels/.rels": f'<Relationships xmlns="{P}"><Relationship Id="doc" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:p><w:r><w:t>Sample body to replace</w:t></w:r></w:p>{body_extra}<w:sectPr>{reference}</w:sectPr></w:body></w:document>',
        "word/_rels/document.xml.rels": f'<Relationships xmlns="{P}">{header_relation}{extra_relations}</Relationships>',
    }
    # The DOCX main content type uses wordprocessingml.document, not the MIME
    # string's final document suffix twice.
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        DOCX_MIME + ".main+xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    if header:
        parts["word/header1.xml"] = f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Official stationery</w:t></w:r></w:p></w:hdr>'
    parts.update(extra_parts or {})
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


def uploaded_template(name="stationery.docx", **kwargs):
    return SimpleUploadedFile(name, stationery_bytes(**kwargs), content_type=DOCX_MIME)


class ReportTemplateValidationTests(SimpleTestCase):
    def test_real_header_is_accepted_and_stream_remains_reusable(self):
        value = uploaded_template()
        validate_report_template(value)
        self.assertEqual(value.tell(), 0)
        form = FixtureLetterheadForm(files={"report_template": value})
        self.assertTrue(form.is_valid(), form.errors)

    def test_body_only_and_blank_header_are_rejected_with_instructions(self):
        for payload in (
            stationery_bytes(header=False),
            stationery_bytes(extra_parts={"word/header1.xml": f'<w:hdr xmlns:w="{W}"><w:p/></w:hdr>'}),
        ):
            with self.assertRaisesMessage(ValidationError, "Insert > Header"):
                validate_report_template_bytes(payload)

    def test_wrong_format_and_renamed_archive_are_rejected(self):
        for name, payload in (("file.docm", stationery_bytes()), ("logo.docx", b"GIF89a"), ("archive.docx", b"not a zip")):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_report_template(SimpleUploadedFile(name, payload))

    def test_macros_embedded_files_and_active_objects_are_rejected(self):
        payloads = [
            stationery_bytes(main_type="application/vnd.ms-word.document.macroEnabled.main+xml"),
            stationery_bytes(extra_parts={"word/vbaProject.bin": b"macro"}),
            stationery_bytes(extra_parts={"word/embeddings/object.docx": b"embedded"}),
            stationery_bytes(body_extra="<w:p><w:r><w:object/></w:r></w:p>"),
            stationery_bytes(extra_relations=f'<Relationship Id="embed" Type="{R}/package" Target="header1.xml"/>'),
        ]
        for payload in payloads:
            with self.subTest(), self.assertRaises(ValidationError):
                validate_report_template_bytes(payload)

    def test_external_images_hyperlinks_and_linked_fields_are_rejected(self):
        payloads = [
            stationery_bytes(extra_relations=f'<Relationship Id="external" Type="{R}/image" Target="https://example.com/image.png" TargetMode="External"/>'),
            stationery_bytes(body_extra='<w:p><w:r><w:instrText>DDEAUTO server command</w:instrText></w:r></w:p>'),
            stationery_bytes(body_extra='<w:p><w:fldSimple w:instr="INCLUDETEXT filename"/></w:p>'),
        ]
        for payload in payloads:
            with self.subTest(), self.assertRaises(ValidationError):
                validate_report_template_bytes(payload)

    def test_ordinary_internal_metadata_relationship_is_allowed(self):
        validate_report_template_bytes(stationery_bytes(
            extra_relations=f'<Relationship Id="metadata" Type="{P}/metadata/core-properties" Target="../docProps/core.xml"/>',
            extra_parts={"docProps/core.xml": '<core/>'},
        ))

    def test_static_website_and_email_hyperlinks_are_allowed(self):
        for target in ("https://www.example.com", "http://www.example.com", "mailto:office@example.com"):
            validate_report_template_bytes(stationery_bytes(extra_relations=f'<Relationship Id="website" Type="{R}/hyperlink" Target="{target}" TargetMode="External"/>'))
        for target in ("file:///c:/private.docx", "javascript:alert(1)", "https://"):
            with self.assertRaises(ValidationError):
                validate_report_template_bytes(stationery_bytes(extra_relations=f'<Relationship Id="unsafe" Type="{R}/hyperlink" Target="{target}" TargetMode="External"/>'))

    def test_later_section_header_does_not_hide_a_blank_initial_header(self):
        xml = f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body><w:p><w:pPr><w:sectPr/></w:pPr></w:p><w:sectPr><w:headerReference w:type="default" r:id="header1"/></w:sectPr></w:body></w:document>'
        with self.assertRaisesMessage(ValidationError, "Insert > Header"):
            validate_report_template_bytes(stationery_bytes(extra_parts={"word/document.xml": xml}))

    def test_xml_entities_and_package_path_traversal_are_rejected(self):
        for extra in ({"../escape.xml": "<safe/>"}, {"word/extra.xml": '<!DOCTYPE x [<!ENTITY x "unsafe">]><x>&x;</x>'}):
            with self.assertRaises(ValidationError):
                validate_report_template_bytes(stationery_bytes(extra_parts=extra))

    def test_declared_xml_and_mixed_case_parts_receive_security_checks(self):
        for name, content_type in (
            ("word/styles.dat", "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"),
            ("word/extra.dat", "application/xml"),
            ("word/renamed-vml.dat", "application/vnd.openxmlformats-officedocument.vmlDrawing"),
            ("word/extra.xMl", "application/octet-stream"),
        ):
            xml = f'<w:styles xmlns:w="{W}"><w:instrText>DDEAUTO server command</w:instrText></w:styles>'
            payload = stationery_bytes(extra_parts={name: xml})
            with ZipFile(BytesIO(payload)) as archive:
                parts = {part: archive.read(part) for part in archive.namelist()}
            parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(b"</Types>", f'<Override PartName="/{name}" ContentType="{content_type}"/></Types>'.encode())
            output = BytesIO()
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                for part, value in parts.items():
                    archive.writestr(part, value)
            with self.subTest(name=name), self.assertRaisesMessage(ValidationError, "linked fields"):
                validate_report_template_bytes(output.getvalue())

    def test_svg_and_external_vml_artwork_are_rejected(self):
        with self.assertRaisesMessage(ValidationError, "PNG or JPEG"):
            validate_report_template_bytes(stationery_bytes(extra_parts={"word/media/header.svg": '<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'}))
        for value in ('file:///c:/linked.png', 'https://example.com/linked.png', '../linked.png'):
            vml = f'<xml xmlns:v="urn:schemas-microsoft-com:vml"><v:imagedata src="{value}"/></xml>'
            with self.assertRaises(ValidationError):
                validate_report_template_bytes(stationery_bytes(extra_parts={"word/headerDrawing.vml": vml}))

    def test_vml_and_drawing_image_ids_require_an_internal_image_relationship(self):
        vml = f'<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:r="{R}"><v:imagedata r:id="image1"/></xml>'
        with self.assertRaisesMessage(ValidationError, "missing image"):
            validate_report_template_bytes(stationery_bytes(extra_parts={"word/headerDrawing.vml": vml}))
        parts = {
            "word/headerDrawing.vml": vml,
            "word/_rels/headerDrawing.vml.rels": f'<Relationships xmlns="{P}"><Relationship Id="image1" Type="{R}/image" Target="media/header.png"/></Relationships>',
            "word/media/header.png": b"embedded-image",
        }
        validate_report_template_bytes(stationery_bytes(extra_parts=parts))

    def test_corrupt_compressed_xml_is_a_form_validation_error(self):
        payload = bytearray(stationery_bytes())
        with ZipFile(BytesIO(payload)) as archive:
            info = archive.getinfo("word/document.xml")
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        payload[offset:offset + 8] = b"\xff" * 8
        with self.assertRaises(ValidationError):
            validate_report_template_bytes(payload)

    def test_only_recognized_native_printer_settings_binary_is_allowed(self):
        path = "word/printerSettings/printerSettings1.bin"
        native_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.printerSettings"
        for mime, relation_type, accepted in (
            (native_mime, f"{R}/printerSettings", True),
            ("application/octet-stream", f"{R}/printerSettings", False),
            (native_mime, f"{R}/image", False),
        ):
            relation = f'<Relationship Id="printer" Type="{relation_type}" Target="printerSettings/printerSettings1.bin"/>'
            payload = stationery_bytes(extra_parts={path: b"native printer DEVMODE settings"}, extra_relations=relation)
            with ZipFile(BytesIO(payload)) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(b"</Types>", f'<Override PartName="/{path}" ContentType="{mime}"/></Types>'.encode())
            parts["word/document.xml"] = parts["word/document.xml"].replace(b"</w:sectPr>", b'<w:printerSettings r:id="printer"/></w:sectPr>')
            output = BytesIO()
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                for name, value in parts.items():
                    archive.writestr(name, value)
            with self.subTest(mime=mime, relation_type=relation_type):
                if accepted:
                    validate_report_template_bytes(output.getvalue())
                else:
                    with self.assertRaises(ValidationError):
                        validate_report_template_bytes(output.getvalue())

    def test_compressed_and_expanded_limits_and_compression_bombs_are_rejected(self):
        with override_settings(REPORT_TEMPLATE_MAX_BYTES=10), self.assertRaises(ValidationError):
            validate_report_template_bytes(stationery_bytes())
        with override_settings(REPORT_TEMPLATE_MAX_UNCOMPRESSED_BYTES=10), self.assertRaises(ValidationError):
            validate_report_template_bytes(stationery_bytes())
        with self.assertRaisesMessage(ValidationError, "excessively compressed"):
            validate_report_template_bytes(stationery_bytes(extra_parts={"word/bomb.txt": "x" * (2 * 1024 * 1024)}))

    def test_template_bucket_override_is_optional_and_explicit_value_wins(self):
        with patch.dict("os.environ", {"SUPABASE_STORAGE_BUCKET": "existing-images", "SUPABASE_REPORT_TEMPLATE_BUCKET": ""}):
            self.assertEqual(SupabaseReportTemplateStorage().bucket_name, "existing-images")
        with patch.dict("os.environ", {"SUPABASE_REPORT_TEMPLATE_BUCKET": "template-documents"}):
            self.assertEqual(SupabaseReportTemplateStorage().bucket_name, "template-documents")
            self.assertEqual(SupabaseReportTemplateStorage(bucket_name="explicit-documents").bucket_name, "explicit-documents")

    def test_raw_supabase_storage_uploads_docx_without_image_optimization(self):
        with patch.dict("os.environ", {"SUPABASE_REPORT_TEMPLATE_BUCKET": "report-documents"}):
            storage = SupabaseReportTemplateStorage()
        storage.supabase_url = "https://storage.example.test"
        storage.supabase_key = "test-key"
        raw = stationery_bytes()
        response = Mock()
        response.status_code = 201
        response.raise_for_status.return_value = None
        with patch("leagues.storage.requests.post", return_value=response) as post, patch(
            "teams.storage_backends.ImageOptimizationService.optimize", side_effect=AssertionError("DOCX must remain raw")
        ):
            result = storage._save("stationery.docx", SimpleUploadedFile("stationery.docx", raw))
        self.assertTrue(result.startswith("football/report-templates/v1/"))
        self.assertTrue(result.endswith(".docx"))
        self.assertIn("/storage/v1/object/report-documents/", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["data"], raw)
        self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"], DOCX_MIME)
        self.assertEqual(post.call_args.kwargs["headers"]["x-upsert"], "false")


class ReportTemplateManagementTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for model in (Region, FootballOrganisation, CompetitionSeries):
            field = model._meta.get_field("report_template")
            original = field.storage
            field.storage = FileSystemStorage(location=self.directory.name)
            self.addCleanup(setattr, field, "storage", original)
        User = get_user_model()
        self.region_admin = User.objects.create_user("template-region-admin")
        self.ewfa_admin = User.objects.create_user("template-ewfa-admin")
        self.zonal_admin = User.objects.create_user("template-zonal-admin")
        self.owner = User.objects.create_user("template-owner")
        self.agent = User.objects.create_user("template-agent")
        self.region = Region.objects.create(name="Template Manzini", slug="template-manzini")
        self.other_region = Region.objects.create(name="Template Hhohho", slug="template-hhohho")
        RegionalAdministrator.objects.create(user=self.region_admin, region=self.region)
        OrganisationAdministrator.objects.create(user=self.ewfa_admin, organisation="EWFA")
        OrganisationAdministrator.objects.create(user=self.zonal_admin, organisation="ZONAL")
        self.zonal = Competition.objects.create(name="Template Zonal", slug="template-zonal", season="2026")
        self.other_zonal = Competition.objects.create(name="Other Template Zonal", slug="other-template-zonal", season="2026")
        self.regional = Competition.objects.create(name="Template Regional", slug="template-regional", season="2026")
        self.regional.series.league_type = "REGIONAL"
        self.regional.series.region = self.region
        self.regional.series.save(update_fields=["league_type", "region"])
        LeagueMembership.objects.create(user=self.owner, competition=self.zonal, role="OWNER")
        LeagueMembership.objects.create(user=self.owner, competition=self.regional, role="OWNER")
        LeagueMembership.objects.create(user=self.agent, competition=self.zonal, role="ADMIN")
        self.ewfa, _ = FootballOrganisation.objects.get_or_create(code="EWFA", defaults={"name": "EWFA"})
        self.ple, _ = FootballOrganisation.objects.get_or_create(code="PLE", defaults={"name": "PLE"})

    def region_url(self, region=None):
        return reverse("league_manager:regional_fixture_letterhead", args=[(region or self.region).slug])

    def zonal_url(self, competition=None):
        return reverse("league_manager:zonal_fixture_letterhead", args=[(competition or self.zonal).series_id])

    def test_region_template_upload_replace_remove_and_season_reuse(self):
        self.client.force_login(self.region_admin)
        for name in ("first.docx", "replacement.docx"):
            self.assertEqual(self.client.post(self.region_url(), {"report_template": uploaded_template(name)}).status_code, 302)
            self.region.refresh_from_db()
            self.assertTrue(self.region.report_template.name.endswith(name))
        self.assertEqual(self.client.post(self.region_url(), {"report_template-clear": "on"}).status_code, 302)
        self.region.refresh_from_db()
        self.assertFalse(self.region.report_template)

    def test_cross_region_and_organisation_uploads_are_denied(self):
        self.client.force_login(self.region_admin)
        for url in (self.region_url(self.other_region), reverse("league_manager:organisation_fixture_letterhead", args=["ewfa"]), self.zonal_url()):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"report_template": uploaded_template()}).status_code, 403)
        self.other_region.refresh_from_db()
        self.assertFalse(self.other_region.report_template)

    def test_organisation_admin_can_only_manage_own_shared_template(self):
        self.client.force_login(self.ewfa_admin)
        self.assertEqual(self.client.post(reverse("league_manager:organisation_fixture_letterhead", args=["ewfa"]), {"report_template": uploaded_template()}).status_code, 302)
        self.assertEqual(self.client.post(reverse("league_manager:organisation_fixture_letterhead", args=["ple"]), {"report_template": uploaded_template()}).status_code, 403)
        self.ewfa.refresh_from_db()
        self.ple.refresh_from_db()
        self.assertTrue(self.ewfa.report_template)
        self.assertFalse(self.ple.report_template)

    def test_zonal_owner_can_only_manage_own_series_across_seasons(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.zonal_url(), {"report_template": uploaded_template()}).status_code, 302)
        self.assertEqual(self.client.post(self.zonal_url(self.other_zonal), {"report_template": uploaded_template()}).status_code, 403)
        self.assertEqual(self.client.post(self.region_url(), {"report_template": uploaded_template()}).status_code, 403)
        self.zonal.series.refresh_from_db()
        next_season = Competition.objects.create(name="Template Zonal 2027", slug="template-zonal-2027", season="2027", series=self.zonal.series)
        self.assertEqual(next_season.series.report_template.name, self.zonal.series.report_template.name)

    def test_league_admin_cannot_replace_owner_template_and_zonal_admin_can(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(self.zonal_url(), {"report_template": uploaded_template()}).status_code, 403)
        self.client.force_login(self.zonal_admin)
        self.assertEqual(self.client.post(self.zonal_url(self.other_zonal), {"report_template": uploaded_template()}).status_code, 302)

    def test_storage_failures_show_safe_form_error_and_keep_existing_preview(self):
        from requests.exceptions import RequestException
        self.region.report_template.save("existing-template.docx", uploaded_template())
        before = self.region.report_template.name
        self.client.force_login(self.region_admin)
        storage = Region._meta.get_field("report_template").storage
        for failure in (RequestException("internal-secret-details"), OSError("internal-secret-details"), ValueError("internal-secret-details")):
            with self.subTest(error=type(failure).__name__):
                with patch.object(storage, "save", side_effect=failure):
                    response = self.client.post(self.region_url(), {"report_template": uploaded_template("replacement.docx")})
                self.assertContains(response, "The report stationery could not be saved.")
                self.assertNotContains(response, "internal-secret-details")
                self.assertNotContains(response, "Report stationery updated.")
                self.region.refresh_from_db()
                self.assertEqual(self.region.report_template.name, before)
                self.assertEqual(response.context["letterhead_owner"].report_template.name, before)
                self.assertContains(response, before)
                self.assertTrue(response.context["letterhead_form"].non_field_errors())

    def test_invalid_upload_keeps_existing_template_and_displays_actionable_error(self):
        self.client.force_login(self.region_admin)
        self.client.post(self.region_url(), {"report_template": uploaded_template()})
        self.region.refresh_from_db()
        before = self.region.report_template.name
        response = self.client.post(self.region_url(), {"report_template": uploaded_template(header=False)})
        self.assertContains(response, "Insert &gt; Header")
        self.region.refresh_from_db()
        self.assertEqual(self.region.report_template.name, before)

    def test_cross_region_staff_admin_cannot_forge_new_template_field(self):
        self.region_admin.is_staff = True
        self.region_admin.save(update_fields=["is_staff"])
        self.region_admin.user_permissions.add(Permission.objects.get(codename="change_region", content_type__app_label="leagues"))
        self.client.force_login(self.region_admin)
        response = self.client.post(reverse("admin:leagues_region_change", args=[self.other_region.pk]), {
            "name": self.other_region.name, "slug": self.other_region.slug, "display_order": 100,
            "is_active": "on", "_save": "Save", "report_template": uploaded_template(),
        })
        self.assertEqual(response.status_code, 302)
        self.other_region.refresh_from_db()
        self.assertFalse(self.other_region.report_template)

    def test_results_export_links_to_stationery_and_legacy_upload_url_still_works(self):
        self.client.force_login(self.region_admin)
        self.assertContains(self.client.get(reverse("league_manager:regional_report_export", args=[self.region.slug, "results"])), self.region_url())
        self.assertContains(self.client.get(self.region_url()), "Word report template (.docx)")
        self.client.force_login(self.ewfa_admin)
        url = reverse("league_manager:organisation_fixture_letterhead", args=["ewfa"])
        self.assertContains(self.client.get(reverse("league_manager:organisation_report_export", args=["ewfa", "results"])), url)

    def test_owner_download_is_limited_to_own_league_even_with_forged_selection(self):
        from league_manager.regional_reports import build_scope_report_data
        self.client.force_login(self.owner)
        url = reverse("league_manager:league_report_export", args=[self.zonal.slug, "fixtures"])
        with patch("league_manager.views.build_scope_report_data", wraps=build_scope_report_data) as build, patch("league_manager.views.render_docx", return_value=b"test-docx") as render:
            response = self.client.post(url, {"output_format": "DOCX", "selection_mode": "ALL", "series": [self.other_zonal.series_id]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], DOCX_MIME)
        self.assertEqual([league.pk for league in build.call_args.kwargs["competitions"]], [self.zonal.pk])
        self.assertIn("scope_template", build.call_args.kwargs)
        self.assertIn("report_template", render.call_args.args[0])
        for kind in ("fixtures", "results"):
            denied = reverse("league_manager:league_report_export", args=[self.other_zonal.slug, kind])
            self.assertEqual(self.client.post(denied, {"output_format": "DOCX"}).status_code, 403)
        unknown = reverse("league_manager:league_report_export", args=[self.zonal.slug, "standings"])
        self.assertEqual(self.client.get(unknown).status_code, 403)

    def test_empty_own_league_exports_keep_the_native_header(self):
        self.zonal.series.report_template.save("native-header.docx", uploaded_template())
        self.client.force_login(self.owner)
        for kind in ("fixtures", "results"):
            url = reverse("league_manager:league_report_export", args=[self.zonal.slug, kind])
            response = self.client.post(url, {"output_format": "DOCX"})
            self.assertEqual(response.status_code, 200)
            with ZipFile(BytesIO(response.content)) as archive:
                header_parts = [name for name in archive.namelist() if "header" in name.lower() and name.endswith(".xml")]
                self.assertTrue(any(b"Official stationery" in archive.read(name) for name in header_parts))
                body = archive.read("word/document.xml")
                self.assertIn(b"No applicable", body)
                self.assertNotIn(b"Sample body to replace", body)

    def test_own_league_export_navigation_default_format_and_invalid_format(self):
        self.client.force_login(self.owner)
        fixture_url = reverse("league_manager:league_report_export", args=[self.zonal.slug, "fixtures"])
        results_url = reverse("league_manager:league_report_export", args=[self.zonal.slug, "results"])
        dashboard = self.client.get(reverse("league_manager:league_dashboard", args=[self.zonal.slug]))
        self.assertContains(dashboard, fixture_url)
        self.assertContains(dashboard, results_url)
        response = self.client.get(fixture_url)
        self.assertEqual(response.context["preference"].output_format, "DOCX")
        self.assertContains(self.client.post(fixture_url, {"output_format": "EXECUTABLE"}), "Select a valid output format.")

    def test_incomplete_results_require_confirmation_for_own_league_download(self):
        from datetime import date
        self.client.force_login(self.owner)
        url = reverse("league_manager:league_report_export", args=[self.zonal.slug, "results"])
        data = {"warnings": ["Only one result is final."], "entries": [], "abbreviation": "OWN", "kind": "RESULTS", "report_date": date(2026, 11, 5)}
        with patch("league_manager.views.build_scope_report_data", return_value=data), patch("league_manager.views.render_docx", return_value=b"test-docx") as render:
            response = self.client.post(url, {"output_format": "DOCX"})
            self.assertContains(response, "Confirm and Download")
            render.assert_not_called()
            response = self.client.post(url, {"output_format": "DOCX", "confirmed": "1"})
            self.assertEqual(response.status_code, 200)
            render.assert_called_once()

    def test_converter_failure_shows_actionable_message_for_all_export_scopes(self):
        from league_manager.regional_reports import ReportExportError
        targets = [
            (self.region_admin, reverse("league_manager:regional_report_export", args=[self.region.slug, "fixtures"])),
            (self.ewfa_admin, reverse("league_manager:organisation_report_export", args=["ewfa", "fixtures"])),
            (self.owner, reverse("league_manager:league_report_export", args=[self.zonal.slug, "fixtures"])),
        ]
        for user, url in targets:
            self.client.force_login(user)
            with self.subTest(url=url), patch("league_manager.views.render_pdf", side_effect=ReportExportError("PDF conversion is unavailable. Download Word instead.")):
                response = self.client.post(url, {"selection_mode": "ALL", "output_format": "PDF"}, follow=True)
                self.assertContains(response, "PDF conversion is unavailable. Download Word instead.")
