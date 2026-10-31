"""Topes de espera de la base en los requests de negocio (revisión 29/10).

Sin topes, un FOR UPDATE o un lock de raza trabado espera hasta que API
Gateway corta a los 29 s, con la única conexión de la Lambda tomada. Con
lock_timeout el request falla enseguida con un 409 que dice que se reintente.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import db
from app.db import obtener_sesion, sesion_de_tenant
from app.errores import registrar_manejadores
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("con_motor_app")]

LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended('prueba-timeout', 0))")


@pytest.fixture(scope="module")
def tenant(motor_owner: Engine) -> Iterator[uuid.UUID]:
    t = uuid.uuid4()
    with motor_owner.begin() as c:
        crear_tenant(c, t)
    yield t
    with motor_owner.begin() as c:
        fijar_tenant(c, t)
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": t})


def test_la_sesion_de_negocio_tiene_topes_de_espera(tenant: uuid.UUID) -> None:
    with sesion_de_tenant(tenant) as sesion:
        lock = sesion.execute(text("SHOW lock_timeout")).scalar_one()
        consulta = sesion.execute(text("SHOW statement_timeout")).scalar_one()

    assert lock == f"{db.LOCK_TIMEOUT_MS // 1000}s"
    assert consulta == f"{db.STATEMENT_TIMEOUT_MS // 1000}s"
    # Menos que los 29 s de API Gateway: el error llega antes que el corte.
    assert db.STATEMENT_TIMEOUT_MS < 29_000


def test_los_topes_no_quedan_en_la_conexion_al_volver_al_pool(
    tenant: uuid.UUID, motor_app: Engine
) -> None:
    with sesion_de_tenant(tenant):
        pass

    with motor_app.connect() as c:
        assert c.execute(text("SHOW lock_timeout")).scalar_one() == "0"


@pytest.mark.usefixtures("cognito")
def test_un_lock_trabado_responde_409_enseguida(
    tenant: uuid.UUID, motor_owner: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 300)
    app = FastAPI()
    registrar_manejadores(app)

    @app.post("/operar")
    def operar(sesion: Session = Depends(obtener_sesion)) -> dict[str, str]:
        sesion.execute(LOCK)
        return {"ok": "si"}

    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": ["admin"]})
    tomado, soltar = threading.Event(), threading.Event()

    # Con otra conexion (la del duenio): el motor de la app tiene una sola, y
    # el request esperaria la conexion en vez del lock. El advisory lock vale
    # para toda la base, sin importar el rol.
    def retener() -> None:
        with motor_owner.connect() as c, c.begin():
            c.execute(LOCK)
            tomado.set()
            soltar.wait(10)

    hilo = threading.Thread(target=retener)
    hilo.start()
    assert tomado.wait(5), "el hilo no llego a tomar el lock"
    try:
        respuesta = TestClient(app).post(
            "/operar", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        soltar.set()
        hilo.join(10)

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "operacion_en_curso"
