from datetime import timedelta

import pytest
from django.utils import timezone

from assets.models import Asset, CheckOut

from .conftest import make_asset, make_employee

pytestmark = pytest.mark.django_db

URL = "/api/v1/checkouts/"


def due_in(days=7, **kw):
    return (timezone.now() + timedelta(days=days, **kw)).isoformat()


def post_checkout(client, asset_tag="CAM-001", employee_code="E001", due_at=None):
    return client.post(
        URL,
        {"asset_tag": asset_tag, "employee_code": employee_code, "due_at": due_at or due_in()},
        format="json",
    )


def test_successful_checkout_creates_row_and_flips_status(client, asset, employee):
    resp = post_checkout(client)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["asset_tag"] == "CAM-001"
    assert body["employee_code"] == "E001"
    assert body["returned_at"] is None

    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1


def test_rule1_unavailable_asset_is_409(client, employee):
    make_asset("CAM-M", status="MAINTENANCE")
    make_asset("CAM-C", status="CHECKED_OUT")
    assert post_checkout(client, asset_tag="CAM-M").status_code == 409
    assert post_checkout(client, asset_tag="CAM-C").status_code == 409
    assert CheckOut.objects.count() == 0


def test_rule2_inactive_employee_is_400(client, asset):
    make_employee("E999", "Gone Person", active=False)
    resp = post_checkout(client, employee_code="E999")
    assert resp.status_code == 400
    assert "employee_code" in resp.json()
    asset.refresh_from_db()
    assert asset.status == Asset.Status.AVAILABLE


def test_rule3_fourth_open_checkout_is_409(client, employee):
    tags = ["A-1", "A-2", "A-3", "A-4"]
    for t in tags:
        make_asset(t, name=t)

    for t in tags[:3]:
        assert post_checkout(client, asset_tag=t).status_code == 201

    resp = post_checkout(client, asset_tag="A-4")
    assert resp.status_code == 409
    assert "3 open" in resp.json()["detail"]
    assert Asset.objects.get(asset_tag="A-4").status == Asset.Status.AVAILABLE

    # Returning one frees a slot.
    first = CheckOut.objects.get(asset__asset_tag="A-1")
    assert client.post(f"{URL}{first.id}/return/", {}, format="json").status_code == 200
    assert post_checkout(client, asset_tag="A-4").status_code == 201


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(days=-1), 400),      # past
        (timedelta(seconds=-1), 400),   # just past
        (timedelta(days=31), 400),      # beyond 30 days
        (timedelta(minutes=5), 201),    # near future
        (timedelta(days=29, hours=23), 201),
    ],
)
def test_rule4_due_at_window(client, asset, employee, delta, expected):
    resp = post_checkout(client, due_at=(timezone.now() + delta).isoformat())
    assert resp.status_code == expected, resp.content


def test_rule8_unknown_asset_or_employee_is_404(client, asset, employee):
    assert post_checkout(client, asset_tag="NOPE").status_code == 404
    assert post_checkout(client, employee_code="NOPE").status_code == 404
    assert CheckOut.objects.count() == 0


def test_missing_fields_is_400(client):
    resp = client.post(URL, {"asset_tag": "CAM-001"}, format="json")
    assert resp.status_code == 400
    assert set(resp.json()) >= {"employee_code", "due_at"}


def test_rule5_partial_unique_index_blocks_second_open_row(asset, employee):
    """The DB constraint itself, independent of the API."""
    from django.db import IntegrityError, transaction

    CheckOut.objects.create(asset=asset, employee=employee, due_at=timezone.now() + timedelta(days=1))
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CheckOut.objects.create(asset=asset, employee=employee, due_at=timezone.now() + timedelta(days=1))

    # A returned row does not count, so a new open one is allowed.
    CheckOut.objects.filter(asset=asset).update(returned_at=timezone.now())
    CheckOut.objects.create(asset=asset, employee=employee, due_at=timezone.now() + timedelta(days=1))
    assert CheckOut.objects.filter(asset=asset).count() == 2
