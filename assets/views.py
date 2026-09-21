from django.db import DatabaseError, connection
from django.db.models import Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, CheckOut
from .serializers import AssetDetailSerializer, AssetSerializer


class AssetViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST /assets/            create
    GET  /assets/            list, ?status= ?category= ?search=
    GET  /assets/{id}/       retrieve with current_holder
    """

    queryset = Asset.objects.all()
    filterset_fields = ("status", "category")
    search_fields = ("name", "asset_tag")
    ordering_fields = ("id", "asset_tag", "name", "purchase_date", "created_at")
    ordering = ("id",)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AssetDetailSerializer
        return AssetSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch(
                    "checkouts",
                    queryset=CheckOut.objects.filter(returned_at__isnull=True)
                    .select_related("employee")
                    .order_by("-checked_out_at"),
                    to_attr="open_checkouts",
                )
            )
        return qs


class HealthView(APIView):
    """
    GET /health/  — unauthenticated. 200 when the database answers, 503 otherwise.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            db_ok = True
        except DatabaseError:
            db_ok = False

        body = {"status": "ok" if db_ok else "degraded", "database": db_ok}
        code = status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(body, status=code)
