"""The one place a user's credit balances change.

`CustomUser.account_credits` and `monthly_credits` are a cached total. Every change goes through `apply`,
which locks the user row, writes `CreditTransaction` rows and updates the cache in one transaction, and
refuses to apply the same business event twice.
"""
from dataclasses import dataclass

from django.db import IntegrityError, transaction

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

    A repeat of a `key` that was already applied is a no-op and returns the current balances with
    `replayed=True`, which makes webhook redeliveries and job retries safe. Raises `InsufficientCredits`
    (writing nothing) if a bucket would go negative, and `ValueError` for an unknown bucket, an empty key, or a key
    already used for a different user. Keys are global: they name one event, such as a store transaction id.
    """
    if not key:
        raise ValueError("A credit change needs an idempotency key")
    changes = {bucket: int(amount) for bucket, amount in changes.items() if int(amount) != 0}
    unknown = set(changes) - set(_FIELDS)
    if unknown:
        raise ValueError(f"Unknown credit bucket: {', '.join(sorted(unknown))}")

    try:
        with transaction.atomic():
            user = CustomUser.objects.select_for_update().get(pk=user_id)
            earlier = CreditTransaction.objects.filter(idempotency_key=key).first()
            if earlier is not None:
                _require_same_user(earlier, user_id, key)
                return CreditResult(user.account_credits, user.monthly_credits, replayed=True)

            balances = {bucket: getattr(user, field) for bucket, field in _FIELDS.items()}
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
