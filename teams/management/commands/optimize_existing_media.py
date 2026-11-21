"""Safely migrate existing Supabase-backed public images to normalized media."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import ImageField

from teams.image_optimization import (
    DEFAULT_MAX_UPLOAD_BYTES,
    IMAGE_PROFILES,
    ImageOptimizationService,
    ImageUploadError,
    is_optimized_media_name,
)
from teams.storage_backends import SupabaseStorage


@dataclass(frozen=True)
class ImageReference:
    model: type
    field_name: str
    primary_key: object
    storage: SupabaseStorage

    @property
    def label(self) -> str:
        return f"{self.model._meta.label}.{self.field_name}#{self.primary_key}"


@dataclass
class MediaObject:
    name: str
    storage: SupabaseStorage
    profile: str
    references: list[ImageReference] = field(default_factory=list)
    profile_conflict: str = ""


@dataclass
class Summary:
    scanned: int = 0
    changed: int = 0
    skipped: int = 0
    errors: int = 0
    original_total: int = 0
    optimized_total: int = 0


class Command(BaseCommand):
    help = (
        "Normalize existing Supabase public images into new versioned object keys. "
        "Use --dry-run before any production write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report planned changes without uploading, updating database references, or deleting objects.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        service = ImageOptimizationService()
        maximum_existing_size = getattr(
            settings,
            "EXISTING_MEDIA_MAX_SOURCE_BYTES",
            max(DEFAULT_MAX_UPLOAD_BYTES, 25 * 1024 * 1024),
        )
        summary = Summary()

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: no remote or database writes will occur."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "LIVE MODE: replacement objects receive new versioned names; originals are retained until upload and verification succeed."
                )
            )

        for media_object in self._media_objects():
            summary.scanned += 1
            original_size = None
            try:
                if media_object.profile_conflict:
                    raise RuntimeError(media_object.profile_conflict)
                raw_bytes = self._read_object(media_object)
                original_size = len(raw_bytes)
                summary.original_total += original_size
                if original_size > maximum_existing_size:
                    raise ImageUploadError(
                        f"Existing object exceeds the {maximum_existing_size // (1024 * 1024)} MB migration safety limit."
                    )

                result = service.optimize_for_transfer(
                    raw_bytes,
                    media_object.name,
                    media_object.profile,
                )

                if is_optimized_media_name(media_object.name):
                    summary.skipped += 1
                    summary.optimized_total += original_size
                    self._report(
                        "SKIP",
                        media_object,
                        result.source_dimensions,
                        original_size,
                        result.source_dimensions,
                        original_size,
                        "already emitted by the v1 image pipeline",
                    )
                    continue

                requires_dimension_safety_resize = self._requires_dimension_safety_resize(
                    result.source_dimensions,
                    media_object.profile,
                )
                should_replace = (
                    result.output_size < original_size
                    or requires_dimension_safety_resize
                )
                if not should_replace:
                    summary.skipped += 1
                    summary.optimized_total += original_size
                    if result.output_size > original_size:
                        reason = "optimized candidate would increase bytes"
                    else:
                        reason = "optimized candidate would not reduce bytes"
                    self._report(
                        "SKIP",
                        media_object,
                        result.source_dimensions,
                        original_size,
                        result.output_dimensions,
                        result.output_size,
                        reason,
                    )
                    continue

                if dry_run:
                    summary.changed += 1
                    summary.optimized_total += result.output_size
                    self._report(
                        "CHANGE",
                        media_object,
                        result.source_dimensions,
                        original_size,
                        result.output_dimensions,
                        result.output_size,
                        self._change_detail(
                            result,
                            original_size,
                            requires_dimension_safety_resize,
                        ),
                    )
                    continue

                replacement_name = self._write_verified_replacement(media_object, result)
                try:
                    self._update_references(media_object.references, replacement_name)
                except Exception:
                    # The original is still live because no reference was
                    # changed.  Best-effort cleanup avoids an unreferenced
                    # replacement consuming storage after a DB failure.
                    try:
                        media_object.storage.delete(replacement_name)
                    except Exception:
                        pass
                    raise
                try:
                    media_object.storage.delete(media_object.name)
                except Exception as error:  # The reference now points safely to the verified replacement.
                    self.stderr.write(
                        self.style.WARNING(
                            f"WARN {media_object.name}: replacement is active but legacy object could not be deleted: {error}"
                        )
                    )
                summary.changed += 1
                summary.optimized_total += result.output_size
                self._report(
                    "CHANGED",
                    media_object,
                    result.source_dimensions,
                    original_size,
                    result.output_dimensions,
                    result.output_size,
                    f"{self._change_detail(result, original_size, requires_dimension_safety_resize)}; "
                    f"replaced with {replacement_name}",
                )
            except Exception as error:
                summary.errors += 1
                # A readable source remains part of the projected total even
                # when it cannot be transformed.  Unreadable sources have no
                # measured size and are excluded from both totals.
                if original_size is not None:
                    summary.optimized_total += original_size
                self.stderr.write(
                    self.style.ERROR(f"ERROR {media_object.name}: {error}")
                )

        self._print_summary(summary)

    def _media_objects(self):
        """Yield each distinct Supabase object referenced by installed models."""
        grouped = {}
        for model in apps.get_models():
            for field in model._meta.local_fields:
                if not isinstance(field, ImageField) or not isinstance(
                    field.storage, SupabaseStorage
                ):
                    continue
                queryset = (
                    model._default_manager.exclude(**{f"{field.name}__isnull": True})
                    .exclude(**{field.name: ""})
                    .values_list("pk", field.name)
                )
                for primary_key, name in queryset.iterator():
                    if not name:
                        continue
                    key = (*field.storage.storage_identity, name)
                    reference = ImageReference(model, field.name, primary_key, field.storage)
                    current = grouped.get(key)
                    if current is None:
                        grouped[key] = MediaObject(
                            name=name,
                            storage=field.storage,
                            profile=field.storage.image_profile,
                            references=[reference],
                        )
                    elif current.profile == field.storage.image_profile:
                        current.references.append(reference)
                    else:
                        # Sharing one source object between profiles is rare,
                        # but choosing an arbitrary bound could silently
                        # reduce quality for another live reference.
                        current.profile_conflict = (
                            f"Object is referenced by incompatible profiles: "
                            f"{current.profile} and {field.storage.image_profile}."
                        )
                        current.references.append(reference)
        for key in sorted(grouped, key=lambda item: (str(item[0]), str(item[1]), item[2])):
            yield grouped[key]

    @staticmethod
    def _read_object(media_object: MediaObject) -> bytes:
        source = media_object.storage.open(media_object.name, "rb")
        try:
            return source.read()
        finally:
            source.close()

    def _write_verified_replacement(self, media_object: MediaObject, result) -> str:
        result.content.seek(0)
        replacement_name = media_object.storage.save(media_object.name, result.content)
        if replacement_name == media_object.name:
            raise RuntimeError(
                "Storage returned the original object name; refusing an in-place replacement because immutable caching is enabled."
            )
        replacement = media_object.storage.open(replacement_name, "rb")
        try:
            replacement_bytes = replacement.read()
        finally:
            replacement.close()
        result.content.seek(0)
        expected_bytes = result.content.read()
        if (
            len(replacement_bytes) != len(expected_bytes)
            or sha256(replacement_bytes).digest() != sha256(expected_bytes).digest()
        ):
            try:
                media_object.storage.delete(replacement_name)
            except Exception:
                pass
            raise RuntimeError("Uploaded replacement failed content verification; original was not changed.")
        return replacement_name

    @staticmethod
    def _update_references(references: list[ImageReference], replacement_name: str):
        with transaction.atomic():
            for reference in references:
                updated = reference.model._default_manager.filter(
                    pk=reference.primary_key
                ).update(**{reference.field_name: replacement_name})
                if updated != 1:
                    raise RuntimeError(f"Could not update database reference {reference.label}.")

    @staticmethod
    def _requires_dimension_safety_resize(
        source_dimensions: tuple[int, int], profile_name: str
    ) -> bool:
        profile = IMAGE_PROFILES[profile_name]
        return (
            source_dimensions[0] > profile.max_width
            or source_dimensions[1] > profile.max_height
        )

    @staticmethod
    def _change_detail(result, original_size: int, safety_resize: bool) -> str:
        if safety_resize and result.output_size >= original_size:
            return "required downscale to enforce configured image bounds despite non-positive byte savings"
        return "would upload a verified replacement under a new object key"

    def _report(
        self,
        state: str,
        media_object: MediaObject,
        source_dimensions: tuple[int, int],
        source_size: int,
        output_dimensions: tuple[int, int],
        output_size: int,
        detail: str,
    ):
        saved = source_size - output_size
        percent = saved / source_size * 100 if source_size else 0
        references = ", ".join(reference.label for reference in media_object.references)
        self.stdout.write(
            f"{state} {media_object.name} [{references}] | "
            f"{source_dimensions[0]}x{source_dimensions[1]}, {source_size} B -> "
            f"{output_dimensions[0]}x{output_dimensions[1]}, {output_size} B | "
            f"saved {saved} B ({percent:.1f}%) | {detail}"
        )

    def _print_summary(self, summary: Summary):
        saved = summary.original_total - summary.optimized_total
        percent = saved / summary.original_total * 100 if summary.original_total else 0
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "Summary: "
                f"scanned={summary.scanned}, changed={summary.changed}, "
                f"skipped={summary.skipped}, errors={summary.errors}, "
                f"original_total={summary.original_total} B, "
                f"optimized_total={summary.optimized_total} B, "
                f"estimated_savings={saved} B ({percent:.1f}%)"
            )
        )
