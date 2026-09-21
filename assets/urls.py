from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("assets", views.AssetViewSet, basename="asset")
router.register("checkouts", views.CheckOutViewSet, basename="checkout")

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
]
urlpatterns += router.urls
