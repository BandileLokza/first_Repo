# Football report stationery

Football fixtures and results can use reusable Word stationery. A saved `.docx` supplies its native page header, footer and page setup. EsportsLive replaces the document's sample body with the selected report's title, dates, venues, fixtures or results. No placeholder tags are required.

## Prepare a template

1. Open the official stationery in Microsoft Word.
2. Put its reusable artwork and association text inside **Insert > Header**. Artwork pasted only into the document body is not a repeating page header.
3. Set the intended paper size and margins. Keep enough room below the header for the report. Optional official footer content is retained; the existing EsportsLive live-scores footer is included too.
4. Embed images in the document. PNG/JPEG artwork is supported; externally linked images, SVG, macros and embedded active files are rejected. Ordinary website and email hyperlinks are supported.
5. Save as **Word Document (.docx)**. Example body text is removed automatically during generation.

The first section's nonempty default header is used on every page. If only a first-page header is populated, that header repeats. Complex first/even-page stationery variants are normalized to this repeating header.

## Upload and access

Open **League Manager**:

- Regional administrator: **Regional Administration > Region > League Owners or Export Fixtures/Results > Report stationery**. Shared stationery applies across seasons to regional leagues without a custom letterhead. Open **League Owners → League stationery** on a particular league to set its own files.
- EWFA/PLE administrator: **Section Administration > EWFA or PLE > League Owners or Export Fixtures/Results > Report stationery**. One template serves that association's leagues across seasons.
- Zonal administrator: **Section Administration > Zonal Leagues > League Owners > Report stationery** on the appropriate permanent league.
- Zonal league owner: **Manage League > League Branding > Report stationery**. The template belongs to that CompetitionSeries and carries across seasons.

Choose a file under **Word report template (.docx)**, then **Save report stationery**. Replace it by choosing another file. Tick **Clear** and save to remove it. Regional/EWFA/PLE league owners cannot change shared association stationery.

Upload a wide PNG/JPEG under **PDF letterhead image**. The artwork repeats on each fixture and result PDF page and scales to fit the printable area. An uploaded Word template supplies the stationery for Word exports. With no Word template, fixture and result Word exports use the same image as a fallback when available.

A regional league can use its own Word template, image, or both. The league image is used for PDF and replaces the shared Word header when the league has no Word template of its own. In that case, Word retains the shared template's page settings and footer. When only a league Word template is set, PDF uses the shared regional image. Clearing both files restores the shared stationery. Combined reports keep the existing league order and start a new page when the letterhead changes. The region's sign-off and committee appear once, after its last league.

## Download reports

Owners and other authorized league managers can use **Manage League > Export Fixtures / Export Results**. The new download route includes only that managed season, regardless of any submitted league IDs. Existing association-wide downloads retain their existing selection and ordering rules.

Word and PDF use the same full-width match columns, fixture dates and headings. Word tables remain editable and follow the uploaded template's page margins. Without a template, Word uses A4 with the same side margins as PDF. PDF is generated directly in Python. Combined Zonal PDFs start each league on a new page with its own letterhead and closing details. Regional, EWFA and PLE reports use one closing section after the last included league.

## Configure official report details

In **Report stationery**, enable **Include official sign-off** and enter the authorised signatory's name and designation. Upload optional PNG or JPEG signature and stamp images. When signing artwork is left out, the report provides space for a handwritten signature. Stored signing artwork is kept in the database without a public media URL.

Enable **Include executive committee** and enter each position and name. Contact numbers are optional. Lower display numbers appear first. Use **Add committee member** to add a row and **Remove** followed by **Save report stationery** to delete a member.

On the export screen, **Preview unsigned PDF** opens the current selection in the browser. For a Word or PDF download, select the relevant **Include approved signature** or **Include approved stamp** checkbox for each authorised final release. These choices apply only to that download. For a combined Zonal export, they apply to each included league with the corresponding artwork configured. Word retains the header and footer of the uploaded Word template. Configured sign-off and committee details appear after the matches in both formats.

## Install and migrate

From the project directory, using its existing virtual environment:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DEBUG = 'True'
.\venv\Scripts\python.exe manage.py migrate
```

Migration `leagues.0026_official_report_document_settings` adds optional document settings and ordered committee members. Previously uploaded Word templates and PDF letterheads are retained.

Render installs ReportLab through `requirements.txt`. No office converter, additional media bucket or PDF environment variables are needed. To run the PDF inspection tests locally, install `requirements-test.txt` instead of `requirements.txt`.

## Media storage

The `report_templates` Django storage alias uses configured local media storage for local development. With the existing Supabase credentials, it stores immutable raw DOCX files without applying the image optimizer. It reuses `SUPABASE_STORAGE_BUCKET` unless `SUPABASE_REPORT_TEMPLATE_BUCKET` selects a separate bucket. The selected bucket must permit the DOCX MIME type `application/vnd.openxmlformats-officedocument.wordprocessingml.document`.

`REPORT_TEMPLATE_MAX_BYTES` defaults to 10 MB and `REPORT_TEMPLATE_MAX_UNCOMPRESSED_BYTES` to 50 MB. The upload and read-time checks validate the DOCX package, header relationships, embedded resources, content types and active-content restrictions. Native printer settings from Word are accepted but removed from generated reports.
