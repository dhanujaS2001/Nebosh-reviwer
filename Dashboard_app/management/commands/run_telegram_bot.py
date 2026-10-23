"""Runs tick() in a loop. Use it locally (or as an always-on task on a paid plan).
On the free PythonAnywhere plan, cron-job.org calls /telegram/tick/<secret>/ instead.
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from Dashboard_app import telegram_bot as tg


class Command(BaseCommand):
    help = "Loop: read Confirm clicks, send new batches, send reminders."

    def add_arguments(self, parser):
        parser.add_argument("--every", type=int, default=5,
                            help="Seconds between ticks (default 5).")

    def handle(self, *args, **opts):
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            raise CommandError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")

        self.stdout.write(self.style.SUCCESS(
            f"Telegram worker started (reminders every "
            f"{settings.TELEGRAM_REMINDER_SECONDS}s). Ctrl+C to stop."))
        while True:
            try:
                tg.tick()
                time.sleep(opts["every"])
            except KeyboardInterrupt:
                self.stdout.write("Stopped.")
                break
            except Exception as e:
                self.stderr.write(f"Tick error: {e}")
                time.sleep(10)