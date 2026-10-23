"""Shared logic used by both the website views and the Telegram bot."""
import re
import zipfile
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Batch, Document, ResultUpload

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


def _pdf_text(pdf_path: Path, pages: int = 3) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        return " ".join((p.extract_text() or "") for p in reader.pages[:pages]).lower()
    except Exception:
        return ""


def extract_similarity(pdf_path: Path):
    """Turnitin similarity report, page 2: '25% Overall Similarity'. None if not found."""
    m = re.search(r"(\d{1,3})\s*%\s*overall\s+similarity", _pdf_text(pdf_path))
    if m and 0 <= int(m.group(1)) <= 100:
        return int(m.group(1))
    return None


def extract_ai(pdf_path: Path):
    """Turnitin AI writing report, page 2: '37% detected as AI', or '*% detected as AI'
    when the score is under Turnitin's 20% threshold (the number is deliberately hidden).
    Returns (pct, below_threshold). The '20%' in the explanatory text is NOT a score."""
    t = _pdf_text(pdf_path)
    m = re.search(r"(\d{1,3})\s*%\s*detected\s+as\s+ai", t)
    if m and 0 <= int(m.group(1)) <= 100:
        return int(m.group(1)), False
    if re.search(r"\*\s*%\s*detected\s+as\s+ai", t):
        return None, True
    return None, False


@transaction.atomic
def ingest_results_zip(batch, zip_path: Path, source: str, original_name: str,
                       uploaded_by: str = "") -> dict:
    """Match report PDFs in the ZIP to the batch's documents, store them, update DB.
    Safe to call repeatedly: later ZIPs fill in gaps or overwrite earlier reports.
    Called by both the website view and the Telegram bot."""
    media_root = Path(settings.MEDIA_ROOT)
    out_dir = media_root / "batches" / batch.batch_id / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = sorted(batch.documents.all(), key=lambda d: len(d.stem), reverse=True)
    touched, matched, unmatched = {}, 0, []

    with zipfile.ZipFile(zip_path) as zf:
        check_zip_safe(zf)
        for info, name in clean_entries(zf):
            if not name.lower().endswith(".pdf") or info.file_size > MAX_REPORT_BYTES:
                unmatched.append(name)
                continue
            kind = classify_report(name)
            doc = match_document(Path(name).stem.lower(), docs) if kind else None
            if not doc:
                unmatched.append(name)
                continue

            # We choose the destination name ourselves, so zip-slip is impossible.
            dest = out_dir / f"{doc.stem}__{kind}.pdf"
            dest.write_bytes(zf.read(info))
            rel = str(dest.relative_to(media_root))

            if kind == "similarity":
                doc.similarity_report = rel
                doc.similarity_pct = extract_similarity(dest)
            else:
                doc.ai_report = rel
                doc.ai_pct, doc.ai_below_threshold = extract_ai(dest)
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
    return {"matched": matched, "documents_updated": len(touched), "unmatched": unmatched}