"""DoD del #24: todos los errores de la API responden con la misma forma."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

CLAVES_DEL_ERROR = {"code", "message", "details", "request_id"}


def assert_forma_unica(respuesta: Any, status: int, code: str) -> dict[str, Any]:
    """Verifica la forma comun y que el request_id coincida con el header."""
    assert respuesta.status_code == status
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"error"}
    error: dict[str, Any] = cuerpo["error"]
    assert set(error) == CLAVES_DEL_ERROR
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    assert error["request_id"] == respuesta.headers["x-request-id"]
    return error


@pytest.mark.parametrize(
    ("metodo", "ruta", "status", "code"),
    [
        ("GET", "/conflicto", 409, "tanda_cerrada"),
        ("GET", "/regla", 422, "validacion"),
        ("POST", "/pedidos", 422, "validacion"),
        ("GET", "/explota", 500, "error_interno"),
        ("GET", "/no-existe", 404, "no_encontrado"),
        ("DELETE", "/regla", 405, "metodo_no_permitido"),
        ("GET", "/tandas/no-es-un-numero", 422, "validacion"),
    ],
)
def test_todos_los_errores_tienen_la_misma_forma(
    cliente: TestClient, metodo: str, ruta: str, status: int, code: str
) -> None:
    respuesta = cliente.request(metodo, ruta, json={})
    assert_forma_unica(respuesta, status, code)


def test_el_error_de_dominio_conserva_sus_detalles(cliente: TestClient) -> None:
    error = assert_forma_unica(cliente.get("/conflicto"), 409, "tanda_cerrada")
    assert error["details"] == {"tanda_id": 7}


def test_la_validacion_indica_el_campo_sin_devolver_el_dato(
    cliente: TestClient,
) -> None:
    respuesta = cliente.post(
        "/pedidos", json={"cantidad": "muchas", "cuit": 20304050607}
    )

    error = assert_forma_unica(respuesta, 422, "validacion")
    campos = {detalle["campo"] for detalle in error["details"]["campos"]}
    assert campos == {"body.cantidad", "body.cuit"}
    assert "20304050607" not in respuesta.text
    assert "muchas" not in respuesta.text


def test_el_error_interno_no_expone_detalles(cliente: TestClient) -> None:
    respuesta = cliente.get("/explota")

    assert_forma_unica(respuesta, 500, "error_interno")
    assert "detalle interno" not in respuesta.text


def test_la_app_real_usa_la_forma_unica() -> None:
    respuesta = TestClient(app).get("/ruta-inexistente")

    assert_forma_unica(respuesta, 404, "no_encontrado")
