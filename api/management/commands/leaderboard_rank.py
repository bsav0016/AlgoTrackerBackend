import logging
import time

from django.core.management.base import BaseCommand

from api import leaderboard_job

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("Take the weekly leaderboard snapshots, publish a finished week's board once, and rebuild the all-time "
            "board. Runs forever, once per --interval seconds, unless --once is given.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
        parser.add_argument("--interval", type=int, default=3600, help="Seconds between passes (default 3600).")

    def handle(self, *args, **options):
        while True:
            try:
                snapshots, weekly, all_time = leaderboard_job.run()
                logger.info("Leaderboard: %s snapshots, weekly board %s, %s all-time entries", snapshots,
                            "already published" if weekly is None else f"{weekly} entries", all_time)
            except Exception:
                logger.exception("Leaderboard run failed")  # the next pass tries again
            if options["once"]:
                return
            time.sleep(options["interval"])
