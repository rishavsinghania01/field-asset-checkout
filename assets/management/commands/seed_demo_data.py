"""
python manage.py seed_demo_data

Populates a database with a known demo dataset and prints an API token.

Safe to re-run: assets and employees are upserted by their natural keys,
and the check-out / notice history is rebuilt from scratch each time so the
dataset always looks the same afterwards (any check-outs created through
the API since the last seed are discarded — this is a reset, not a merge).
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from assets.models import Asset, CheckOut, Employee, OverdueNotice

API_USERNAME = "reviewer"

ASSETS = [
    # tag, name, category, purchase_date
    ("CAM-001", "Canon EOS R5", "CAMERA", date(2024, 2, 12)),
    ("CAM-002", "Sony A7 IV", "CAMERA", date(2024, 6, 30)),
    ("CAM-003", "DJI Osmo Pocket 3", "CAMERA", date(2025, 1, 8)),
    ("LAP-001", "ThinkPad X1 Carbon", "LAPTOP", date(2023, 11, 20)),
    ("LAP-002", "MacBook Pro 14", "LAPTOP", date(2024, 9, 5)),
    ("LAP-003", "Dell Latitude 5450", "LAPTOP", date(2025, 3, 17)),
    ("SEN-001", "Trimble R12i GNSS receiver", "SENSOR", date(2023, 7, 1)),
    ("SEN-002", "FLIR E8 thermal imager", "SENSOR", date(2024, 4, 22)),
    ("SEN-003", "Leica Disto X4", "SENSOR", date(2025, 5, 3)),
    ("VEH-001", "Mahindra Bolero (HR26 DK 4410)", "VEHICLE", date(2022, 8, 15)),
    ("VEH-002", "Tata Ace (HR55 AB 1207)", "VEHICLE", date(2023, 3, 9)),
    ("VEH-003", "Toyota Innova (DL3C AQ 2210)", "VEHICLE", date(2024, 12, 1)),
]

EMPLOYEES = [
    # code, full name, email, active
    ("EMP-001", "Asha Verma", "asha.verma@example.com", True),
    ("EMP-002", "Rohan Mehta", "rohan.mehta@example.com", True),
    ("EMP-003", "Priya Nair", "priya.nair@example.com", True),
    ("EMP-004", "Vikram Sethi", "vikram.sethi@example.com", True),
    ("EMP-005", "Neha Kapoor", "neha.kapoor@example.com", False),  # left the company
]

# Check-outs relative to "now", in days. returned=None means still open.
# (asset, employee, checked_out_days_ago, due_in_days_from_checkout, returned_days_after_checkout, note)
CHECKOUTS = [
    # --- currently overdue (open, due in the past)
    ("CAM-001", "EMP-001", 20, 7, None, ""),
    ("LAP-001", "EMP-002", 45, 30, None, ""),
    ("VEH-001", "EMP-002", 12, 5, None, ""),
    # --- open, not yet due
    ("SEN-001", "EMP-003", 2, 14, None, ""),
    ("CAM-002", "EMP-001", 1, 10, None, ""),
    # --- returned on time
    ("LAP-002", "EMP-003", 30, 10, 8, "Fine, charger included"),
    ("SEN-002", "EMP-004", 25, 7, 7, ""),
    ("CAM-003", "EMP-001", 60, 14, 3, "Lens cap missing, replaced"),
    # --- returned late
    ("VEH-002", "EMP-004", 40, 5, 12, "Returned a week late; rear tyre needs inspection"),
    ("LAP-003", "EMP-002", 90, 30, 41, ""),
]

# Assets that are in the workshop regardless of check-out history.
MAINTENANCE = {"SEN-003"}


class Command(BaseCommand):
    help = "Seed the database with demo assets, employees, check-outs and an API token."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()

        assets = {}
        for tag, name, category, purchased in ASSETS:
            asset, _ = Asset.objects.update_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "purchase_date": purchased,
                    "status": Asset.Status.AVAILABLE,
                },
            )
            assets[tag] = asset

        employees = {}
        for code, name, email, active in EMPLOYEES:
            emp, _ = Employee.objects.update_or_create(
                employee_code=code,
                defaults={"full_name": name, "email": email, "is_active": active},
            )
            employees[code] = emp

        # Rebuild history so re-runs converge on the same state.
        OverdueNotice.objects.all().delete()
        CheckOut.objects.all().delete()

        for tag, code, out_days_ago, due_in, returned_after, note in CHECKOUTS:
            checked_out_at = now - timedelta(days=out_days_ago)
            due_at = checked_out_at + timedelta(days=due_in)
            returned_at = None if returned_after is None else checked_out_at + timedelta(days=returned_after)

            checkout = CheckOut.objects.create(
                asset=assets[tag],
                employee=employees[code],
                due_at=due_at,
                returned_at=returned_at,
                condition_note=note,
            )
            # checked_out_at is auto_now_add, so it has to be backdated separately.
            CheckOut.objects.filter(pk=checkout.pk).update(checked_out_at=checked_out_at)

            if returned_at is None:
                assets[tag].status = Asset.Status.CHECKED_OUT
                assets[tag].save(update_fields=["status", "updated_at"])

        for tag in MAINTENANCE:
            assets[tag].status = Asset.Status.MAINTENANCE
            assets[tag].save(update_fields=["status", "updated_at"])

        user, _ = get_user_model().objects.get_or_create(
            username=API_USERNAME, defaults={"is_staff": True}
        )
        token, _ = Token.objects.get_or_create(user=user)

        open_count = CheckOut.objects.filter(returned_at__isnull=True).count()
        overdue_count = CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(assets)} assets, {len(employees)} employees "
                f"({sum(1 for e in employees.values() if not e.is_active)} inactive), "
                f"{len(CHECKOUTS)} check-outs ({open_count} open, {overdue_count} overdue)."
            )
        )
        self.stdout.write(f"API token for user '{API_USERNAME}': {token.key}")
        self.stdout.write("Use it as:  Authorization: Token <key>")
