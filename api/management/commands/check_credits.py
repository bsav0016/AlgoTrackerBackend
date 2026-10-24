from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from api.models import CreditTransaction, CustomUser

FIELDS = {"account": "account_credits", "monthly": "monthly_credits"}


def find_problems(users):
    """Compare each user's cached balances with their ledger. Returns a list of readable problems.

    Per bucket the ledger must start from zero, every row's `balance_after` must be the previous one plus its
    `amount`, and the last `balance_after` must equal the cached balance (a user with no rows must be at zero).
    """
    rows_by_bucket = defaultdict(list)
    ids = [user.pk for user in users]
    for user_id, bucket, amount, balance_after, row_id in (
            CreditTransaction.objects.filter(user_id__in=ids).order_by("id")
            .values_list("user_id", "bucket", "amount", "balance_after", "id").iterator()):
        rows_by_bucket[(user_id, bucket)].append((row_id, amount, balance_after))

    problems = []
    for user in users:
        for bucket, field in FIELDS.items():
            cached = getattr(user, field)
            running = 0
            for row_id, amount, balance_after in rows_by_bucket.get((user.pk, bucket), []):
                if running + amount != balance_after:
                    problems.append(f"{user.username} {bucket}: ledger row {row_id} says the balance became "
                                    f"{balance_after}, but {running} {amount:+d} is {running + amount}")
                running = balance_after
            if running != cached:
                problems.append(f"{user.username} {bucket}: balance is {cached} but the ledger ends at {running}")
    return problems


class Command(BaseCommand):
    help = ("Check that every user's credit balances match the credit ledger. Prints each mismatch and exits "
            "with an error if there is any, so it can run from cron or CI.")

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Only check this username")

    def handle(self, *args, **options):
        users = CustomUser.objects.all().order_by("pk")
        if options["user"]:
            users = users.filter(username=options["user"])
        users = list(users)
        if options["user"] and not users:
            raise CommandError(f"No user named {options['user']!r}")

        problems = find_problems(users)
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} credit mismatch(es) across {len(users)} user(s) checked")
        self.stdout.write(f"OK: {len(users)} user(s) checked, every balance matches its ledger")
