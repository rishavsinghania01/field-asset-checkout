from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from assets import queries
from assets.models import Asset, CheckOut

from .conftest import make_asset, make_employee

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def make_checkout(asset, employee, *, out_days_ago, due_in_days, returned_days_ago=None):
    """
    Create a check-out relative to NOW. checked_out_at is auto_now_add so it is
    set with a second UPDATE, which is also what the seed command has to do.
    """
    co = CheckOut.objects.create(asset=asset, employee=employee, due_at=NOW + timedelta(days=due_in_days))
    CheckOut.objects.filter(pk=co.pk).update(
        checked_out_at=NOW - timedelta(days=out_days_ago),
        returned_at=None if returned_days_ago is None else NOW - timedelta(days=returned_days_ago),
    )
    co.refresh_from_db()
    if co.returned_at is None:
        asset.status = Asset.Status.CHECKED_OUT
        asset.save()
    return co


@pytest.fixture
def frozen_now():
    with mock.patch("assets.queries.timezone.now", return_value=NOW):
        yield NOW


# ---------------------------------------------------------------- summary


def test_summary_four_numbers(client, frozen_now):
    emp = make_employee("E100", "Summary Person")
    other = make_employee("E200", "Someone Else")
    a = [make_asset(f"S-{i}", name=f"Asset {i}") for i in range(6)]

    # Returned: held 4 days, 2 days and 6 days -> mean 4.0
    make_checkout(a[0], emp, out_days_ago=30, due_in_days=-20, returned_days_ago=26)
    make_checkout(a[1], emp, out_days_ago=20, due_in_days=-10, returned_days_ago=18)
    make_checkout(a[2], emp, out_days_ago=10, due_in_days=-5, returned_days_ago=4)
    # Open, not overdue
    make_checkout(a[3], emp, out_days_ago=1, due_in_days=5)
    # Open, overdue
    make_checkout(a[4], emp, out_days_ago=9, due_in_days=-2)
    # Someone else's overdue item must not leak in
    make_checkout(a[5], other, out_days_ago=9, due_in_days=-2)

    resp = client.get("/api/v1/employees/E100/summary/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["lifetime_checkouts"] == 5
    assert body["currently_held"] == 2
    assert body["currently_overdue"] == 1
    assert body["mean_hold_days"] == pytest.approx(4.0)
    assert body["employee_code"] == "E100"


def test_summary_no_checkouts(client):
    make_employee("E300", "Newbie")
    body = client.get("/api/v1/employees/E300/summary/").json()
    assert body == {
        "employee_code": "E300",
        "full_name": "Newbie",
        "is_active": True,
        "lifetime_checkouts": 0,
        "currently_held": 0,
        "currently_overdue": 0,
        "mean_hold_days": None,
    }


def test_summary_unknown_employee_404(client):
    assert client.get("/api/v1/employees/NOPE/summary/").status_code == 404


def test_summary_is_one_query(client, frozen_now):
    emp = make_employee("E400", "Q Counter")
    for i in range(3):
        make_checkout(make_asset(f"Q-{i}"), emp, out_days_ago=3, due_in_days=2)

    # Warm the auth lookup so we only count the summary itself.
    client.get("/api/v1/employees/E400/summary/")
    with CaptureQueriesContext(connection) as ctx:
        client.get("/api/v1/employees/E400/summary/")
    sql = [q["sql"] for q in ctx.captured_queries if "assets_" in q["sql"]]
    assert len(sql) == 1, sql


# ---------------------------------------------------------------- overdue


def test_overdue_boundary_due_exactly_now(frozen_now):
    emp = make_employee()
    exactly_now = make_checkout(make_asset("B-NOW"), emp, out_days_ago=3, due_in_days=0)
    one_second_ago = CheckOut.objects.create(
        asset=make_asset("B-PAST"), employee=emp, due_at=NOW - timedelta(seconds=1)
    )
    future = make_checkout(make_asset("B-FUT"), emp, out_days_ago=1, due_in_days=1)
    returned_late = make_checkout(
        make_asset("B-RET"), emp, out_days_ago=10, due_in_days=-5, returned_days_ago=1
    )

    ids = list(queries.overdue_checkouts(now=NOW).values_list("id", flat=True))
    assert one_second_ago.id in ids
    assert exactly_now.id not in ids  # due now is not yet overdue
    assert future.id not in ids
    assert returned_late.id not in ids  # returned rows never appear

    # One tick later the "due exactly now" item is overdue.
    ids_later = list(queries.overdue_checkouts(now=NOW + timedelta(seconds=1)).values_list("id", flat=True))
    assert exactly_now.id in ids_later


def test_overdue_report_rows_and_ordering(client, frozen_now):
    emp = make_employee("E500", "Late Larry")
    a1 = make_asset("R-1", name="Drone cam")
    a2 = make_asset("R-2", name="Field laptop", category="LAPTOP")
    a3 = make_asset("R-3", name="On time sensor", category="SENSOR")
    make_checkout(a1, emp, out_days_ago=10, due_in_days=-3)  # 3 days overdue
    make_checkout(a2, emp, out_days_ago=20, due_in_days=-12)  # 12 days overdue
    make_checkout(a3, emp, out_days_ago=1, due_in_days=4)  # not overdue

    resp = client.get("/api/v1/reports/overdue/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    rows = body["results"]
    assert [r["asset_tag"] for r in rows] == ["R-2", "R-1"]  # most overdue first
    assert rows[0] == {
        "checkout_id": rows[0]["checkout_id"],
        "asset_name": "Field laptop",
        "asset_tag": "R-2",
        "employee_code": "E500",
        "employee_name": "Late Larry",
        "due_at": (NOW - timedelta(days=12)).isoformat().replace("+00:00", "Z"),
        "days_overdue": 12,
    }
    assert rows[1]["days_overdue"] == 3


def test_overdue_report_does_not_query_per_row(client, frozen_now):
    emps = [make_employee(f"N-{i}", f"Person {i}") for i in range(5)]
    for i in range(15):
        make_checkout(make_asset(f"N-{i}"), emps[i % 5], out_days_ago=10, due_in_days=-(i + 1))

    client.get("/api/v1/reports/overdue/")  # warm auth
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get("/api/v1/reports/overdue/")
    assert resp.json()["count"] == 15
    sql = [q["sql"] for q in ctx.captured_queries if "assets_" in q["sql"]]
    # One COUNT for pagination + one SELECT with the joins. Never 1 + N.
    assert len(sql) == 2, sql
