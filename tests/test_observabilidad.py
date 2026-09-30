"""request_id y logs JSON (#24)."""

from __future__ import annotations

import io
import json
import re
from typing import Any

from fastapi.testclient import TestClient

from app.main import handler
from app.observabilidad import REDACTADO, sanear


def lineas_de_log(logs: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in logs.getvalue().splitlines() if linea]


def acceso(logs: io.StringIO) -> dict[str, Any]:
    """La linea de acceso: la unica con mensaje "request"."""
    (linea,) = [linea for linea in lineas_de_log(logs) if linea["mensaje"] == "request"]
    return linea


def test_se_usa_el_request_id_del_header(cliente: TestClient) -> None:
    respuesta = cliente.get("/tandas/1", headers={"x-request-id": "abc-123"})

    assert respuesta.headers["x-request-id"] == "abc-123"


def test_sin_header_se_genera_uno(cliente: TestClient) -> None:
    respuesta = cliente.get("/tandas/1")

    assert re.fullmatch(r"[0-9a-f]{32}", respuesta.headers["x-request-id"])


def test_un_header_invalido_se_reemplaza(cliente: TestClient) -> None:
    invalido = "con espacios; y\ttab"
    respuesta = cliente.get("/tandas/1", headers={"x-request-id": invalido})

    assert respuesta.headers["x-request-id"] != invalido


def test_en_lambda_se_usa_el_id_de_api_gateway() -> None:
    evento = {
        "version": "2.0",
        "routeKey": "GET /health",
        "rawPath": "/health",
        "rawQueryString": "",
        "headers": {"host": "api.example.com"},
        "requestContext": {
            "requestId": "JKJaXmPLvHcESHA=",
            "http": {
                "method": "GET",
                "path": "/health",
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest",
            },
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }

    respuesta = handler(evento, None)

    assert respuesta["statusCode"] == 200
    assert respuesta["headers"]["x-request-id"] == "JKJaXmPLvHcESHA="


def test_cada_request_deja_una_linea_json_con_sus_campos(
    cliente: TestClient, logs: io.StringIO
) -> None:
    cliente.get("/tandas/42", headers={"x-request-id": "req-1"})

    linea = acceso(logs)
    assert linea["request_id"] == "req-1"
    assert linea["route"] == "/tandas/{tanda_id}"
    assert linea["status"] == 200
    assert isinstance(linea["latency_ms"], float)
    assert linea["tenant_id"] is None
    assert linea["user_id"] is None


def test_el_error_interno_se_loguea_con_su_traza(
    cliente: TestClient, logs: io.StringIO
) -> None:
    cliente.get("/explota", headers={"x-request-id": "req-500"})

    (error,) = [linea for linea in lineas_de_log(logs) if linea["nivel"] == "ERROR"]
    assert error["request_id"] == "req-500"
    assert error["error"] == "RuntimeError"
    assert acceso(logs)["status"] == 500


def test_nunca_se_loguean_tokens_ni_datos_fiscales(
    cliente: TestClient, logs: io.StringIO
) -> None:
    cliente.post(
        "/pedidos",
        headers={"Authorization": "Bearer token-super-secreto"},
        json={"cantidad": 1, "cuit": "20-30405060-7"},
    )

    texto = logs.getvalue()
    assert "token-super-secreto" not in texto
    assert "20-30405060-7" not in texto


def test_sanear_tacha_claves_sensibles_a_cualquier_nivel() -> None:
    datos = {
        "tanda": 3,
        "Authorization": "Bearer x",
        "cliente": {"cuit": "20-1", "nombre": "Malka", "contactos": [{"dni": "1"}]},
    }

    assert sanear(datos) == {
        "tanda": 3,
        "Authorization": REDACTADO,
        "cliente": {
            "cuit": REDACTADO,
            "nombre": "Malka",
            "contactos": [{"dni": REDACTADO}],
        },
    }
