from io import BytesIO, StringIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from PIL import Image

from leagues.models import Competition
from teams.image_optimization import ImageOptimizationService, ImageUploadError
from teams.models import Team
from teams.storage_backends import SupabaseStorage


def image_bytes(size=(1200, 800), mode="RGB", image_format="JPEG", **save_kwargs):
    image = Image.new(mode, size, (40, 100, 180, 120) if "A" in mode else (40, 100, 180))
    data = BytesIO()
    image.save(data, format=image_format, **save_kwargs)
    return data.getvalue()


def noisy_jpeg_bytes(size, quality):
    image = Image.effect_noise(size, 100).convert("RGB")
    data = BytesIO()
    image.save(data, format="JPEG", quality=quality, optimize=True)
    return data.getvalue()


class ImageOptimizationServiceTests(SimpleTestCase):
    def setUp(self):
        self.service = ImageOptimizationService()

    def test_logo_is_downscaled_without_changing_aspect_ratio(self):
        result = self.service.optimize(
            SimpleUploadedFile("crest.jpg", image_bytes((2000, 1000)), "image/jpeg"),
            "logo",
        )

        self.assertEqual(result.source_dimensions, (2000, 1000))
        self.assertEqual(result.output_dimensions, (512, 256))
        self.assertEqual(result.output_format, "JPEG")

    def test_transparent_logo_stays_transparent_png(self):
        result = self.service.optimize(
            SimpleUploadedFile(
                "crest.png",
                image_bytes((1000, 500), mode="RGBA", image_format="PNG"),
                "image/png",
            ),
            "logo",
        )

        self.assertEqual(result.output_format, "PNG")
        self.assertTrue(result.has_transparency)
        with Image.open(result.content) as output:
            self.assertEqual(output.size, (512, 256))
            self.assertIn("A", output.getbands())

    def test_exif_orientation_is_applied_before_resizing(self):
        source = Image.new("RGB", (100, 200), (20, 40, 60))
        exif = Image.Exif()
        exif[274] = 6  # Rotate 90 degrees clockwise.
        data = BytesIO()
        source.save(data, format="JPEG", exif=exif)

        result = self.service.optimize(
            SimpleUploadedFile("portrait.jpg", data.getvalue(), "image/jpeg"),
            "photo",
        )

        self.assertEqual(result.source_dimensions, (100, 200))
        self.assertEqual(result.output_dimensions, (200, 100))

    def test_small_image_is_not_upscaled(self):
        result = self.service.optimize(
            SimpleUploadedFile("small.jpg", image_bytes((120, 80)), "image/jpeg"),
            "general",
        )

        self.assertEqual(result.output_dimensions, (120, 80))

    def test_transfer_optimization_tries_more_efficient_jpeg_encoding(self):
        raw_bytes = noisy_jpeg_bytes((400, 300), quality=10)
        standard = self.service.optimize_bytes(raw_bytes, "small.jpg", "logo")
        transfer_candidate = self.service.optimize_for_transfer(
            raw_bytes, "small.jpg", "logo"
        )

        self.assertLess(transfer_candidate.output_size, standard.output_size)
        self.assertGreater(transfer_candidate.output_size, len(raw_bytes))

    def test_invalid_content_is_rejected(self):
        with self.assertRaises(ImageUploadError):
            self.service.optimize(
                SimpleUploadedFile("not-an-image.jpg", b"not an image", "image/jpeg"),
                "logo",
            )

    def test_upload_size_limit_is_enforced_before_processing(self):
        service = ImageOptimizationService(max_upload_bytes=50)
        with self.assertRaises(ImageUploadError):
            service.optimize(
                SimpleUploadedFile("large.jpg", image_bytes(), "image/jpeg"),
                "logo",
            )


class _Response:
    status_code = 201
    text = ""


class SupabaseStorageUploadTests(SimpleTestCase):
    @patch("teams.storage_backends.requests.post", return_value=_Response())
    def test_upload_uses_versioned_key_and_immutable_cache_header(self, post):
        storage = SupabaseStorage(folder="teams", image_profile="logo")
        storage.supabase_url = "https://project.supabase.co"
        storage.supabase_key = "secret"
        name = storage._save(
            "teams/source.jpg",
            SimpleUploadedFile("source.jpg", image_bytes((900, 900)), "image/jpeg"),
        )

        self.assertRegex(name, r"^teams/v1/[0-9a-f]{32}\.jpg$")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Cache-Control"],
            "public, max-age=31536000, immutable",
        )
        self.assertEqual(post.call_args.kwargs["headers"]["x-upsert"], "false")

    @patch("teams.storage_backends.requests.post")
    def test_invalid_upload_never_reaches_supabase(self, post):
        storage = SupabaseStorage(folder="teams", image_profile="logo")
        storage.supabase_url = "https://project.supabase.co"
        storage.supabase_key = "secret"

        with self.assertRaises(ImageUploadError):
            storage._save("teams/bad.jpg", ContentFile(b"bad", name="bad.jpg"))
        post.assert_not_called()


