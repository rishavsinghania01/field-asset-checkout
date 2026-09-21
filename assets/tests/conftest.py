from datetime import date

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from assets.models import Asset, Employee


@pytest.fixture
def api_user(db):
    return get_user_model().objects.create_user(username="tester", password="pw")


@pytest.fixture
def client(api_user):
    token, _ = Token.objects.get_or_create(user=api_user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


@pytest.fixture
def anon_client():
    return APIClient()


def make_asset(tag="CAM-001", name="Field camera", category="CAMERA", status="AVAILABLE"):
    return Asset.objects.create(
        asset_tag=tag,
        name=name,
        category=category,
        status=status,
        purchase_date=date(2024, 1, 15),
    )


def make_employee(code="E001", name="Asha Verma", active=True):
    return Employee.objects.create(
        employee_code=code,
        full_name=name,
        email=f"{code.lower()}@example.com",
        is_active=active,
    )


@pytest.fixture
def asset(db):
    return make_asset()


@pytest.fixture
def employee(db):
    return make_employee()
