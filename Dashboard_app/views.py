import hmac
import json
import shutil
import tempfile
import uuid
import zipfile
from collections import Counter
from functools import wraps
from pathlib import Path
from zipfile import ZipFile

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_protect

from . import telegram_bot as tg
from .models import Batch, Document
from .services import create_documents, ingest_results_zip, scan_zip
from .notifications import email_results_received

MAX_ZIP_SIZE = 50 * 1024 ** 2   # 50 MB
NAME_PREFIXES = {"ADI", "AMR", "ARO", "SHA", "SAB"}


def api_login_required(view):
    """Optional protection: set DOCS_REQUIRE_LOGIN = True in settings.py to require a
    logged-in user. Returns JSON so fetch() calls get a usable error."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if getattr(settings, "DOCS_REQUIRE_LOGIN", False) and not request.user.is_authenticated:
            return JsonResponse({"error": "Please log in."}, status=401)
        return view(request, *args, **kwargs)
    return wrapper


def DashboardView(request):
    return render(request, "document-management.html")


def _save_upload(upload, dest: Path):
    with open(dest, "wb") as f:
        for chunk in upload.chunks():
            f.write(chunk)


def _zip_error_message(exc):
    return "Invalid or corrupted ZIP file." if isinstance(exc, zipfile.BadZipFile) else str(exc)


def _filtered_docs(request, batch):
    """Single source of truth for table filtering.
    Used by both the dashboard table and the reports ZIP download."""
    docs = batch.documents.all()

    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    name = request.GET.get("name", "").upper()

    if q:
        docs = docs.filter(file_name__icontains=q)
    if status in Document.Status.values:
        docs = docs.filter(status=status)
    if name in NAME_PREFIXES:
        docs = docs.filter(file_name__istartswith=name)
    return docs


# ---------- 1. Coordinator uploads documents for scanning ----------

@require_POST
@api_login_required
def upload_for_scanning(request):
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "No file uploaded."}, status=400)
    if not upload.name.lower().endswith(".zip"):
        return JsonResponse({"error": "Please upload a .zip file."}, status=400)
    if upload.size > MAX_ZIP_SIZE:
        return JsonResponse({"error": "ZIP exceeds the 50 MB limit."}, status=400)

    batch_id = uuid.uuid4().hex[:12]
    media_root = Path(settings.MEDIA_ROOT)
    batch_dir = media_root / "batches" / batch_id
    batch_dir.mkdir(parents=True)

    zip_path = batch_dir / "upload.zip"
    _save_upload(upload, zip_path)

    try:
        result = scan_zip(zip_path)
    except (zipfile.BadZipFile, ValueError) as e:
        shutil.rmtree(batch_dir, ignore_errors=True)
        return JsonResponse({"error": _zip_error_message(e)}, status=400)

    if result["total"] == 0:
        shutil.rmtree(batch_dir, ignore_errors=True)
        return JsonResponse({"error": "The ZIP contains no files."}, status=400)

    manifest = {
        "batch_id": batch_id,
        "original_name": upload.name,
        "size_bytes": upload.size,
        **result,
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Batch + its Document rows are created together, or not at all.
    with transaction.atomic():
        batch = Batch.objects.create(
            batch_id=batch_id,
            original_name=upload.name,
            zip_path=str(zip_path.relative_to(media_root)),
            total_files=result["total"],
            by_type=result["by_type"],
        )
        create_documents(batch, result["files"])

    # Telegram sends it on the next tick (cron / run_telegram_bot).
    return JsonResponse({
        "batch_id": batch_id,
        "original_name": upload.name,
        "total": result["total"],
        "by_type": result["by_type"],
        "telegram": "queued",
    })


# ---------- 2. Reviewer's results ZIP (website channel) ----------

def _batch_or_latest(batch_id=None):
    qs = Batch.objects.all()
    return qs.filter(batch_id=batch_id).first() if batch_id else qs.order_by("-uploaded_at").first()


@require_POST
@api_login_required
def upload_results(request):
    upload = request.FILES.get("file")
    batch = _batch_or_latest(request.POST.get("batch_id"))
    if not batch:
        return JsonResponse({"error": "No active batch. Upload documents for scanning first."},
                            status=400)
    if not upload or not upload.name.lower().endswith(".zip"):
        return JsonResponse({"error": "Please upload a .zip file."}, status=400)
    if upload.size > MAX_ZIP_SIZE:
        return JsonResponse({"error": "ZIP exceeds the 50 MB limit."}, status=400)

    res_dir = Path(settings.MEDIA_ROOT) / "batches" / batch.batch_id / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    zip_path = res_dir / f"{timezone.now():%Y%m%d%H%M%S}.zip"
    _save_upload(upload, zip_path)

    user = request.user
    uploaded_by = user.get_username() if user.is_authenticated else "web"
    print(f"Results ZIP uploaded by {uploaded_by}: {upload.name} ({upload.size} bytes)")
    try:
        summary = ingest_results_zip(batch, zip_path, "web", upload.name, uploaded_by=uploaded_by)
        print(f"Results ZIP uploaded by {uploaded_by}: {summary['matched']} reports matched.")
    except (zipfile.BadZipFile, ValueError) as e:
        zip_path.unlink(missing_ok=True)
        return JsonResponse({"error": _zip_error_message(e)}, status=400)

    if summary["matched"] == 0:
        return JsonResponse({"error": "No reports matched any document in this batch.",
                             "unmatched": summary["unmatched"]}, status=400)

    tg.notify(f"📦 Results uploaded on the website for batch {batch.batch_id}: "
              f"{summary['matched']} reports matched.")
    email_results_received(batch, summary, "website", upload.name, uploaded_by)
    return JsonResponse({"batch_id": batch.batch_id, **summary})


# ---------- 3. Dashboard data ----------

def _fmt(dt):
    return timezone.localtime(dt).strftime("%d %b, %I:%M %p") if dt else ""


@api_login_required
def batch_state(request):
    """JSON for the dashboard: batch info, stat cards, one page of documents."""
    batch = _batch_or_latest(request.GET.get("batch_id"))
    if not batch:
        return JsonResponse({"batch": None})

    # Stat cards always reflect the whole batch; the table honours the filters.
    counts = Counter(batch.documents.values_list("status", flat=True))
    docs = _filtered_docs(request, batch)

    local = timezone.localtime(batch.uploaded_at)
    return JsonResponse({
        "batch": {
            "id": batch.batch_id,
            "title": f"Batch - {local:%d %b %Y}",
            "uploaded": f"{local:%d %b %Y, %I:%M %p}",
            "total": batch.total_files,
            "completed": counts["completed"],
            "in_progress": counts["in_progress"],
            "pending": counts["pending"],
            "stage": batch.stage,
            "confirmed_by": batch.confirmed_by,
            "confirmed_at": _fmt(batch.confirmed_at),
            "results_at": _fmt(batch.results_received_at),
        },
        "count": len(docs),
        "documents": [{
            "id": d.pk, "name": d.file_name, "format": d.file_format,
            "size": f"{d.size_bytes / 1024 ** 2:.1f} MB",
            "similarity": d.similarity_pct, "ai": d.ai_pct, "ai_low": d.ai_below_threshold,
            "status": d.status,
            "has_sim": bool(d.similarity_report), "has_ai": bool(d.ai_report),
        } for d in docs],
    })


@api_login_required
def document_detail(request, pk):
    """Everything we parsed from one document's reports, for the dashboard pop-up."""
    d = get_object_or_404(Document, pk=pk)
    data = d.report_data or {}
    return JsonResponse({
        "id": d.pk,
        "name": d.file_name,
        "format": d.file_format,
        "status": d.status,
        "similarity": d.similarity_pct,
        "ai": d.ai_pct,
        "ai_low": d.ai_below_threshold,
        "has_sim": bool(d.similarity_report),
        "has_ai": bool(d.ai_report),
        "cover": data.get("cover") or {},
        "sim": data.get("similarity") or {},
        "ai_detail": data.get("ai") or {},
    })


