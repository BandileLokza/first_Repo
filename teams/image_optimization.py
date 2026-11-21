"""Centralized, safe image normalization for public media uploads.

The storage backend calls this module for every configured public image.  It is
deliberately independent of Django models so uploads and the existing-media
command use precisely the same rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import warnings

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000


class ImageUploadError(ValidationError):
    """Raised when content is not a safe raster image upload."""


@dataclass(frozen=True)
class ImageProfile:
    """The maximum output bounds for a category of public media."""

    name: str
    max_width: int
    max_height: int


IMAGE_PROFILES = {
    "logo": ImageProfile("logo", 512, 512),
    "sponsor": ImageProfile("sponsor", 768, 768),
    "photo": ImageProfile("photo", 800, 800),
    "general": ImageProfile("general", 1600, 1600),
}


@dataclass
class OptimizedImage:
    """Normalized image data and the measurements used by the migration."""

    content: ContentFile
    source_size: int
    source_dimensions: tuple[int, int]
    output_dimensions: tuple[int, int]
    output_format: str
    has_transparency: bool
    profile: str

    @property
    def output_size(self) -> int:
        return self.content.size

    @property
    def bytes_saved(self) -> int:
        return self.source_size - self.output_size

    @property
    def percent_saved(self) -> float:
        if not self.source_size:
            return 0.0
        return self.bytes_saved / self.source_size * 100


class ImageOptimizationService:
    """Validate, orient, resize and encode public raster images.

    Transparent assets are encoded as PNG so alpha is preserved.  Opaque
    assets are encoded as progressive JPEGs at a conservative visual quality.
    Rebuilding the image rather than copying it strips EXIF and ancillary
    metadata.  Animated uploads are rejected instead of silently flattening
    an animation to its first frame.
    """

    allowed_formats = frozenset({"JPEG", "PNG", "WEBP", "GIF"})
    migration_jpeg_qualities = (86, 82, 78)

    def __init__(
        self,
        *,
        max_upload_bytes: int | None = None,
        max_image_pixels: int | None = None,
    ):
        self.max_upload_bytes = (
            max_upload_bytes
            if max_upload_bytes is not None
            else getattr(settings, "IMAGE_UPLOAD_MAX_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        )
        self.max_image_pixels = (
            max_image_pixels
            if max_image_pixels is not None
            else getattr(settings, "IMAGE_UPLOAD_MAX_PIXELS", DEFAULT_MAX_IMAGE_PIXELS)
        )

    def optimize(self, upload, profile: str | ImageProfile) -> OptimizedImage:
        """Return a validated, normalized image without changing the source."""
        selected_profile = self._get_profile(profile)
        raw_bytes = self._read_upload(upload)
        return self.optimize_bytes(raw_bytes, self._upload_name(upload), selected_profile)

    def optimize_bytes(
        self,
        raw_bytes: bytes,
        filename: str,
        profile: str | ImageProfile,
        *,
        enforce_upload_limit: bool = True,
        jpeg_quality: int = 86,
    ) -> OptimizedImage:
        """Normalize raw image bytes for uploads and migration downloads."""
        selected_profile = self._get_profile(profile)
        source_size = len(raw_bytes)
        if not raw_bytes:
            raise ImageUploadError("The uploaded image is empty.")
        if enforce_upload_limit and source_size > self.max_upload_bytes:
            raise ImageUploadError(
                f"Image uploads must be {self.max_upload_bytes // (1024 * 1024)} MB or smaller."
            )

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw_bytes)) as verified:
                    verified.verify()
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw_bytes)) as opened:
                    source_format = opened.format
                    if source_format not in self.allowed_formats:
                        raise ImageUploadError(
                            "Upload a JPEG, PNG, WebP, or non-animated GIF image."
                        )
                    if getattr(opened, "is_animated", False):
                        raise ImageUploadError("Animated images are not supported for public media.")
                    source_dimensions = opened.size
                    if source_dimensions[0] * source_dimensions[1] > self.max_image_pixels:
                        raise ImageUploadError("The image has too many pixels to process safely.")
                    image = ImageOps.exif_transpose(opened)
                    image.load()
        except ImageUploadError:
            raise
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ImageUploadError("Upload a valid, safe image file.") from error

        has_transparency = self._has_transparency(image)
        image.thumbnail(
            (selected_profile.max_width, selected_profile.max_height),
            Image.Resampling.LANCZOS,
        )
        output_format = "PNG" if has_transparency else "JPEG"
        output = BytesIO()
        if output_format == "PNG":
            if image.mode not in {"RGBA", "LA"}:
                image = image.convert("RGBA")
            image.save(output, format="PNG", optimize=True, compress_level=9)
            extension = ".png"
            content_type = "image/png"
        else:
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.save(
                output,
                format="JPEG",
                quality=jpeg_quality,
                optimize=True,
                progressive=True,
            )
            extension = ".jpg"
            content_type = "image/jpeg"

        normalized_name = f"{Path(filename or 'image').stem or 'image'}{extension}"
        content = ContentFile(output.getvalue(), name=normalized_name)
        # Supabase needs an explicit media type; ContentFile does not provide
        # one, while UploadedFile does.
        content.content_type = content_type
        result = OptimizedImage(
            content=content,
            source_size=source_size,
            source_dimensions=source_dimensions,
            output_dimensions=image.size,
            output_format=output_format,
            has_transparency=has_transparency,
            profile=selected_profile.name,
        )
        # The storage backend uses this internal marker when the migration has
        # already normalized bytes.  It prevents a second lossy JPEG encode on
        # the same write while never trusting user-provided uploads.
        content._esportslive_optimized_image = result
        return result

    def optimize_for_transfer(
        self,
        raw_bytes: bytes,
        filename: str,
        profile: str | ImageProfile,
        *,
        enforce_upload_limit: bool = False,
    ) -> OptimizedImage:
        """Return the smallest visually-safe candidate for a media migration.

        Future uploads retain the standard quality-86 setting.  Existing
        already-compressed JPEGs occasionally become larger at that quality,
        so the migration tries quality 82 and 78 before deciding whether a
        byte-saving replacement is available.
        """
        primary = self.optimize_bytes(
            raw_bytes,
            filename,
            profile,
            enforce_upload_limit=enforce_upload_limit,
            jpeg_quality=self.migration_jpeg_qualities[0],
        )
        if primary.output_format != "JPEG" or primary.output_size < primary.source_size:
            return primary

        candidates = [primary]
        for quality in self.migration_jpeg_qualities[1:]:
            candidates.append(
                self.optimize_bytes(
                    raw_bytes,
                    filename,
                    profile,
                    enforce_upload_limit=enforce_upload_limit,
                    jpeg_quality=quality,
                )
            )
        return min(candidates, key=lambda candidate: candidate.output_size)

    def inspect(self, raw_bytes: bytes, filename: str = "image") -> tuple[tuple[int, int], int]:
        """Validate an existing object and return dimensions and byte count."""
        result = self.optimize_bytes(
            raw_bytes,
            filename,
            IMAGE_PROFILES["general"],
            enforce_upload_limit=False,
        )
        return result.source_dimensions, result.source_size

    def _read_upload(self, upload) -> bytes:
        declared_size = getattr(upload, "size", None)
        if declared_size is not None and declared_size > self.max_upload_bytes:
            raise ImageUploadError(
                f"Image uploads must be {self.max_upload_bytes // (1024 * 1024)} MB or smaller."
            )
        try:
            upload.seek(0)
        except (AttributeError, OSError):
            pass
        raw_bytes = upload.read()
        try:
            upload.seek(0)
        except (AttributeError, OSError):
            pass
        if len(raw_bytes) > self.max_upload_bytes:
            raise ImageUploadError(
                f"Image uploads must be {self.max_upload_bytes // (1024 * 1024)} MB or smaller."
            )
        return raw_bytes

    @staticmethod
    def _upload_name(upload) -> str:
        return getattr(upload, "name", "image") or "image"

    @staticmethod
    def _has_transparency(image: Image.Image) -> bool:
        return "A" in image.getbands() or image.info.get("transparency") is not None

    @staticmethod
    def _get_profile(profile: str | ImageProfile) -> ImageProfile:
        if isinstance(profile, ImageProfile):
            return profile
        try:
            return IMAGE_PROFILES[profile]
        except KeyError as error:
            raise ValueError(f"Unknown image optimization profile: {profile}") from error


def is_optimized_media_name(name: str) -> bool:
    """Whether an object was emitted by this versioned normalization pipeline."""
    return "/v1/" in f"/{name.lstrip('/')}"
