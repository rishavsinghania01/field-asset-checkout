from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class Conflict(APIException):
    """409 — the request is valid but conflicts with the current state."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


def exception_handler(exc, context):
    """
    Thin wrapper around DRF's handler so every error body has the same shape:
    {"detail": "...", "code": "..."}.
    """
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict):
        code = getattr(exc, "default_code", None)
        if "detail" in response.data and code and "code" not in response.data:
            response.data["code"] = code
    return response
