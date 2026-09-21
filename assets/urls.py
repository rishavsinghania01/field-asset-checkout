from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("assets", views.AssetViewSet, basename="asset")

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
]
urlpatterns += router.urls
