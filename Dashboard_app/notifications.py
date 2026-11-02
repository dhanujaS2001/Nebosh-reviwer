import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

log = logging.getLogger(__name__)


def email_results_received(batch, summary, channel, filename, uploaded_by):
    """Email the configured address(es) when a results ZIP is ingested.
    Never raises, so a mail failure can't break the upload or the Telegram tick."""
    recipients = getattr(settings, "RESULTS_NOTIFY_EMAILS", [])
    if not recipients:
        return

    when = timezone.localtime().strftime("%d %b %Y, %I:%M %p")
    lines = [
        f"Review reports were received for batch {batch.batch_id}.",
        "",
        f"Channel:      {channel}",
        f"Uploaded by:  {uploaded_by}",
        f"File:         {filename}",
        f"Received:     {when}",
        f"Reports matched:   {summary.get('matched', 0)}",
        f"Documents updated: {summary.get('documents_updated', 0)}",
    ]
    unmatched = summary.get("unmatched") or []
    if unmatched:
        lines.append(f"Not matched ({len(unmatched)}): " + ", ".join(unmatched[:10]))

    try:
        send_mail(
            subject=f"Review reports received - batch {batch.batch_id}",
            message="\n".join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=list(recipients),
            fail_silently=False,
        )
    except Exception:
        log.exception("Could not send results email for batch %s", batch.batch_id)