# ---------- 4. Downloads ----------

@api_login_required
def download_report(request, pk, kind):
    doc = get_object_or_404(Document, pk=pk)
    rel = {"similarity": doc.similarity_report, "ai": doc.ai_report}.get(kind)
    path = Path(settings.MEDIA_ROOT) / rel if rel else None
    if not path or not path.exists():
        raise Http404
    return FileResponse(open(path, "rb"), as_attachment=True,
                        filename=f"{doc.stem}_{kind}.pdf")


@api_login_required
def download_batch(request, batch_id):
    batch = get_object_or_404(Batch, batch_id=batch_id)
    path = Path(settings.MEDIA_ROOT) / batch.zip_path
    if not path.exists():
        raise Http404
    return FileResponse(open(path, "rb"), as_attachment=True, filename=batch.original_name)


@api_login_required
def download_reports_zip(request):
    """ZIP of all similarity + AI report PDFs for the documents matching the
    table filters (search, name, status), across ALL pages."""
    batch = _batch_or_latest(request.GET.get("batch_id"))
    if not batch:
        return JsonResponse({"error": "No batch found."}, status=404)

    docs = _filtered_docs(request, batch)
    media_root = Path(settings.MEDIA_ROOT).resolve()

    tmp = tempfile.TemporaryFile()      # on disk, so large batches don't fill RAM
    used_names = set()
    files_added = 0

    # PDFs are already compressed, so ZIP_STORED is faster and loses nothing.
    with ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for doc in docs:
            for kind, rel in (("similarity", doc.similarity_report),
                              ("ai", doc.ai_report)):
                if not rel:
                    continue

                path = (media_root / str(rel)).resolve()
                try:
                    path.relative_to(media_root)      # stay inside MEDIA_ROOT
                except ValueError:
                    continue
                if not path.is_file():
                    continue

                arcname = f"{doc.stem}_{kind}.pdf"
                n = 1
                while arcname in used_names:          # avoid duplicate names
                    n += 1
                    arcname = f"{doc.stem}_{kind}_{n}.pdf"
                used_names.add(arcname)

                zf.write(path, arcname=arcname)
                files_added += 1

    if not files_added:
        tmp.close()
        return JsonResponse(
            {"error": "No reports available for the documents currently shown."},
            status=404,
        )

    tmp.seek(0)
    return FileResponse(
        tmp,
        as_attachment=True,
        filename=f"reports_{batch.batch_id}.zip",
        content_type="application/zip",
    )


