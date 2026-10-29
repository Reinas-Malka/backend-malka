"""Limite de uso de la IA por criadero (#45, DoD).

Bedrock cobra por token: sin topes, un bucle en el frontend o un usuario
insistente genera una factura inesperada. Cada criadero tiene un tope de
llamadas por minuto (rafagas) y por dia (costo), y cada llamada un tope de
tokens de salida. Al pasarse, 429 con Retry-After.

Contra PostgreSQL real con RLS (migracion 0015): el conteo es por criadero.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.db import obtener_sesion, sesion_de_tenant
from app.errores import registrar_manejadores
from app.services.ia import limites
from app.services.ia.cliente import RespuestaIA, pedir_al_modelo
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = pytest.mark.usefixtures("con_motor_app")


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    for tenant in (a, b):
        with motor_owner.begin() as c:
            crear_tenant(c, tenant)
    yield a, b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM llamada_ia WHERE true"))
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_llamadas(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> Iterator[None]:
    yield
    for tenant in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM llamada_ia WHERE true"))


@pytest.fixture
def topes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Topes chicos para no hacer cientos de llamadas en el test."""
    monkeypatch.setenv("IA_LLAMADAS_POR_MINUTO", "3")
    monkeypatch.setenv("IA_LLAMADAS_POR_DIA", "5")


def reservar(tenant: uuid.UUID) -> uuid.UUID:
    with sesion_de_tenant(tenant) as sesion:
        return limites.reservar_llamada(sesion, "borrador")


def envejecer(motor_owner: Engine, tenant: uuid.UUID, intervalo: str) -> None:
    """Corre hacia atras las llamadas ya hechas (como duenio)."""
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text("UPDATE llamada_ia SET creado_en = creado_en - CAST(:i AS interval)"),
            {"i": intervalo},
        )


# --- topes ---


@pytest.mark.usefixtures("topes")
def test_una_rafaga_corta_en_el_tope_por_minuto(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = criaderos
    for _ in range(3):
        reservar(a)

    with pytest.raises(limites.LimiteExcedidoError) as error:
        reservar(a)

    assert error.value.code == "limite_ia_por_minuto"
    assert error.value.reintentar_en == 60


@pytest.mark.usefixtures("topes")
def test_pasado_el_minuto_vuelve_a_dejar_hasta_el_tope_diario(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    for _ in range(3):
        reservar(a)
    envejecer(motor_owner, a, "61 seconds")
    reservar(a)
    reservar(a)

    with pytest.raises(limites.LimiteExcedidoError) as error:
        reservar(a)

    assert error.value.code == "limite_ia_diario"
    # Hasta la medianoche de Argentina: entre 1 segundo y un dia.
    assert 0 < error.value.reintentar_en <= 86_400


@pytest.mark.usefixtures("topes")
def test_el_dia_siguiente_arranca_de_cero(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    for _ in range(3):
        reservar(a)
    envejecer(motor_owner, a, "61 seconds")
    reservar(a)
    reservar(a)
    envejecer(motor_owner, a, "1 day")

    reservar(a)


@pytest.mark.usefixtures("topes")
def test_el_tope_es_de_cada_criadero(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = criaderos
    for _ in range(3):
        reservar(a)

    # A ya llego al tope por minuto; B no consumio nada.
    reservar(b)


def test_sin_configurar_usa_los_topes_por_defecto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IA_LLAMADAS_POR_MINUTO", raising=False)
    monkeypatch.delenv("IA_LLAMADAS_POR_DIA", raising=False)

    assert limites.llamadas_por_minuto() == limites.LLAMADAS_POR_MINUTO
    assert limites.llamadas_por_dia() == limites.LLAMADAS_POR_DIA


@pytest.mark.parametrize("valor", ["cero", "0", "-3", ""])
def test_un_tope_mal_configurado_cae_al_valor_por_defecto(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    """Un error de configuracion nunca deja la IA sin limite."""
    monkeypatch.setenv("IA_LLAMADAS_POR_DIA", valor)

    assert limites.llamadas_por_dia() == limites.LLAMADAS_POR_DIA


# --- consumo y metrica ---


def test_registrar_el_consumo_guarda_los_tokens_y_emite_la_metrica(
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    capsys: pytest.CaptureFixture[str],
) -> None:
    a, _ = criaderos
    llamada = reservar(a)
    respuesta = RespuestaIA(
        texto="{}", tokens_entrada=1200, tokens_salida=300, latencia_ms=850
    )

    with sesion_de_tenant(a) as sesion:
        limites.registrar_consumo(sesion, llamada, respuesta)

    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        fila = c.execute(
            text("SELECT tokens_entrada, tokens_salida FROM llamada_ia WHERE id = :id"),
            {"id": llamada},
        ).one()
    assert (fila.tokens_entrada, fila.tokens_salida) == (1200, 300)

    # Embedded Metric Format: CloudWatch la convierte en metrica por criadero.
    [linea] = [
        json.loads(renglon)
        for renglon in capsys.readouterr().out.splitlines()
        if "_aws" in renglon
    ]
    [definicion] = linea["_aws"]["CloudWatchMetrics"]
    assert definicion["Namespace"] == "MalkaSuite/IA"
    assert definicion["Dimensions"] == [["tenant_id"]]
    assert linea["tenant_id"] == str(a)
    assert (linea["TokensEntrada"], linea["TokensSalida"], linea["Llamadas"]) == (
        1200,
        300,
        1,
    )


def test_cada_llamada_tiene_tope_de_tokens_de_salida() -> None:
    class ModeloQueAnota:
        def __init__(self) -> None:
            self.config: dict[str, Any] = {}

        def converse(self, **kwargs: Any) -> dict[str, Any]:
            self.config = kwargs["inferenceConfig"]
            return {
                "output": {"message": {"content": [{"text": "{}"}]}},
                "usage": {"inputTokens": 1, "outputTokens": 1},
            }

    modelo = ModeloQueAnota()
    pedir_al_modelo(modelo, "sistema", "mensaje")

    assert modelo.config["maxTokens"] == limites.MAX_TOKENS_SALIDA


# --- de punta a punta: 429 con Retry-After (DoD) ---


@pytest.fixture
def api() -> TestClient:
    """Un endpoint que consume una llamada, como el borrador del #40."""
    app = FastAPI()
    registrar_manejadores(app)

    @app.post("/ia")
    def usar_ia(sesion: Session = Depends(obtener_sesion)) -> dict[str, str]:
        return {"llamada": str(limites.reservar_llamada(sesion, "borrador"))}

    return TestClient(app)


@pytest.mark.usefixtures("cognito", "topes")
def test_pasado_el_tope_diario_responde_429_con_retry_after(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    token = emitir(**{"custom:tenant_id": str(a), "cognito:groups": ["ventas"]})
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(3):
        assert api.post("/ia", headers=headers).status_code == 200
    envejecer(motor_owner, a, "61 seconds")
    for _ in range(2):
        assert api.post("/ia", headers=headers).status_code == 200

    respuesta = api.post("/ia", headers=headers)

    assert respuesta.status_code == 429
    assert respuesta.json()["error"]["code"] == "limite_ia_diario"
    assert 0 < int(respuesta.headers["Retry-After"]) <= 86_400
    assert respuesta.json()["error"]["details"] == {"tope": 5}
