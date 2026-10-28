import logging
import time

from django.core.management.base import BaseCommand

from api.push_sweep import sweep

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("Re-queue push sends that were missed and settle sent pushes from Expo's receipts. Runs forever, "
            "once per --interval seconds, unless --once is given.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between passes (default 60).")

    def handle(self, *args, **options):
        while True:
            try:
                requeued, settled = sweep()
                if requeued or settled:
                    logger.info("Push sweep: re-queued %s notifications, settled %s receipts", requeued, settled)
            except Exception:
                logger.exception("Push sweep failed")  # the next pass tries again
            if options["once"]:
                return
            time.sleep(options["interval"])
