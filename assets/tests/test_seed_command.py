import pytest
from django.core.management import call_command
from django.utils import timezone

from assets.models import Asset, CheckOut, Employee

pytestmark = pytest.mark.django_db


def test_seed_meets_assignment_minimums():
    call_command("seed_demo_data", verbosity=0)
    now = timezone.now()

    assert Asset.objects.count() >= 8
    assert set(Asset.objects.values_list("category", flat=True)) == {"CAMERA", "LAPTOP", "SENSOR", "VEHICLE"}
    assert Employee.objects.count() >= 4
    assert Employee.objects.filter(is_active=False).count() >= 1

    open_overdue = CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
    assert open_overdue.count() >= 2
    returned = CheckOut.objects.filter(returned_at__isnull=False)
    assert returned.filter(returned_at__lte=models_f("due_at")).count() >= 2  # on time
    assert returned.filter(returned_at__gt=models_f("due_at")).count() >= 1  # late

    # Status is consistent with history: every open check-out's asset is CHECKED_OUT.
    for co in CheckOut.objects.filter(returned_at__isnull=True).select_related("asset"):
        assert co.asset.status == "CHECKED_OUT"
    # ...and no AVAILABLE asset has an open check-out.
    assert not CheckOut.objects.filter(returned_at__isnull=True, asset__status="AVAILABLE").exists()


def test_seed_is_rerunnable():
    call_command("seed_demo_data", verbosity=0)
    snapshot = _snapshot()
    call_command("seed_demo_data", verbosity=0)
    call_command("seed_demo_data", verbosity=0)
    assert _snapshot() == snapshot


def test_seed_prints_token(capsys):
    call_command("seed_demo_data")
    out = capsys.readouterr().out
    assert "API token for user 'reviewer'" in out


def models_f(name):
    from django.db.models import F

    return F(name)


def _snapshot():
    return {
        "assets": list(Asset.objects.order_by("asset_tag").values_list("asset_tag", "status")),
        "employees": Employee.objects.count(),
        "checkouts": CheckOut.objects.count(),
        "open": CheckOut.objects.filter(returned_at__isnull=True).count(),
    }
