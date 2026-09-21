from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import Asset, CheckOut, Employee


class CurrentHolderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ("employee_code", "full_name")


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = (
            "id",
            "asset_tag",
            "name",
            "category",
            "status",
            "purchase_date",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class AssetDetailSerializer(AssetSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta(AssetSerializer.Meta):
        fields = AssetSerializer.Meta.fields + ("current_holder",)

    def get_current_holder(self, obj):
        # `open_checkouts` is populated by a Prefetch on the view queryset, so
        # this does not hit the database again per asset.
        open_checkouts = getattr(obj, "open_checkouts", None)
        if open_checkouts is None:
            open_checkouts = list(obj.checkouts.filter(returned_at__isnull=True).select_related("employee"))
        if not open_checkouts:
            return None
        return CurrentHolderSerializer(open_checkouts[0].employee).data


MAX_LOAN_DAYS = 30


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = CheckOut
        fields = (
            "id",
            "asset",
            "asset_tag",
            "employee",
            "employee_code",
            "checked_out_at",
            "due_at",
            "returned_at",
            "condition_note",
        )
        read_only_fields = fields


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("due_at must be in the future.")
        if value > now + timedelta(days=MAX_LOAN_DAYS):
            raise serializers.ValidationError(f"due_at must be no more than {MAX_LOAN_DAYS} days from now.")
        return value


class CheckOutReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(allow_blank=True, required=False, default="")
    needs_maintenance = serializers.BooleanField(required=False, default=False)


class EmployeeSummarySerializer(serializers.Serializer):
    employee_code = serializers.CharField()
    full_name = serializers.CharField()
    is_active = serializers.BooleanField()
    lifetime_checkouts = serializers.IntegerField()
    currently_held = serializers.IntegerField()
    currently_overdue = serializers.IntegerField()
    mean_hold_days = serializers.FloatField(allow_null=True)


class OverdueRowSerializer(serializers.Serializer):
    checkout_id = serializers.IntegerField(source="id")
    asset_name = serializers.CharField(source="asset.name")
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")
    employee_name = serializers.CharField(source="employee.full_name")
    due_at = serializers.DateTimeField()
    days_overdue = serializers.SerializerMethodField()

    def get_days_overdue(self, obj):
        # `overdue_for` is annotated by the query; whole days, floor.
        return obj.overdue_for.days
