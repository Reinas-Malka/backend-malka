"""Pruebas de los endpoints de salud."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db, salud
from app.main import app
from app.salud import Estado

cliente = TestClient(app)


@pytest.fixture
def sin_base(monkeypatch: pytest.MonkeyPatch, config_limpia: None) -> Iterator[None]:
    """Sin DATABASE_URL ni DB_SECRET_NAME: como una Lambda mal configurada."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_SECRET_NAME", raising=False)
    db.obtener_motor.cache_clear()
    yield
    db.obtener_motor.cache_clear()


def test_health_responde_200_con_sus_claves() -> None:
    respuesta = cliente.get("/health")

    assert respuesta.status_code == 200
    assert set(respuesta.json()) == {"estado", "version", "entorno", "momento"}


def test_ready_responde_200_si_la_base_responde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(salud, "verificar_base_de_datos", lambda: Estado.OK)

    respuesta = cliente.get("/health/ready")

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "estado": "ok",
        "dependencias": {"base_de_datos": "ok"},
    }
    assert respuesta.headers["cache-control"] == "no-store"


def test_ready_responde_503_si_la_base_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(salud, "verificar_base_de_datos", lambda: Estado.ERROR)

    respuesta = cliente.get("/health/ready")

    assert respuesta.status_code == 503
    assert respuesta.json() == {
        "estado": "degradado",
        "dependencias": {"base_de_datos": "error"},
    }


@pytest.mark.usefixtures("sin_base")
def test_ready_sin_configuracion_responde_503_y_lo_loguea(logs: io.StringIO) -> None:
    respuesta = cliente.get("/health/ready")

    assert respuesta.status_code == 503
    assert respuesta.json()["dependencias"] == {"base_de_datos": "error"}

    lineas = [json.loads(linea) for linea in logs.getvalue().splitlines()]
    (aviso,) = [
        linea for linea in lineas if linea["mensaje"] == "dependencia no disponible"
    ]
    assert aviso["dependencia"] == "base_de_datos"
    # Solo el tipo de error: el mensaje podria incluir el host o el secreto.
    assert aviso["error"] == "RuntimeError"
