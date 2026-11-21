# teams/storage_backends.py
import os
import uuid
from urllib.parse import quote

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

from teams.image_optimization import ImageOptimizationService


@deconstructible
class SupabaseStorage(Storage):
    """Public Supabase media storage with immutable, versioned object names."""

    cache_control = "public, max-age=31536000, immutable"

    def __init__(self, folder="teams", image_profile="logo"):
        self.supabase_url = os.environ.get("SUPABASE_URL")
        self.supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        self.bucket_name = os.environ.get("SUPABASE_STORAGE_BUCKET", "team-logos")
        self.folder = folder
        self.image_profile = image_profile

    @property
    def storage_identity(self):
        """Stable identity used by the media migration to group shared objects."""
        return (self.supabase_url, self.bucket_name)

    def get_available_name(self, name, max_length=None):
        # _save deliberately replaces the requested name with a UUID path, so
        # consulting exists(name) would be an unnecessary remote request.
        return name

    def _save(self, name, content):
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("Supabase storage environment variables are missing.")

        optimized = getattr(content, "_esportslive_optimized_image", None)
        if not optimized or optimized.profile != self.image_profile:
            optimized = ImageOptimizationService().optimize(content, self.image_profile)
        original_name = optimized.content.name
        ext = os.path.splitext(original_name)[1]
        # v1 identifies assets which have passed through the centralized
        # pipeline.  It also lets the migration safely recognize idempotent
        # runs without relying on lossy re-encoding heuristics.
        unique_name = f"{self.folder}/v1/{uuid.uuid4().hex}{ext}"
        optimized.content.seek(0)
        file_bytes = optimized.content.read()

        upload_url = f"{self.supabase_url}/storage/v1/object/{self.bucket_name}/{unique_name}"

        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": getattr(optimized.content, "content_type", "application/octet-stream"),
            "Cache-Control": self.cache_control,
            "x-upsert": "false",
        }

        response = requests.post(upload_url, headers=headers, data=file_bytes, timeout=30)

        if response.status_code not in (200, 201):
            raise Exception(f"Supabase upload failed: {response.status_code} - {response.text}")

        return unique_name

    def url(self, name):
        if not name:
            return ""
        object_name = quote(name, safe="/")
        return f"{self.supabase_url}/storage/v1/object/public/{self.bucket_name}/{object_name}"

    def exists(self, name):
        if not self.supabase_url or not self.supabase_key or not name:
            return False
        response = requests.head(
            self._object_info_url(name), headers=self._headers(), timeout=30
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def _open(self, name, mode="rb"):
        if "r" not in mode:
            raise ValueError("SupabaseStorage only supports reading through open().")
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("Supabase storage environment variables are missing.")
        response = requests.get(self._object_url(name), headers=self._headers(), timeout=60)
        if response.status_code == 404:
            raise FileNotFoundError(name)
        response.raise_for_status()
        return ContentFile(response.content, name=name)

    def delete(self, name):
        if not self.supabase_url or not self.supabase_key or not name:
            return

        response = requests.delete(
            self._object_url(name), headers=self._headers(), timeout=30
        )
        if response.status_code not in (200, 204):
            response.raise_for_status()

    def size(self, name):
        if not self.supabase_url or not self.supabase_key or not name:
            raise FileNotFoundError(name)
        response = requests.get(self._object_url(name), headers=self._headers(), timeout=60)
        if response.status_code == 404:
            raise FileNotFoundError(name)
        response.raise_for_status()
        return len(response.content)

    def _object_url(self, name):
        object_name = quote(name, safe="/")
        return f"{self.supabase_url}/storage/v1/object/{self.bucket_name}/{object_name}"

    def _object_info_url(self, name):
        object_name = quote(name, safe="/")
        return f"{self.supabase_url}/storage/v1/object/info/{self.bucket_name}/{object_name}"

    def _headers(self):
        return {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
        }
