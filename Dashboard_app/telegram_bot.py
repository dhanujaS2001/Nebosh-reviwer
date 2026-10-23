"""Telegram integration that works the same locally and on PythonAnywhere.

Everything happens inside tick():
  1. read Confirm-button clicks AND reviewer ZIP uploads (getUpdates polling, no webhook needed)
  2. send newly uploaded batches
  3. send reminders for batches that are still unconfirmed

Locally, `python manage.py run_telegram_bot` calls tick() every few seconds.
On the server, cron-job.org calls /telegram/tick/<secret>/ every minute.
"""
import json
import logging
import re
from datetime import timedelta
from pathlib import Path

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_SEND_ATTEMPTS = 5
TG_DOWNLOAD_LIMIT = 20 * 1024 ** 2     # Bot API getFile hard limit (20 MB)


class TelegramError(RuntimeError):
    pass


def _call(method, data=None, files=None, timeout=30):
    url = API.format(token=settings.TELEGRAM_BOT_TOKEN, method=method)
    resp = requests.post(url, data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except ValueError:
        raise TelegramError(f"{method}: bad response ({resp.status_code})")
    if not payload.get("ok"):
        raise TelegramError(f"{method}: {payload.get('description', 'unknown error')}")
    return payload["result"]


def _confirm_keyboard(batch):
    return json.dumps({"inline_keyboard": [[
        {"text": "✅ Confirm", "callback_data": f"confirm:{batch.batch_id}"}
    ]]})


def _strip_buttons(message_id):
    if not message_id:
        return
    try:
        _call("editMessageReplyMarkup", {
            "chat_id": settings.TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })
    except TelegramError:
        pass   # already removed or message deleted


def _delete(message_id):
    if not message_id:
        return
    try:
        _call("deleteMessage", {"chat_id": settings.TELEGRAM_CHAT_ID,
                                "message_id": message_id})
    except TelegramError:
        pass


def _send(text, reply_to=None):
    """Plain message to the reviewer chat. Never raises."""
    data = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": text}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    try:
        _call("sendMessage", data)
    except TelegramError:
        pass


notify = _send          # used by the website view: tg.notify("...")


def _sender_label(msg):
    u = msg.get("from") or {}
    handle = f"@{u['username']}" if u.get("username") else u.get("first_name", "unknown")
    return f"tg:{handle}"


# ---------- Sending ----------
def send_batch(batch):
    """ZIP + caption + Confirm button in ONE message (no duplicates on retry)."""
    types = ", ".join(f"{n} {ext.upper()}" for ext, n in batch.by_type.items())
    received = timezone.localtime(batch.uploaded_at).strftime("%d %b %Y, %I:%M %p")
    caption = (
        "📥 New document batch received\n"
        f"Batch: {batch.batch_id}\n"
        f"Files inside: {batch.total_files} ({types})\n"
        f"Received: {received}\n\n"
        "Tap Confirm once you have received it, then reply to this message "
        "with a ZIP of the Turnitin reports (Similarity + AI Writing). "
    )
    zip_file = Path(settings.MEDIA_ROOT) / batch.zip_path
    with open(zip_file, "rb") as f:
        result = _call(
            "sendDocument",
            data={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "caption": caption,
                "reply_markup": _confirm_keyboard(batch),
            },
            files={"document": (batch.original_name, f)},
            timeout=300,
        )
    batch.initial_message_id = result["message_id"]
    batch.initial_sent_at = timezone.now()
    batch.last_error = ""
    batch.save(update_fields=["initial_message_id", "initial_sent_at", "last_error"])


def send_reminder(batch):
    _delete(batch.reminder_message_id)          # keep one live reminder only
    n = batch.reminder_count + 1
    result = _call("sendMessage", {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": (f"⏰ Reminder #{n}: batch {batch.batch_id} "
                 f"({batch.total_files} files) is still waiting for confirmation."),
        "reply_markup": _confirm_keyboard(batch),
    })
    batch.reminder_message_id = result["message_id"]
    batch.reminder_count = n
    batch.save(update_fields=["reminder_message_id", "reminder_count"])


# ---------- Reading Confirm clicks ----------
def handle_callback(cq):
    from .models import Batch

    data = cq.get("data", "")
    if not data.startswith("confirm:"):
        return

    def answer(text):
        try:
            _call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text})
        except TelegramError:
            pass   # query too old (button tapped while no tick was running)

    chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
    if chat_id != str(settings.TELEGRAM_CHAT_ID):
        return answer("Not allowed.")

    batch = Batch.objects.filter(batch_id=data.split(":", 1)[1]).first()
    if not batch:
        return answer("Batch not found.")

    user = cq.get("from", {})
    name = f"@{user['username']}" if user.get("username") else user.get("first_name", "someone")

    # Atomic: only the first click wins
    won = Batch.objects.filter(pk=batch.pk, confirmed_at__isnull=True).update(
        confirmed_at=timezone.now(), confirmed_by=name
    )
    batch.refresh_from_db()
    _strip_buttons(batch.initial_message_id)
    _strip_buttons(batch.reminder_message_id)

    if not won:
        return answer("Already confirmed.")

    answer("Confirmed ✅")
    _delete(batch.reminder_message_id)
    when = timezone.localtime(batch.confirmed_at).strftime("%d %b %Y, %I:%M %p")
    _call("sendMessage", {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": f"✅ Batch {batch.batch_id} confirmed by {name} at {when}. Reminders stopped.",
    })


