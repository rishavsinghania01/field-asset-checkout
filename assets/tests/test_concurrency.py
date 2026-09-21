"""
Rule 7: two check-out requests for the same asset at the same moment —
exactly one succeeds, the other gets 409.

These tests use transaction=True so each thread gets its own real database
connection and its own transaction; with the default (wrapped-in-a-
transaction) test isolation the threads would not contend on row locks.
"""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from assets.models import Asset, CheckOut

from .conftest import make_asset, make_employee

URL = "/api/v1/checkouts/"


def _run_parallel(fn, n):
    """Run fn(index) in n threads released at the same instant; return results."""
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = [None] * n

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results[i] = fn(i)
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(errors), errors
    return results


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_checkouts_of_one_asset(api_user):
    asset = make_asset("CAM-RACE")
    emp_a = make_employee("E-A", "Alpha")
    emp_b = make_employee("E-B", "Bravo")
    token = Token.objects.create(user=api_user).key
    due = (timezone.now() + timedelta(days=3)).isoformat()

    def attempt(i):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        code = emp_a.employee_code if i == 0 else emp_b.employee_code
        resp = client.post(
            URL, {"asset_tag": asset.asset_tag, "employee_code": code, "due_at": due}, format="json"
        )
        return resp.status_code

    codes = _run_parallel(attempt, 2)

    assert sorted(codes) == [201, 409], codes
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT


@pytest.mark.django_db(transaction=True)
def test_many_simultaneous_checkouts_of_one_asset(api_user):
    """Same idea with more contention: 8 requests, still exactly one winner."""
    asset = make_asset("LAP-RACE", category="LAPTOP")
    employees = [make_employee(f"E{i}", f"Person {i}") for i in range(8)]
    token = Token.objects.create(user=api_user).key
    due = (timezone.now() + timedelta(days=3)).isoformat()

    def attempt(i):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        resp = client.post(
            URL,
            {"asset_tag": asset.asset_tag, "employee_code": employees[i].employee_code, "due_at": due},
            format="json",
        )
        return resp.status_code

    codes = _run_parallel(attempt, 8)
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 7, codes
    assert CheckOut.objects.filter(asset=asset).count() == 1


@pytest.mark.django_db(transaction=True)
def test_simultaneous_checkouts_by_one_employee_respect_limit(api_user):
    """
    Rule 3 under concurrency: an employee with 2 open check-outs fires 3
    requests for 3 different assets at once. Only one may succeed, otherwise
    the employee would end up holding 4.
    """
    emp = make_employee("E-LIM", "Limit Tester")
    held = [make_asset(f"HELD-{i}") for i in range(2)]
    for a in held:
        CheckOut.objects.create(asset=a, employee=emp, due_at=timezone.now() + timedelta(days=2))
        a.status = Asset.Status.CHECKED_OUT
        a.save()
    targets = [make_asset(f"NEW-{i}") for i in range(3)]
    token = Token.objects.create(user=api_user).key
    due = (timezone.now() + timedelta(days=3)).isoformat()

    def attempt(i):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        resp = client.post(
            URL,
            {"asset_tag": targets[i].asset_tag, "employee_code": emp.employee_code, "due_at": due},
            format="json",
        )
        return resp.status_code

    codes = _run_parallel(attempt, 3)
    assert codes.count(201) == 1, codes
    assert CheckOut.objects.filter(employee=emp, returned_at__isnull=True).count() == 3
