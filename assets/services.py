"""
Business rules for checking assets out and back in.

Everything that changes state goes through here so the API layer stays thin
and the rules can be unit-tested without HTTP.
"""

from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from .exceptions import Conflict
from .models import Asset, CheckOut, Employee

MAX_OPEN_CHECKOUTS_PER_EMPLOYEE = 3


@transaction.atomic
def check_out_asset(*, asset_tag: str, employee_code: str, due_at: datetime) -> CheckOut:
    """
    Rules 1, 2, 3, 5, 7 and 8. Rule 4 (due_at window) is validated at the
    serializer layer before we get here.

    Concurrency: both the employee row and the asset row are locked with
    SELECT ... FOR UPDATE for the duration of the transaction, always in the
    same order (employee, then asset) so two requests can never deadlock.
    A second request for the same asset blocks on the row lock, then sees the
    status already flipped to CHECKED_OUT and gets a 409. If anything ever
    slipped past the lock, the partial unique index
    `uniq_open_checkout_per_asset` makes the INSERT itself fail.
    """
    # Lock the employee first: this serialises the "at most three open" check
    # for the same person, which the asset lock alone would not cover.
    try:
        employee = Employee.objects.select_for_update().get(employee_code=employee_code)
    except Employee.DoesNotExist:
        raise NotFound(f"Employee {employee_code!r} not found.")

    try:
        asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)
    except Asset.DoesNotExist:
        raise NotFound(f"Asset {asset_tag!r} not found.")

    if not employee.is_active:
        raise ValidationError({"employee_code": "Employee is inactive and cannot check out assets."})

    if asset.status != Asset.Status.AVAILABLE:
        raise Conflict(f"Asset {asset_tag} is {asset.status}, not AVAILABLE.")

    open_count = CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count()
    if open_count >= MAX_OPEN_CHECKOUTS_PER_EMPLOYEE:
        raise Conflict(
            f"Employee {employee_code} already holds {open_count} open check-outs "
            f"(limit {MAX_OPEN_CHECKOUTS_PER_EMPLOYEE})."
        )

    try:
        checkout = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
    except IntegrityError:
        # Partial unique index tripped: another open check-out exists for this
        # asset. Only reachable if the row lock was bypassed, but the database
        # is the last line of defence.
        raise Conflict(f"Asset {asset_tag} already has an open check-out.")

    # Same transaction as the INSERT above: both persist or neither does.
    asset.status = Asset.Status.CHECKED_OUT
    asset.save(update_fields=["status", "updated_at"])
    return checkout


@transaction.atomic
def return_checkout(
    *, checkout_id: int, condition_note: str = "", needs_maintenance: bool = False
) -> CheckOut:
    """
    Rule 6. Locks the check-out row (and then its asset) so two concurrent
    returns cannot both succeed.
    """
    try:
        checkout = CheckOut.objects.select_for_update().get(pk=checkout_id)
    except CheckOut.DoesNotExist:
        raise NotFound(f"Check-out {checkout_id} not found.")

    if checkout.returned_at is not None:
        raise Conflict(f"Check-out {checkout_id} was already returned at {checkout.returned_at.isoformat()}.")

    asset = Asset.objects.select_for_update().get(pk=checkout.asset_id)

    now = timezone.now()
    checkout.returned_at = now
    checkout.condition_note = condition_note or ""
    checkout.save(update_fields=["returned_at", "condition_note"])

    new_status = Asset.Status.MAINTENANCE if needs_maintenance else Asset.Status.AVAILABLE
    asset.status = new_status
    asset.save(update_fields=["status", "updated_at"])

    checkout.asset = asset
    return checkout
