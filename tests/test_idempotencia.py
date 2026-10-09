"""Idempotencia (#25): las partes que no necesitan base de datos."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.idempotencia import huella
from app.main import app
from tests.conftest import emitir

URL = "/api/v1/clientes"

PEDIDO = {"nombre": "Apicola del Sur", "pais": "AR", "cantidad": 3}


def cuerpo(datos: object, **opciones: object) -> bytes:
    return json.dumps(datos, **opciones).encode()  # type: ignore[arg-type]


# --- huella del pedido ---


def test_el_mismo_json_con_otro_formato_da_la_misma_huella() -> None:
    compacto = cuerpo(PEDIDO, separators=(",", ":"))
    con_espacios = cuerpo(dict(reversed(PEDIDO.items())), indent=2)

    assert huella("POST", URL, compacto) == huella("POST", URL, con_espacios)


def test_otro_cuerpo_da_otra_huella() -> None:
    otro = {**PEDIDO, "cantidad": 4}

    assert huella("POST", URL, cuerpo(PEDIDO)) != huella("POST", URL, cuerpo(otro))


def test_otra_ruta_u_otro_metodo_dan_otra_huella() -> None:
    base = huella("POST", URL, cuerpo(PEDIDO))

    assert huella("POST", "/api/v1/pedidos", cuerpo(PEDIDO)) != base
    assert huella("PUT", URL, cuerpo(PEDIDO)) != base


def test_un_cuerpo_que_no_es_json_tambien_tiene_huella() -> None:
    assert huella("POST", URL, b"no es json") != huella("POST", URL, b"otro texto")
    assert len(huella("POST", URL, b"")) == 64


# --- validacion del header ---


@pytest.mark.usefixtures("cognito")
@pytest.mark.parametrize(
    "clave",
    # Los no ASCII no se prueban: el cliente HTTP no los deja mandar.
    ["con espacios", "x" * 256, ""],
)
def test_una_clave_mal_formada_se_rechaza_sin_abrir_la_base(clave: str) -> None:
    # Sin DATABASE_URL ni base de prueba: si la dependencia abriera una
    # conexion, esto seria un 500 y no un 422.
    token = emitir(**{"cognito:groups": ["ventas"]})
    respuesta = TestClient(app).post(
        URL,
        json={},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": clave},
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "idempotency_key_invalida"
