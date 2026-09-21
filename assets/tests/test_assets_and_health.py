import pytest
from django.utils import timezone

from assets.models import CheckOut

from .conftest import make_asset, make_employee

pytestmark = pytest.mark.django_db


def test_health_is_unauthenticated(anon_client):
    resp = anon_client.get("/api/v1/health/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": True}


def test_assets_require_auth(anon_client):
    assert anon_client.get("/api/v1/assets/").status_code == 401


def test_create_asset(client):
    payload = {
        "asset_tag": "LAP-100",
        "name": "ThinkPad X1",
        "category": "LAPTOP",
        "purchase_date": "2025-03-01",
    }
    resp = client.post("/api/v1/assets/", payload, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["status"] == "AVAILABLE"
    assert body["asset_tag"] == "LAP-100"


def test_create_asset_rejects_bad_category(client):
    resp = client.post(
        "/api/v1/assets/",
        {"asset_tag": "X", "name": "x", "category": "DRONE", "purchase_date": "2025-01-01"},
        format="json",
    )
    assert resp.status_code == 400


def test_list_filters_and_search(client):
    make_asset("CAM-001", "Canon R5", "CAMERA")
    make_asset("CAM-002", "Sony A7", "CAMERA", status="MAINTENANCE")
    make_asset("LAP-001", "MacBook", "LAPTOP")

    assert client.get("/api/v1/assets/").json()["count"] == 3
    assert client.get("/api/v1/assets/?category=CAMERA").json()["count"] == 2
    assert client.get("/api/v1/assets/?status=MAINTENANCE").json()["count"] == 1
    assert client.get("/api/v1/assets/?search=sony").json()["count"] == 1
    assert client.get("/api/v1/assets/?search=LAP-").json()["count"] == 1


def test_list_is_paginated_at_20(client):
    for i in range(25):
        make_asset(f"SEN-{i:03d}", f"Sensor {i}", "SENSOR")
    body = client.get("/api/v1/assets/").json()
    assert body["count"] == 25
    assert len(body["results"]) == 20
    assert body["next"] is not None


def test_detail_current_holder(client):
    asset = make_asset()
    emp = make_employee()

    resp = client.get(f"/api/v1/assets/{asset.id}/")
    assert resp.status_code == 200
    assert resp.json()["current_holder"] is None

    CheckOut.objects.create(asset=asset, employee=emp, due_at=timezone.now() + timezone.timedelta(days=3))
    asset.status = "CHECKED_OUT"
    asset.save()

    body = client.get(f"/api/v1/assets/{asset.id}/").json()
    assert body["current_holder"] == {"employee_code": "E001", "full_name": "Asha Verma"}
