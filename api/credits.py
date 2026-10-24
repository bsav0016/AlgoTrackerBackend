"""The one place a user's credit balances change.

`CustomUser.account_credits` and `monthly_credits` are a cached total. Every change goes through `apply`,
which locks the user row, writes `CreditTransaction` rows and updates the cache in one transaction, and
refuses to apply the same business event twice.
"""
from dataclasses import dataclass

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from api.models import CreditTransaction, CustomUser

ACCOUNT = CreditTransaction.ACCOUNT
MONTHLY = CreditTransaction.MONTHLY
_FIELDS = {ACCOUNT: "account_credits", MONTHLY: "monthly_credits"}


class InsufficientCredits(Exception):
    """The change would take a balance below zero. Nothing was written."""


@dataclass(frozen=True)
class CreditResult:
    account: int
    monthly: int
    replayed: bool  # True when this key had already been applied, so nothing changed


def apply(user_id, changes, key, kind, meta=None):
    """Apply signed changes, e.g. `{"account": 500}` or `{"monthly": -3, "account": -1}`, exactly once per `key`.

    `changes` may also be a function `(monthly_balance, account_balance) -> dict`, called while the user row is
    locked, for changes that depend on the balances (see `spend`).

    A repeat of a `key` that was already applied is a no-op and returns the current balances with
    `replayed=True`, which makes webhook redeliveries and job retries safe. Raises `InsufficientCredits`
    (writing nothing) if a bucket would go negative, and `ValueError` for an unknown bucket, an empty key, or a key
    already used for a different user. Keys are global: they name one event, such as a store transaction id.
    """
    if not key:
        raise ValueError("A credit change needs an idempotency key")
    if not callable(changes):
        changes = _clean(changes)

    try:
        with transaction.atomic():
            user = CustomUser.objects.select_for_update().get(pk=user_id)
            earlier = CreditTransaction.objects.filter(idempotency_key=key).first()
            if earlier is not None:
                _require_same_user(earlier, user_id, key)
                return CreditResult(user.account_credits, user.monthly_credits, replayed=True)

            balances = {bucket: getattr(user, field) for bucket, field in _FIELDS.items()}
            if callable(changes):
                changes = _clean(changes(balances[MONTHLY], balances[ACCOUNT]))
            for bucket, amount in changes.items():
                if balances[bucket] + amount < 0:
                    raise InsufficientCredits(f"{bucket} credits would go below zero")
                balances[bucket] += amount

            for bucket, amount in changes.items():
                CreditTransaction.objects.create(
                    user=user, bucket=bucket, amount=amount, balance_after=balances[bucket],
                    kind=kind, idempotency_key=key, meta=meta or {})
            if changes:
                for bucket, field in _FIELDS.items():
                    setattr(user, field, balances[bucket])
                user.save(update_fields=list(_FIELDS.values()))
            return CreditResult(balances[ACCOUNT], balances[MONTHLY], replayed=False)
    except IntegrityError:
        # Another request applied the same key between our check and our insert (only possible where the row
        # lock is not enforced). Its result stands; report it as a replay.
        user = CustomUser.objects.get(pk=user_id)
        earlier = CreditTransaction.objects.filter(idempotency_key=key).first()
        if earlier is None:
            raise
        _require_same_user(earlier, user_id, key)
        return CreditResult(user.account_credits, user.monthly_credits, replayed=True)


def _clean(changes):
    changes = {bucket: int(amount) for bucket, amount in changes.items() if int(amount) != 0}
    unknown = set(changes) - set(_FIELDS)
    if unknown:
        raise ValueError(f"Unknown credit bucket: {', '.join(sorted(unknown))}")
    return changes


def _require_same_user(earlier, user_id, key):
    # A key names one business event, so it belongs to one user. Reusing it for someone else is a caller bug,
    # and silently treating it as a replay would hide a missing credit.
    if earlier.user_id != user_id:
        raise ValueError(f"Idempotency key {key!r} was already used for another user")


def split_spend(monthly_balance, account_balance, amount):
    """How to pay `amount` credits: monthly credits first, then account credits.

    Returns `{"monthly": -a, "account": -b}` for `apply`, or raises `InsufficientCredits` when the two
    balances together cannot cover it.
    """
    amount = int(amount)
    if amount < 0:
        raise ValueError("Cannot spend a negative amount")
    if monthly_balance + account_balance < amount:
        raise InsufficientCredits("Not enough credits")
    from_monthly = min(monthly_balance, amount)
    return {MONTHLY: -from_monthly, ACCOUNT: -(amount - from_monthly)}


def spend(user_id, amount, key, kind, meta=None):
    """Pay `amount` credits, monthly credits first, exactly once per `key`.

    The split is worked out while the user row is locked, so concurrent spends cannot both use the same
    credits. Raises `InsufficientCredits` (writing nothing) when the two balances cannot cover it.
    """
    return apply(user_id, lambda monthly, account: split_spend(monthly, account, amount), key, kind, meta)


def refund(user_id, key, refund_key, kind, meta=None):
    """Undo what `key` applied, putting each amount back in the bucket it came from, once per `refund_key`.

    Returns the new `CreditResult`, or `None` when `key` was never applied (so there is nothing to give back).
    """
    rows = list(CreditTransaction.objects.filter(idempotency_key=key, user_id=user_id))
    if not rows:
        return None
    return apply(user_id, {row.bucket: -row.amount for row in rows}, refund_key, kind,
                 meta={**(meta or {}), "refund_of": key})


def reset_monthly(user_id, allowance, days=30):
    """If the user's monthly reset date has passed, set the monthly balance to `allowance`.

    Unused monthly credits expire (purchased account credits are never touched). Happens once per reset date,
    under the user lock, and the next reset date moves `days` ahead. Returns True when a reset happened.
    """
    with transaction.atomic():
        user = CustomUser.objects.select_for_update().get(pk=user_id)
        now = timezone.now()
        due = user.reset_monthly_credits
        if now <= due:
            return False
        apply(user_id, {MONTHLY: allowance - user.monthly_credits}, f"monthly:{user_id}:{int(due.timestamp())}",
              "monthly_grant", meta={"expired": user.monthly_credits, "granted": allowance})
        CustomUser.objects.filter(pk=user_id).update(reset_monthly_credits=now + timedelta(days=days))
        return True


def record_opening_balance(user):
    """Start a new user's ledger: one row per non-zero starting balance, key `opening:<id>`.

    Without it the ledger would not add up to the balance a user is created with (the monthly allowance, or
    credits set by hand). The data migration that introduced the ledger uses the same key for existing users.
    """
    rows = [
        CreditTransaction(user=user, bucket=bucket, amount=balance, balance_after=balance, kind="adjustment",
                          idempotency_key=f"opening:{user.pk}", meta={"reason": "opening balance"})
        for bucket, balance in ((ACCOUNT, user.account_credits), (MONTHLY, user.monthly_credits)) if balance
    ]
    CreditTransaction.objects.bulk_create(rows)
