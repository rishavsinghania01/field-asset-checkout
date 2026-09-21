from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("assets", views.AssetViewSet, basename="asset")
router.register("checkouts", views.CheckOutViewSet, basename="checkout")

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path(
        "employees/<str:employee_code>/summary/",
        views.EmployeeSummaryView.as_view(),
        name="employee-summary",
    ),
    path("reports/overdue/", views.OverdueReportView.as_view(), name="overdue-report"),
]
urlpatterns += router.urls
