import asyncio

from django.core.management.base import BaseCommand

from api.redis_manager import RedisManager


class Command(BaseCommand):
    help = (
        "Load recent candles (with highs and lows) from TwelveData for every configured symbol and interval, so "
        "strategies have enough history on a fresh Redis. Pairs that already have candles are skipped; strategies "
        "are not run on history. Uses TwelveData credits."
    )

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Clear the stored candles of every pair first, then load again")

    def handle(self, *args, **options):
        asyncio.run(RedisManager.populate_redis_all(force=options["force"]))
        self.stdout.write("Candles loaded.")
