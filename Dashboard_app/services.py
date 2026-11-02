"""Shared logic used by both the website views and the Telegram bot."""
import re
import zipfile
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Batch, Document, ResultUpload
from .report_parser import analyse_report, apply_to_document

MAX_UNCOMPRESSED = 500 * 1024 ** 2    # zip-bomb guard
MAX_REPORT_BYTES = 25 * 1024 ** 2


# ---------- ZIP helpers ----------

def clean_entries(zf):
    """Yield (ZipInfo, basename), skipping folders, macOS junk, hidden and ~$ files."""
    for info in zf.infolist():
        if info.is_dir() or "__MACOSX" in info.filename:
            continue
        name = Path(info.filename).name
        if not name or name.startswith((".", "~$")):
            continue
        yield info, name


def check_zip_safe(zf):
    if sum(i.file_size for i in zf.infolist()) > MAX_UNCOMPRESSED:
        raise ValueError("ZIP expands to more than the allowed size.")


def scan_zip(zip_path: Path) -> dict:
    """List the files inside the ZIP without extracting it."""
    with zipfile.ZipFile(zip_path) as zf:
        check_zip_safe(zf)
        files = [{"name": n, "size": i.file_size} for i, n in clean_entries(zf)]
    by_type = Counter(Path(f["name"]).suffix.lower().lstrip(".") or "none" for f in files)
    return {"total": len(files), "by_type": dict(by_type), "files": files}


def create_documents(batch, files):
    Document.objects.bulk_create([
        Document(
            batch=batch,
            file_name=f["name"],
            stem=Path(f["name"]).stem.lower(),
            file_format=Path(f["name"]).suffix.lower().lstrip("."),
            size_bytes=f["size"],
        ) for f in files
    ])


# ---------- Results ZIP ----------

def classify_report(name: str):
    n = name.lower()
    if "similarity" in n or "plagiarism" in n:
        return "similarity"
    if re.search(r"(?<![a-z])ai(?![a-z])", n):
        return "ai"
    return None


def match_document(report_stem: str, docs_by_len):
    """Longest stem first, with boundaries so 'document_001' never matches 'document_0010'."""
    for doc in docs_by_len:
        if re.search(rf"(?<![a-z0-9]){re.escape(doc.stem)}(?![a-z0-9])", report_stem):
            return doc
    return None


def _find_document(cover: dict, report_name: str, docs):
    """Match a report to a document. The Turnitin cover page is the most reliable:
    it lists the ORIGINAL file name, so it still works if the reviewer renames the report."""
    for candidate in (Path(cover.get("file_name") or "").stem,
                      cover.get("title") or "",
                      Path(report_name).stem):
        cand = candidate.strip().lower()
        if cand:
            doc = match_document(cand, docs)
            if doc:
                return doc
    return None


@transaction.atomic
def ingest_results_zip(batch, zip_path: Path, source: str, original_name: str,
                       uploaded_by: str = "") -> dict:
    """Read every report PDF in the ZIP (content, not just file names), attach it to the
    right document, store the file and the parsed numbers/details.
    Safe to call repeatedly: later ZIPs fill in gaps or overwrite earlier reports.
    Called by both the website view and the Telegram bot."""
    media_root = Path(settings.MEDIA_ROOT)
    out_dir = media_root / "batches" / batch.batch_id / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = sorted(batch.documents.all(), key=lambda d: len(d.stem), reverse=True)
    touched, matched, unmatched, no_score = {}, 0, [], []

    with zipfile.ZipFile(zip_path) as zf:
        check_zip_safe(zf)
        for info, name in clean_entries(zf):
            if not name.lower().endswith(".pdf") or info.file_size > MAX_REPORT_BYTES:
                unmatched.append(name)
                continue

            data = zf.read(info)
            parsed = analyse_report(data)
            print(parsed,"----------------------------")
            kind = parsed["kind"] or classify_report(name)     # content first, file name second
            doc = _find_document(parsed["cover"], name, docs) if kind else None
            if not doc:
                unmatched.append(name)
                continue

            # We choose the destination name ourselves, so zip-slip is impossible.
            dest = out_dir / f"{doc.stem}__{kind}.pdf"
            dest.write_bytes(data)
            rel = str(dest.relative_to(media_root))

            # Set the stored file FIRST: apply_to_document -> refresh_status() reads it.
            if kind == "similarity":
                doc.similarity_report = rel
            else:
                doc.ai_report = rel

            # Parsed numbers + details are merged onto the document. A report whose
            # score could not be read keeps any previously saved score (never wiped).
            if parsed["kind"] == kind:
                if not apply_to_document(doc, parsed, save=False):
                    no_score.append(name)
            else:
                no_score.append(name)    # identified by file name only, nothing to parse

            touched[doc.pk] = doc
            matched += 1

    for doc in touched.values():
        doc.refresh_status()
        doc.save()

    if matched:
        now = timezone.now()
        batch.results_received_at = now
        batch.results_source = source
        batch.save(update_fields=["results_received_at", "results_source"])
        # Sending results implies the reviewer accepted the batch.
        Batch.objects.filter(pk=batch.pk, confirmed_at__isnull=True).update(
            confirmed_at=now, confirmed_by=uploaded_by or source
        )

    ResultUpload.objects.create(
        batch=batch, source=source, uploaded_by=uploaded_by, original_name=original_name,
        zip_path=str(zip_path.relative_to(media_root)),
        matched_count=matched, unmatched=unmatched,
    )
    return {
        "matched": matched,
        "documents_updated": len(touched),
        "unmatched": unmatched,
        "no_score": no_score,        # matched and stored, but no percentage could be read
        "details": [{"file": d.file_name, "similarity": d.similarity_pct,
                     "ai": d.ai_pct, "ai_low": d.ai_below_threshold}
                    for d in touched.values()],
    }