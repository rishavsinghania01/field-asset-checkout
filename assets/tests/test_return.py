from datetime import timedelta

import pytest
from django.utils import timezone

from assets.models import Asset, CheckOut

pytestmark = pytest.mark.django_db


@pytest.fixture
def open_checkout(asset, employee):
    co = CheckOut.objects.create(asset=asset, employee=employee, due_at=timezone.now() + timedelta(days=5))
    asset.status = Asset.Status.CHECKED_OUT
    asset.save()
    return co


def test_return_sets_returned_at_and_frees_asset(client, open_checkout):
    resp = client.post(
        f"/api/v1/checkouts/{open_checkout.id}/return/",
        {"condition_note": "Scratched lens cap", "needs_maintenance": False},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["returned_at"] is not None
    assert body["condition_note"] == "Scratched lens cap"

    open_checkout.refresh_from_db()
    assert open_checkout.returned_at is not None
    assert open_checkout.asset.status == Asset.Status.AVAILABLE


def test_return_with_maintenance_flag(client, open_checkout):
    resp = client.post(
        f"/api/v1/checkouts/{open_checkout.id}/return/",
        {"condition_note": "Battery swollen", "needs_maintenance": True},
        format="json",
    )
    assert resp.status_code == 200
    open_checkout.asset.refresh_from_db()
    assert open_checkout.asset.status == Asset.Status.MAINTENANCE


def test_return_body_is_optional(client, open_checkout):
    resp = client.post(f"/api/v1/checkouts/{open_checkout.id}/return/", {}, format="json")
    assert resp.status_code == 200
    assert resp.json()["condition_note"] == ""


def test_rule6_double_return_is_409(client, open_checkout):
    url = f"/api/v1/checkouts/{open_checkout.id}/return/"
    assert client.post(url, {}, format="json").status_code == 200
    resp = client.post(url, {"needs_maintenance": True}, format="json")
    assert resp.status_code == 409
    # The second call must not have touched the asset.
    open_checkout.asset.refresh_from_db()
    assert open_checkout.asset.status == Asset.Status.AVAILABLE


def test_return_unknown_checkout_is_404(client):
    assert client.post("/api/v1/checkouts/999999/return/", {}, format="json").status_code == 404


def test_returned_asset_can_be_checked_out_again(client, open_checkout, employee):
    client.post(f"/api/v1/checkouts/{open_checkout.id}/return/", {}, format="json")
    resp = client.post(
        "/api/v1/checkouts/",
        {
            "asset_tag": open_checkout.asset.asset_tag,
            "employee_code": employee.employee_code,
            "due_at": (timezone.now() + timedelta(days=2)).isoformat(),
        },
        format="json",
    )
    assert resp.status_code == 201
    assert CheckOut.objects.filter(asset=open_checkout.asset).count() == 2
