from django.db import DatabaseError, connection
from django.db.models import Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import queries, services
from .models import Asset, CheckOut
from .serializers import (
    AssetDetailSerializer,
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutReturnSerializer,
    CheckOutSerializer,
    EmployeeSummarySerializer,
    OverdueRowSerializer,
)


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


class CheckOutViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    POST /checkouts/               body {asset_tag, employee_code, due_at} -> 201
    POST /checkouts/{id}/return/   body {condition_note, needs_maintenance} -> 200
    GET  /checkouts/ and /checkouts/{id}/ are read-only conveniences.
    """

    queryset = CheckOut.objects.select_related("asset", "employee")
    serializer_class = CheckOutSerializer
    filterset_fields = ("employee__employee_code", "asset__asset_tag")
    ordering = ("-checked_out_at",)

    def create(self, request, *args, **kwargs):
        payload = CheckOutCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        checkout = services.check_out_asset(**payload.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return")
    def return_(self, request, pk=None):
        payload = CheckOutReturnSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        checkout = services.return_checkout(checkout_id=int(pk), **payload.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_200_OK)


class EmployeeSummaryView(APIView):
    """
    GET /employees/{employee_code}/summary/

    Four numbers computed by one aggregate query (see queries.employee_summary).
    """

    def get(self, request, employee_code):
        employee = queries.employee_summary(employee_code)
        if employee is None:
            raise NotFound(f"Employee {employee_code!r} not found.")
        mean_hold = employee.mean_hold
        data = {
            "employee_code": employee.employee_code,
            "full_name": employee.full_name,
            "is_active": employee.is_active,
            "lifetime_checkouts": employee.lifetime_checkouts,
            "currently_held": employee.currently_held,
            "currently_overdue": employee.currently_overdue,
            "mean_hold_days": (
                round(mean_hold.total_seconds() / 86400, 2) if mean_hold is not None else None
            ),
        }
        return Response(EmployeeSummarySerializer(data).data)


class OverdueReportView(ListAPIView):
    """
    GET /reports/overdue/

    Open check-outs past due, most overdue first. One query for the page
    (plus the pagination count): asset and employee come via select_related,
    days_overdue via an annotation.
    """

    serializer_class = OverdueRowSerializer
    filter_backends = ()

    def get_queryset(self):
        return queries.overdue_checkouts()


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
