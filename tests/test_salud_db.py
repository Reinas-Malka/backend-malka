"""/health/ready contra un PostgreSQL real (#23)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, pool, text

from app import db, salud
from app.main import app
from app.salud import Estado
from tests.conftest import BaseDePrueba

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("con_motor_app")]


def test_ready_con_la_base_disponible_responde_200(
    base_de_prueba: BaseDePrueba,
) -> None:
    respuesta = TestClient(app).get("/health/ready")

    assert respuesta.status_code == 200
    assert respuesta.json()["dependencias"] == {"base_de_datos": "ok"}
    # Nunca el endpoint, el usuario ni la clave de la base.
    url = base_de_prueba.url_app
    for dato in (url.host, url.username, url.password, str(url.port)):
        assert str(dato) not in respuesta.text


def test_una_consulta_lenta_se_corta_con_el_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(salud, "CONSULTA", text("SELECT pg_sleep(5)"))
    monkeypatch.setattr(salud, "TIMEOUT_CONSULTA_MS", 200)

    inicio = time.monotonic()
    estado = salud.verificar_base_de_datos()

    assert estado == Estado.ERROR
    assert time.monotonic() - inicio < 2


def test_el_timeout_no_queda_pegado_en_la_conexion(motor_app: Engine) -> None:
    """SET LOCAL: despues del chequeo, la conexion del pool vuelve sin limite."""
    assert salud.verificar_base_de_datos() == Estado.OK

    with motor_app.connect() as conexion:
        assert conexion.execute(text("SHOW statement_timeout")).scalar() == "0"


@pytest.fixture
def base_inalcanzable(
    monkeypatch: pytest.MonkeyPatch, con_motor_app: None
) -> Iterator[None]:
    """Un puerto donde no escucha nadie: la conexion se rechaza.

    Pide con_motor_app para correr despues y pisar su motor, no al reves.
    """
    motor = create_engine(
        "postgresql+psycopg://nadie:nada@127.0.0.1:1/malka",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": db.CONNECT_TIMEOUT_S},
    )
    monkeypatch.setattr(db, "obtener_motor", lambda: motor)
    yield
    motor.dispose()


@pytest.mark.usefixtures("base_inalcanzable")
def test_sin_conexion_responde_503_sin_detalles() -> None:
    respuesta = TestClient(app).get("/health/ready")

    assert respuesta.status_code == 503
    assert respuesta.json() == {
        "estado": "degradado",
        "dependencias": {"base_de_datos": "error"},
    }
    assert "127.0.0.1" not in respuesta.text
