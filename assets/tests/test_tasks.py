from datetime import timedelta

import pytest
from django.utils import timezone

from assets.models import CheckOut, OverdueNotice
from assets.tasks import flag_overdue_checkouts

from .conftest import make_asset, make_employee

pytestmark = pytest.mark.django_db


@pytest.fixture
def overdue_set():
    emp = make_employee()
    now = timezone.now()
    overdue = [
        CheckOut.objects.create(asset=make_asset(f"OD-{i}"), employee=emp, due_at=now - timedelta(days=i + 1))
        for i in range(3)
    ]
    not_due = CheckOut.objects.create(asset=make_asset("OK-1"), employee=emp, due_at=now + timedelta(days=2))
    returned = CheckOut.objects.create(
        asset=make_asset("RET-1"), employee=emp, due_at=now - timedelta(days=9), returned_at=now
    )
    return overdue, not_due, returned


def test_task_creates_one_notice_per_overdue_checkout(overdue_set):
    overdue, not_due, returned = overdue_set
    result = flag_overdue_checkouts()

    assert result["created"] == 3
    assert OverdueNotice.objects.count() == 3
    assert set(OverdueNotice.objects.values_list("checkout_id", flat=True)) == {c.id for c in overdue}
    assert not OverdueNotice.objects.filter(checkout__in=[not_due, returned]).exists()
    assert set(OverdueNotice.objects.values_list("notice_date", flat=True)) == {timezone.now().date()}


def test_task_is_idempotent_within_a_day(overdue_set):
    first = flag_overdue_checkouts()
    second = flag_overdue_checkouts()
    for _ in range(3):
        flag_overdue_checkouts()

    assert first["created"] == 3
    assert second["created"] == 0
    assert OverdueNotice.objects.count() == 3


def test_task_picks_up_newly_overdue_items(overdue_set):
    flag_overdue_checkouts()
    overdue, not_due, _ = overdue_set
    # Item becomes overdue between runs.
    CheckOut.objects.filter(pk=not_due.pk).update(due_at=timezone.now() - timedelta(minutes=1))
    result = flag_overdue_checkouts()
    assert result["created"] == 1
    assert OverdueNotice.objects.filter(checkout=not_due).count() == 1
    assert OverdueNotice.objects.count() == 4


def test_task_skips_returned_items(overdue_set):
    overdue, _, _ = overdue_set
    CheckOut.objects.filter(pk=overdue[0].pk).update(returned_at=timezone.now())
    result = flag_overdue_checkouts()
    assert result["created"] == 2


def test_unique_constraint_per_checkout_per_day(overdue_set):
    from django.db import IntegrityError, transaction

    overdue, _, _ = overdue_set
    today = timezone.now().date()
    OverdueNotice.objects.create(checkout=overdue[0], notice_date=today)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            OverdueNotice.objects.create(checkout=overdue[0], notice_date=today)
    # A different day is fine.
    OverdueNotice.objects.create(checkout=overdue[0], notice_date=today - timedelta(days=1))


def test_task_is_registered_with_celery():
    from config.celery import app

    assert "assets.tasks.flag_overdue_checkouts" in app.tasks
    assert "flag-overdue-checkouts-hourly" in app.conf.beat_schedule
