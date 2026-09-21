from django.contrib import admin

from .models import Asset, CheckOut, Employee, OverdueNotice


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("asset_tag", "name", "category", "status", "purchase_date")
    list_filter = ("category", "status")
    search_fields = ("asset_tag", "name")


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "full_name", "email", "is_active")
    list_filter = ("is_active",)
    search_fields = ("employee_code", "full_name", "email")


@admin.register(CheckOut)
class CheckOutAdmin(admin.ModelAdmin):
    list_display = ("id", "asset", "employee", "checked_out_at", "due_at", "returned_at")
    list_filter = ("returned_at",)
    raw_id_fields = ("asset", "employee")


@admin.register(OverdueNotice)
class OverdueNoticeAdmin(admin.ModelAdmin):
    list_display = ("id", "checkout", "notice_date", "created_at")
    raw_id_fields = ("checkout",)
