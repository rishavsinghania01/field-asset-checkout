from rest_framework import serializers

from .models import Asset, Employee


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
            open_checkouts = list(
                obj.checkouts.filter(returned_at__isnull=True).select_related("employee")
            )
        if not open_checkouts:
            return None
        return CurrentHolderSerializer(open_checkouts[0].employee).data