# ---------- Cron endpoint ----------

def telegram_tick(request, secret):
    """Server: cron-job.org calls this every minute.
    Works locally too (open the URL, or call it from a script)."""
    expected = settings.TELEGRAM_TICK_SECRET
    if not expected or not hmac.compare_digest(secret, expected):
        return HttpResponse(status=404)
    tg.tick()
    return HttpResponse("ok")


@require_POST
@csrf_protect
def clear_batch(request):
    media_root = Path(settings.MEDIA_ROOT).resolve()

    # Keep this directory consistent with your actual batch storage.
    batches_dir = (media_root / "batches").resolve()

    if batches_dir.parent != media_root:
        return JsonResponse(
            {"error": "Invalid batch directory."},
            status=400,
        )

    # Remove database records and their related records together.
    try:
        with transaction.atomic():
            Batch.objects.all().delete()

    except Exception:
        return JsonResponse(
            {"error": "Could not clear batch database records."},
            status=500,
        )

    # Remove files after the database transaction succeeds.
    if batches_dir.exists():
        if not batches_dir.is_dir() or batches_dir.is_symlink():
            return JsonResponse(
                {"error": "Batch storage path is invalid."},
                status=400,
            )

        for folder in batches_dir.iterdir():
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
            elif folder.is_file():
                folder.unlink()

    return JsonResponse({
        "success": True,
        "message": "All batches, documents, result records, and batch files have been cleared.",
    })