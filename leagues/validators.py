"""Validation shared by fixture stationery upload forms and Django admin."""

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError


def validate_fixture_letterhead(value):
    if not value:
        return
    # Existing storage names need no re-download during unrelated model edits.
    if getattr(value, "_committed", False):
        return
    if value.size > getattr(settings, "IMAGE_UPLOAD_MAX_BYTES", 10 * 1024 * 1024):
        raise ValidationError("Fixture letterheads must be no larger than 10 MB.")
    try:
        value.seek(0)
        with Image.open(value) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValidationError("Upload a PNG or JPEG fixture letterhead.")
            if image.width * image.height > getattr(settings, "IMAGE_UPLOAD_MAX_PIXELS", 40_000_000):
                raise ValidationError("The fixture letterhead image is too large.")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValidationError("Upload a valid PNG or JPEG fixture letterhead.") from error
    finally:
        value.seek(0)



def _safe_template_hyperlink(target):
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    if any(character in target for character in "\r\n\x00"):
        return False
    return bool(
        (parsed.scheme.lower() in {"http", "https"} and parsed.netloc)
        or (parsed.scheme.lower() == "mailto" and parsed.path)
    )


def validate_report_template_bytes(raw):
    """Accept bounded, self-contained macro-free Word stationery packages."""
    from io import BytesIO
    from pathlib import PurePosixPath
    import posixpath
    import re
    import zlib
    from xml.etree import ElementTree as ET
    from zipfile import BadZipFile, ZipFile, ZIP_STORED, ZIP_DEFLATED

    compressed_limit = getattr(settings, "REPORT_TEMPLATE_MAX_BYTES", 10 * 1024 * 1024)
    expanded_limit = getattr(settings, "REPORT_TEMPLATE_MAX_UNCOMPRESSED_BYTES", 50 * 1024 * 1024)
    if len(raw) > compressed_limit:
        raise ValidationError("The Word report template exceeds the allowed upload size.")
    word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    office = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    try:
        with ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(members) > 512 or len(set(names)) != len(names):
                raise ValidationError("The Word template contains too many or duplicate package parts.")
            if sum(member.file_size for member in members) > expanded_limit:
                raise ValidationError("The expanded Word template exceeds the allowed size.")
            printer_parts = set()
            for member in members:
                path = PurePosixPath(member.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                        or member.flag_bits & 1 or member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValidationError("The Word template contains an unsafe package part.")
                if member.file_size > expanded_limit or (
                    member.file_size > 1024 * 1024 and member.file_size > max(member.compress_size, 1) * 200
                ):
                    raise ValidationError("The Word template contains excessively compressed content.")
                lower = member.filename.lower()
                if lower.endswith(".svg"):
                    raise ValidationError("Use PNG or JPEG artwork in the Word template instead of SVG.")
                if any(part in lower for part in ("vbaproject", "activex/", "embeddings/")):
                    raise ValidationError("Use a macro-free DOCX without embedded files or active content.")
                if lower.endswith(".bin"):
                    if not re.fullmatch(r"word/printerSettings/printerSettings[0-9]+\.bin", member.filename):
                        raise ValidationError("Use a macro-free DOCX without embedded files or active content.")
                    printer_parts.add(member.filename)
            required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels"}
            if not required.issubset(names):
                raise ValidationError("Upload a valid Word .docx document, not a renamed archive.")
            def checked_xml(value):
                # Content declarations must be checked before deciding which
                # package parts are XML, including UTF-16 declarations.
                check = value.replace(b"\x00", b"").upper()
                if b"<!DOCTYPE" in check or b"<!ENTITY" in check:
                    raise ValidationError("The Word template contains unsupported XML declarations.")
                return ET.fromstring(value)

            content = checked_xml(archive.read("[Content_Types].xml"))
            if content.tag != f"{{{content_ns}}}Types":
                raise ValidationError("Upload a valid Word .docx document.")
            content_overrides = {node.get("PartName", "").lstrip("/"): node.get("ContentType", "") for node in content if node.tag == f"{{{content_ns}}}Override"}
            content_defaults = {node.get("Extension", "").lower(): node.get("ContentType", "") for node in content if node.tag == f"{{{content_ns}}}Default"}

            def declared_type(name):
                return content_overrides.get(name, content_defaults.get(name.rsplit(".", 1)[-1].lower(), "")).split(";", 1)[0].strip().lower()

            xml_parts = {}
            forbidden_tags = {"object", "OLEObject", "control", "altChunk", "attachedTemplate", "subDoc", "mailMerge", "dataSource", "script"}
            for name in names:
                content_type = declared_type(name)
                if content_type == "image/svg+xml":
                    raise ValidationError("Use PNG or JPEG artwork in the Word template instead of SVG.")
                if name.lower().endswith((".xml", ".rels", ".vml")) or content_type in {"application/xml", "text/xml", "application/vnd.openxmlformats-officedocument.vmldrawing"} or content_type.endswith("+xml"):
                    tree = content if name == "[Content_Types].xml" else checked_xml(archive.read(name))
                    for node in tree.iter():
                        local = node.tag.rsplit("}", 1)[-1]
                        if local in forbidden_tags:
                            raise ValidationError("Use a DOCX without embedded files, active content or linked templates.")
                        if local in {"instrText", "fldSimple"}:
                            instruction = (node.text or "") + " " + node.get(f"{{{word}}}instr", "")
                            if re.search(r"\b(DDEAUTO|DDE|INCLUDETEXT|INCLUDEPICTURE|DATABASE|LINK)\b", instruction, re.I):
                                raise ValidationError("Remove linked fields and external links from the Word template.")
                        for key, value in node.attrib.items():
                            attribute = key.rsplit("}", 1)[-1].lower()
                            if attribute.startswith("on") or (attribute in {"href", "src"} and value and not value.startswith("#")):
                                raise ValidationError("The Word template must use embedded artwork, not directly linked images or active content.")
                    instructions = "".join(node.text or "" for node in tree.iter(f"{{{word}}}instrText"))
                    if re.search(r"\b(DDEAUTO|DDE|INCLUDETEXT|INCLUDEPICTURE|DATABASE|LINK)\b", instructions, re.I):
                        raise ValidationError("Remove linked fields from the Word template.")
                    for node in tree.iter(f"{{{word}}}fldSimple"):
                        instructions += " " + node.get(f"{{{word}}}instr", "")
                    for hyperlink in re.finditer(r'HYPERLINK\s+(?:"([^"]*)"|(\S+))', instructions, re.I):
                        target = hyperlink.group(1) or hyperlink.group(2)
                        if target != "\\l" and not _safe_template_hyperlink(target):
                            raise ValidationError("Word template hyperlinks may use only http, https or mailto addresses.")
                    xml_parts[name] = tree
            content = xml_parts["[Content_Types].xml"]
            if content.tag != f"{{{content_ns}}}Types":
                raise ValidationError("Upload a valid Word .docx document.")
            main_types = [node.get("ContentType") for node in content if node.get("PartName") == "/word/document.xml"]
            if main_types != ["application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"]:
                raise ValidationError("Use a macro-free Word .docx document.")
            for node in content:
                content_type = node.get("ContentType", "").lower()
                if content_type == "image/svg+xml":
                    raise ValidationError("Use PNG or JPEG artwork in the Word template instead of SVG.")
                if any(value in content_type for value in ("macroenabled", "vbaproject", "oleobject", "activex")):
                    raise ValidationError("Use a macro-free DOCX without embedded files or active content.")
            for part in printer_parts:
                mime = content_overrides.get(part, content_defaults.get("bin"))
                if mime != "application/vnd.openxmlformats-officedocument.wordprocessingml.printerSettings":
                    raise ValidationError("The Word template contains an unrecognized binary part.")
            relationships = {}
            for name, tree in xml_parts.items():
                if not (
                    name.lower().endswith(".rels")
                    or declared_type(name) == "application/vnd.openxmlformats-package.relationships+xml"
                    or tree.tag == f"{{{package}}}Relationships"
                ):
                    continue
                if tree.tag != f"{{{package}}}Relationships":
                    raise ValidationError("The Word template contains invalid relationships.")
                source_dir = "" if name == "_rels/.rels" else posixpath.dirname(posixpath.dirname(name))
                ids = set()
                relationships[name] = {}
                for relation in tree:
                    target = relation.get("Target", "")
                    relation_type = relation.get("Type", "")
                    relation_id = relation.get("Id", "")
                    if not relation_id or relation_id in ids:
                        raise ValidationError("The Word template contains invalid relationship identifiers.")
                    ids.add(relation_id)
                    if relation.get("TargetMode", "").lower() == "external":
                        if relation_type == f"{office}/hyperlink" and _safe_template_hyperlink(target):
                            relationships[name][relation_id] = (relation_type, target)
                            continue
                        raise ValidationError("Use embedded images and remove linked templates. External hyperlinks must use http, https or mailto.")
                    if ":" in target or "\\" in target or target.startswith("//"):
                        raise ValidationError("The Word template contains an invalid related part.")
                    if relation_type.rsplit("/", 1)[-1].lower() in {"oleobject", "package", "control", "attachedtemplate", "afchunk", "vbaproject"}:
                        raise ValidationError("The Word template contains unsupported active or embedded content.")
                    resolved = posixpath.normpath(posixpath.join(source_dir, target)).lstrip("/")
                    if resolved.startswith("../") or resolved not in names:
                        raise ValidationError("The Word template contains a missing or invalid related part.")
                    relationships[name][relation_id] = (relation_type, resolved)
            referenced_printers = set()
            for relations in relationships.values():
                for kind, resolved in relations.values():
                    if resolved in printer_parts:
                        if kind != f"{office}/printerSettings":
                            raise ValidationError("The Word template contains an invalid printer-settings relationship.")
                        referenced_printers.add(resolved)
                    elif kind == f"{office}/printerSettings":
                        raise ValidationError("The Word template contains an unrecognized printer-settings part.")
            if referenced_printers != printer_parts:
                raise ValidationError("The Word template contains an unrecognized binary part.")
            for part_name, tree in xml_parts.items():
                if part_name.lower().endswith(".rels") or tree.tag == f"{{{package}}}Relationships":
                    continue
                related_name = posixpath.join(posixpath.dirname(part_name), "_rels", posixpath.basename(part_name) + ".rels")
                related = relationships.get(related_name, {})
                for node in tree.iter():
                    local = node.tag.rsplit("}", 1)[-1]
                    for attribute in ("id", "embed", "link"):
                        relation_id = node.get(f"{{{office}}}{attribute}")
                        if relation_id is None:
                            continue
                        if relation_id not in related:
                            raise ValidationError("The Word template contains a missing image or document relationship.")
                        kind, resolved = related[relation_id]
                        if local in {"imagedata", "blip"} or attribute in {"embed", "link"}:
                            if kind != f"{office}/image" or resolved not in names:
                                raise ValidationError("The Word template must use embedded artwork, not linked images.")
            root_relationships = relationships["_rels/.rels"]
            if not any(kind == f"{office}/officeDocument" and target == "word/document.xml" for kind, target in root_relationships.values()):
                raise ValidationError("Upload a valid Word .docx document.")
            document = xml_parts["word/document.xml"]
            if document.tag != f"{{{word}}}document" or document.find(f"{{{word}}}body") is None:
                raise ValidationError("Upload a valid Word .docx document.")
            doc_relationships = relationships["word/_rels/document.xml.rels"]
            has_header = False
            first_section = next(document.iter(f"{{{word}}}sectPr"), None)
            references = first_section.findall(f"{{{word}}}headerReference") if first_section is not None else []
            for reference in references:
                kind, target = doc_relationships.get(reference.get(f"{{{office}}}id"), (None, None))
                header = xml_parts.get(target)
                if kind != f"{office}/header" or header is None or header.tag != f"{{{word}}}hdr":
                    raise ValidationError("The Word template contains an invalid page header.")
                if any((node.tag == f"{{{word}}}t" and (node.text or "").strip()) or node.tag in {f"{{{word}}}drawing", f"{{{word}}}pict"} for node in header.iter()):
                    has_header = True
            if not has_header:
                raise ValidationError("Put your letterhead inside Word's page Header (Insert > Header), then save as .docx. Artwork placed only in the document body cannot repeat on report pages.")
    except ValidationError:
        raise
    except (BadZipFile, ET.ParseError, KeyError, OSError, ValueError, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        raise ValidationError("Upload a valid, macro-free Word .docx report template.") from error


def validate_report_template(value):
    if not value or getattr(value, "_committed", False):
        return
    if not str(getattr(value, "name", "")).lower().endswith(".docx"):
        raise ValidationError("Upload a Word .docx report template.")
    limit = getattr(settings, "REPORT_TEMPLATE_MAX_BYTES", 10 * 1024 * 1024)
    try:
        value.seek(0)
        validate_report_template_bytes(value.read(limit + 1))
    finally:
        value.seek(0)
