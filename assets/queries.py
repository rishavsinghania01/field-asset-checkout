"""
Read-side queries: employee summary and overdue report.

Kept separate from services.py (which mutates state) so the report logic can
be reused by the Celery task and tested with a controlled "now".
"""

from datetime import datetime

from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Q,
    QuerySet,
    Value,
)
from django.utils import timezone

from .models import CheckOut, Employee


def open_checkouts() -> QuerySet[CheckOut]:
    return CheckOut.objects.filter(returned_at__isnull=True)


def overdue_checkouts(now: datetime | None = None) -> QuerySet[CheckOut]:
    """
    Open check-outs whose due_at is strictly in the past.

    "Overdue" means due_at < now. An item due at exactly this instant is not
    overdue yet; it becomes overdue one tick later. Ordered most overdue first.
    """
    now = now or timezone.now()
    return (
        open_checkouts()
        .filter(due_at__lt=now)
        .select_related("asset", "employee")
        .annotate(overdue_for=ExpressionWrapper(Value(now) - F("due_at"), output_field=DurationField()))
        .order_by("due_at", "id")
    )


def employee_summary(employee_code: str, now: datetime | None = None) -> Employee | None:
    """
    One query. Returns the Employee annotated with:
      lifetime_checkouts, currently_held, currently_overdue, mean_hold_days
    or None when the employee does not exist.

    Counts and the average are all conditional aggregates over the LEFT JOIN
    to checkouts, so the database does the work in a single GROUP BY.
    mean_hold is an interval; the view turns it into days.
    """
    now = now or timezone.now()
    is_open = Q(checkouts__returned_at__isnull=True)
    is_returned = Q(checkouts__returned_at__isnull=False)
    hold_duration = ExpressionWrapper(
        F("checkouts__returned_at") - F("checkouts__checked_out_at"),
        output_field=DurationField(),
    )
    return (
        Employee.objects.filter(employee_code=employee_code)
        .annotate(
            lifetime_checkouts=Count("checkouts"),
            currently_held=Count("checkouts", filter=is_open),
            currently_overdue=Count("checkouts", filter=is_open & Q(checkouts__due_at__lt=now)),
            mean_hold=Avg(hold_duration, filter=is_returned),
        )
        .first()
    )
