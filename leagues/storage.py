"""Configured media storage for reusable football report stationery."""

import os
import uuid

import requests
from django.conf import settings
from django.core.files.storage import storages
from django.utils.deconstruct import deconstructible

from teams.storage_backends import SupabaseStorage


def fixture_letterhead_storage():
    return storages["fixture_letterheads"]


def report_template_storage():
    return storages["report_templates"]


@deconstructible
class SupabaseReportTemplateStorage(SupabaseStorage):
    """Persistent immutable DOCX objects without the public-image optimizer."""

    def __init__(self, folder="football/report-templates", bucket_name=None):
        super().__init__(folder=folder)
        self.bucket_name = bucket_name or os.environ.get("SUPABASE_REPORT_TEMPLATE_BUCKET") or self.bucket_name

    def _save(self, name, content):
        from .validators import validate_report_template_bytes
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("Supabase storage environment variables are missing.")
        content.seek(0)
        raw = content.read(getattr(settings, "REPORT_TEMPLATE_MAX_BYTES", 10 * 1024 * 1024) + 1)
        validate_report_template_bytes(raw)
        object_name = f"{self.folder}/v1/{uuid.uuid4().hex}.docx"
        headers = {
            **self._headers(),
            "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "Cache-Control": self.cache_control,
            "x-upsert": "false",
        }
        response = requests.post(
            f"{self.supabase_url}/storage/v1/object/{self.bucket_name}/{object_name}",
            headers=headers, data=raw, timeout=30,
        )
        if response.status_code not in (200, 201):
            response.raise_for_status()
            raise OSError(f"Report template upload failed (HTTP {response.status_code}).")
        return object_name