class MemorySupabaseStorage(SupabaseStorage):
    """Supabase-shaped storage used to verify command safety without HTTP."""

    def __init__(self):
        super().__init__(folder="teams", image_profile="logo")
        self.supabase_url = "memory://supabase"
        self.supabase_key = "test"
        self.objects = {}
        self.write_count = 0
        self.fail_writes = False
        self.events = []

    def _save(self, name, content):
        self.write_count += 1
        self.events.append("save")
        if self.fail_writes:
            raise OSError("simulated upload failure")
        result = getattr(content, "_esportslive_optimized_image", None)
        extension = ".png" if result and result.output_format == "PNG" else ".jpg"
        new_name = f"teams/v1/replacement-{self.write_count}{extension}"
        content.seek(0)
        self.objects[new_name] = content.read()
        return new_name

    def _open(self, name, mode="rb"):
        if name not in self.objects:
            raise FileNotFoundError(name)
        self.events.append(f"open:{name}")
        return ContentFile(self.objects[name], name=name)

    def exists(self, name):
        return name in self.objects

    def delete(self, name):
        self.events.append(f"delete:{name}")
        self.objects.pop(name, None)


class OptimizeExistingMediaCommandTests(TestCase):
    def setUp(self):
        self.field = Team._meta.get_field("logo")
        self.original_storage = self.field.storage
        self.storage = MemorySupabaseStorage()
        self.field.storage = self.storage
        self.competition = Competition.objects.create(
            name="Image Test League", slug="image-test-league", season="2026"
        )
        self.legacy_name = "teams/legacy-logo.jpg"
        self.team = Team.objects.create(
            name="Image Team", competition=self.competition, logo=self.legacy_name
        )
        self.storage.objects[self.legacy_name] = image_bytes((1800, 900))

    def tearDown(self):
        self.field.storage = self.original_storage
        super().tearDown()

    def command(self, *arguments):
        output = StringIO()
        call_command("optimize_existing_media", *arguments, stdout=output, stderr=output)
        return output.getvalue()

    def test_dry_run_reports_change_without_writes_or_reference_change(self):
        output = self.command("--dry-run")

        self.team.refresh_from_db()
        self.assertEqual(self.team.logo.name, self.legacy_name)
        self.assertEqual(self.storage.write_count, 0)
        self.assertIn("CHANGE teams/legacy-logo.jpg", output)
        change_line = next(line for line in output.splitlines() if line.startswith("CHANGE "))
        self.assertNotIn("saved -", change_line)
        self.assertIn("scanned=1, changed=1, skipped=0, errors=0", output)

    def test_real_run_updates_reference_only_after_verified_upload_and_is_idempotent(self):
        output = self.command()

        self.team.refresh_from_db()
        replacement_name = self.team.logo.name
        self.assertTrue(replacement_name.startswith("teams/v1/"))
        self.assertIn(replacement_name, self.storage.objects)
        self.assertNotIn(self.legacy_name, self.storage.objects)
        self.assertLess(
            self.storage.events.index(f"open:{replacement_name}"),
            self.storage.events.index(f"delete:{self.legacy_name}"),
        )
        self.assertIn("changed=1", output)

        second_run = self.command()
        self.assertEqual(self.storage.write_count, 1)
        self.assertIn("skipped=1", second_run)
        self.assertIn("errors=0", second_run)

    def test_upload_failure_keeps_original_reference_and_continues_to_summary(self):
        self.storage.fail_writes = True
        output = self.command()

        self.team.refresh_from_db()
        self.assertEqual(self.team.logo.name, self.legacy_name)
        self.assertIn(self.legacy_name, self.storage.objects)
        self.assertIn("errors=1", output)
        self.assertIn("simulated upload failure", output)

    def test_small_already_compressed_image_is_skipped_when_candidate_grows(self):
        self.storage.objects[self.legacy_name] = noisy_jpeg_bytes((400, 300), quality=10)

        dry_run = self.command("--dry-run")
        self.assertIn("SKIP teams/legacy-logo.jpg", dry_run)
        self.assertIn("optimized candidate would increase bytes", dry_run)
        self.assertIn("scanned=1, changed=0, skipped=1, errors=0", dry_run)
        self.assertNotIn("CHANGE teams/legacy-logo.jpg", dry_run)
        self.assertFalse(
            any(
                line.startswith("CHANGE ") and "saved -" in line
                for line in dry_run.splitlines()
            )
        )

        live_run = self.command()
        self.team.refresh_from_db()
        self.assertEqual(self.team.logo.name, self.legacy_name)
        self.assertEqual(self.storage.write_count, 0)
        self.assertIn("optimized candidate would increase bytes", live_run)

    def test_oversized_image_can_replace_when_downscale_is_required(self):
        self.storage.objects[self.legacy_name] = noisy_jpeg_bytes((600, 600), quality=10)

        output = self.command("--dry-run")

        self.assertIn("CHANGE teams/legacy-logo.jpg", output)
        self.assertIn("saved -", output)
        self.assertIn("required downscale to enforce configured image bounds", output)
        self.assertIn("scanned=1, changed=1, skipped=0, errors=0", output)