# ---------- Receiving the reviewer's results ZIP ----------
def _resolve_batch(msg):
    """1) reply to our message  2) 12-hex batch id in caption  3) oldest batch still waiting for results."""
    from .models import Batch

    replied = (msg.get("reply_to_message") or {}).get("message_id")
    if replied:
        b = Batch.objects.filter(
            Q(initial_message_id=replied) | Q(reminder_message_id=replied)
        ).first()
        if b:
            return b
    m = re.search(r"\b[0-9a-f]{12}\b", msg.get("caption") or "")
    if m:
        b = Batch.objects.filter(batch_id=m.group(0)).first()
        if b:
            return b
    return (Batch.objects.filter(results_received_at__isnull=True).order_by("uploaded_at").first()
            or Batch.objects.order_by("-uploaded_at").first())


def handle_document(msg):
    from .services import ingest_results_zip

    if str((msg.get("chat") or {}).get("id")) != str(settings.TELEGRAM_CHAT_ID):
        return                                   # same chat check as Confirm
    doc, mid = msg["document"], msg["message_id"]
    name = doc.get("file_name", "")

    if not name.lower().endswith(".zip"):
        return _send("Please send the reports as a .zip file.", mid)
    if doc.get("file_size", 0) > TG_DOWNLOAD_LIMIT:
        return _send("Telegram bots can only download files up to 20 MB. "
                     "Please upload this ZIP on the website instead.", mid)

    batch = _resolve_batch(msg)
    if not batch:
        return _send("No batch found.", mid)

    zip_path = None
    try:
        info = _call("getFile", {"file_id": doc["file_id"]})
        res_dir = Path(settings.MEDIA_ROOT) / "batches" / batch.batch_id / "results"
        res_dir.mkdir(parents=True, exist_ok=True)
        zip_path = res_dir / f"{timezone.now():%Y%m%d%H%M%S}.zip"
        url = f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{info['file_path']}"
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        s = ingest_results_zip(batch, zip_path, "telegram", name,
                               uploaded_by=_sender_label(msg))
    except Exception as e:
        log.exception("Failed to process results ZIP for batch %s", batch.batch_id)
        if zip_path:
            zip_path.unlink(missing_ok=True)
        return _send(f"Could not process that ZIP: {str(e)[:200]}", mid)

    # Results count as acceptance (ingest sets confirmed_at when something matched);
    # clean up the Confirm buttons only in that case
    if s["matched"]:
        _strip_buttons(batch.initial_message_id)
        if batch.reminder_message_id:
            _strip_buttons(batch.reminder_message_id)
            _delete(batch.reminder_message_id)

    text = (f"📦 Batch {batch.batch_id}: {s['matched']} reports matched, "
            f"{s['documents_updated']} documents updated.")
    if s["unmatched"]:
        text += f"\nNot matched ({len(s['unmatched'])}): " + ", ".join(s["unmatched"][:10])
    _send(text, mid)


# ---------- Update dispatcher ----------
def handle_update(update):
    if update.get("callback_query"):
        handle_callback(update["callback_query"])
    elif (msg := update.get("message")) and msg.get("document"):
        handle_document(msg)


def poll_updates():
    """Fetch new button clicks and uploaded ZIPs. The last processed update id is
    stored in the DB, so nothing is handled twice, even across restarts."""
    from .models import BotState

    state, _ = BotState.objects.get_or_create(key="telegram_offset", defaults={"value": "0"})
    offset = int(state.value or 0)

    params = {"timeout": 0,
              "allowed_updates": json.dumps(["callback_query", "message"])}
    if offset:
        params["offset"] = offset
    try:
        updates = _call("getUpdates", params, timeout=20)
    except TelegramError as e:
        if "webhook" in str(e).lower():       # getUpdates is blocked while a webhook is set
            try:
                _call("deleteWebhook")
            except TelegramError:
                pass
        return

    for u in updates:
        try:
            handle_update(u)
        except Exception:
            log.exception("Error handling Telegram update %s", u.get("update_id"))
        offset = max(offset, u["update_id"] + 1)

    if updates:
        state.value = str(offset)
        state.save(update_fields=["value"])


# ---------- The one function everything calls ----------
def tick():
    """Safe if two calls overlap: batches and reminders are claimed atomically."""
    from .models import Batch

    # 1) Confirm clicks and results ZIPs first, so a just-confirmed batch gets no more reminders
    poll_updates()

    # 2) new batches
    for b in Batch.objects.filter(initial_sent_at__isnull=True,
                                  send_attempts__lt=MAX_SEND_ATTEMPTS):
        claimed = Batch.objects.filter(
            pk=b.pk, initial_sent_at__isnull=True, send_attempts=b.send_attempts
        ).update(send_attempts=b.send_attempts + 1)
        if not claimed:
            continue                      # another call is already sending it
        try:
            send_batch(b)
        except Exception as e:
            b.last_error = str(e)[:500]
            b.save(update_fields=["last_error"])

    # 3) reminders for unconfirmed batches
    interval = timedelta(seconds=settings.TELEGRAM_REMINDER_SECONDS)
    now = timezone.now()
    for b in Batch.objects.filter(initial_sent_at__isnull=False,
                                  confirmed_at__isnull=True):
        prev = b.last_reminder_at
        if now - (prev or b.initial_sent_at) < interval:
            continue
        claimed = Batch.objects.filter(
            pk=b.pk, confirmed_at__isnull=True, last_reminder_at=prev
        ).update(last_reminder_at=now)
        if not claimed:
            continue
        try:
            send_reminder(b)
        except Exception:
            log.exception("Reminder failed for batch %s", b.batch_id)