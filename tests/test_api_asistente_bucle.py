"""El bucle del asistente: topes de rondas y de tiempo, limite de IA,
Bedrock caido, consumo anotado, logs sin contenido y transaccion de solo
lectura. Contra PostgreSQL real, con el modelo falso.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import InternalError

from app.auth import Identidad
from app.main import app
from app.routers import asistente as router_asistente
from app.schemas.asistente import MensajeAsistente
from app.services.ia.asistente import conversacion, herramientas
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.asistente.herramientas import Herramienta
from app.services.ia.asistente.lectura import EntradaDisponibilidad
from tests.asistente_datos import (
    Criadero,
    ModeloCaido,
    ModeloGuionado,
    cargar_criadero,
    como,
    conversar,
    limpiar,
    pide,
    responde,
)
from tests.conftest import fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

DISPONIBILIDAD = ("ver_disponibilidad", {"raza": "Buckfast"})


@pytest.fixture
def criadero(motor_owner: Engine) -> Iterator[Criadero]:
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        datos = cargar_criadero(c, tenant)
    yield datos
    limpiar(motor_owner, tenant)


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def llamadas_ia(motor: Engine, tenant: uuid.UUID) -> list[Any]:
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        return list(
            c.execute(
                text("SELECT motivo, tokens_entrada, tokens_salida FROM llamada_ia")
            )
        )


# --- topes ---


def test_pasadas_las_rondas_responde_con_lo_que_hay_y_un_aviso(
    api: TestClient, criadero: Criadero
) -> None:
    rondas = conversacion.MAX_RONDAS_DE_HERRAMIENTAS
    modelo = ModeloGuionado(
        [pide(*DISPONIBILIDAD, f"u{n}") for n in range(rondas + 1)]
        + [responde("nunca llega")]
    )

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 200
    # Una llamada inicial mas una por ronda de herramientas; la ultima pide
    # otra herramienta y ahi se corta.
    assert len(modelo.pedidos) == rondas + 1
    assert respuesta.json()["texto"] == conversacion.AVISO_RONDAS
    assert len(modelo.guion) == 1


def test_sin_tiempo_para_otra_llamada_corta_con_aviso(
    criadero: Criadero, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cada lectura del reloj avanza 6 s: despues de la primera llamada ya no
    # entra otra de hasta TIEMPO_MAXIMO_LLAMADA_S en el presupuesto.
    instantes = iter(range(0, 1000, 6))
    modelo = ModeloGuionado(
        [
            {
                **pide(*DISPONIBILIDAD),
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"text": "Ya miro."},
                            {
                                "toolUse": {
                                    "toolUseId": "u1",
                                    "name": "ver_disponibilidad",
                                    "input": {},
                                }
                            },
                        ],
                    }
                },
            },
            responde("nunca llega"),
        ]
    )

    respuesta = conversacion.conversar(
        [MensajeAsistente(rol="usuario", texto="¿Cuántas hay?")],
        modelo=modelo,
        identidad=Identidad("usuario-1", criadero.tenant, frozenset({"lectura"})),
        reloj=lambda: float(next(instantes)),
    )

    assert len(modelo.pedidos) == 1
    assert respuesta.texto == f"Ya miro.\n\n{conversacion.AVISO_TIEMPO}"
    # La herramienta pedida se corrio igual: la consulta queda a la vista.
    assert [c.tool for c in respuesta.consultas] == ["ver_disponibilidad"]


def test_con_tiempo_hace_todas_las_rondas_que_necesita(
    criadero: Criadero,
) -> None:
    instantes = iter(range(0, 1000, 1))
    modelo = ModeloGuionado([pide(*DISPONIBILIDAD), responde("Hay 50.")])

    respuesta = conversacion.conversar(
        [MensajeAsistente(rol="usuario", texto="¿Cuántas hay?")],
        modelo=modelo,
        identidad=Identidad("usuario-1", criadero.tenant, frozenset({"lectura"})),
        reloj=lambda: float(next(instantes)),
    )

    assert respuesta.texto == "Hay 50."
    assert len(modelo.pedidos) == 2


# --- limite de IA ---


def test_cada_llamada_reserva_y_anota_sus_tokens(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    modelo = ModeloGuionado([pide(*DISPONIBILIDAD), responde("Hay 50.")])

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 200
    filas = llamadas_ia(motor_owner, criadero.tenant)
    assert [(f.tokens_entrada, f.tokens_salida) for f in filas] == [(120, 30)] * 2
    assert {f.motivo for f in filas} == {conversacion.MOTIVO_LIMITE}


def test_pasado_el_limite_de_ia_es_429_con_retry_after(
    api: TestClient, criadero: Criadero, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IA_LLAMADAS_POR_MINUTO", "1")
    primero = conversar(api, criadero, ModeloGuionado([responde("Hola.")]))
    modelo = ModeloGuionado([responde("no llega")])

    segundo = conversar(api, criadero, modelo)

    assert primero.status_code == 200
    assert segundo.status_code == 429
    assert segundo.json()["error"]["code"] == "limite_ia_por_minuto"
    assert segundo.headers["Retry-After"] == "60"
    assert modelo.pedidos == []


def test_el_limite_corta_tambien_a_mitad_de_las_rondas(
    api: TestClient, criadero: Criadero, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IA_LLAMADAS_POR_MINUTO", "1")
    modelo = ModeloGuionado([pide(*DISPONIBILIDAD), responde("no llega")])

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 429
    assert len(modelo.pedidos) == 1


# --- Bedrock caido ---


@pytest.mark.parametrize(
    "modelo",
    [
        pytest.param(ModeloCaido(), id="excepcion"),
        pytest.param(ModeloGuionado([{"sin": "output"}]), id="respuesta_rota"),
    ],
)
def test_si_bedrock_falla_es_503_sin_detalles(
    api: TestClient, criadero: Criadero, modelo: Any, logs: io.StringIO
) -> None:
    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 503
    error = respuesta.json()["error"]
    assert error["code"] == "asistente_no_disponible"
    assert "AWS" not in json.dumps(respuesta.json())
    assert "detalle interno" not in logs.getvalue()


def test_sin_boto3_el_cliente_real_responde_503(
    api: TestClient, criadero: Criadero, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sin_modulo(nombre: str) -> Any:
        raise ModuleNotFoundError(nombre)

    monkeypatch.setattr(conversacion.importlib, "import_module", sin_modulo)
    router_asistente._cliente_bedrock.cache_clear()
    try:
        respuesta = api.post(
            "/api/v1/asistente",
            json={"mensajes": [{"rol": "usuario", "texto": "Hola"}]},
            headers=como(criadero.tenant),
        )
    finally:
        router_asistente._cliente_bedrock.cache_clear()

    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["code"] == "asistente_no_disponible"


def test_el_cliente_real_lleva_timeouts_cortos_y_sin_reintentos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creados: dict[str, Any] = {}

    class Config:
        def __init__(self, **kwargs: Any) -> None:
            creados["config"] = kwargs

    class Boto3:
        @staticmethod
        def client(servicio: str, **kwargs: Any) -> str:
            creados["cliente"] = (servicio, kwargs["region_name"])
            return "cliente"

    modulos = {"boto3": Boto3, "botocore.config": type("M", (), {"Config": Config})}
    monkeypatch.setattr(conversacion.importlib, "import_module", modulos.__getitem__)

    assert conversacion.crear_cliente_asistente() == "cliente"
    assert creados["cliente"] == ("bedrock-runtime", "us-east-1")
    assert creados["config"] == {
        "connect_timeout": 2,
        "read_timeout": 9,
        "retries": {"max_attempts": 1, "mode": "standard"},
    }


# --- logs ---


def test_los_logs_no_llevan_el_contenido_de_los_mensajes(
    api: TestClient, criadero: Criadero, logs: io.StringIO
) -> None:
    modelo = ModeloGuionado(
        [pide(*DISPONIBILIDAD), responde("Respuesta ZANAHORIA-DEL-MODELO")]
    )

    respuesta = conversar(
        api,
        criadero,
        modelo,
        mensajes=[{"rol": "usuario", "texto": "Pregunta ZANAHORIA-SECRETA"}],
    )

    assert respuesta.status_code == 200
    salida = logs.getvalue()
    assert "ZANAHORIA" not in salida
    assert "Buckfast" not in salida
    (linea,) = [
        json.loads(renglon)
        for renglon in salida.splitlines()
        if "asistente respondio" in renglon
    ]
    assert linea["herramientas"] == ["ver_disponibilidad"]
    assert linea["llamadas"] == 2
    assert linea["consumo_entrada"] == 240
    assert linea["consumo_salida"] == 60
    assert linea["fin"] == "respuesta"


# --- solo lectura ---


def test_las_herramientas_corren_en_una_transaccion_de_solo_lectura(
    criadero: Criadero, monkeypatch: pytest.MonkeyPatch
) -> None:
    def escribe(ctx: Contexto, entrada: Any) -> Resultado:
        ctx.sesion.execute(text("INSERT INTO raza (nombre) VALUES ('Intrusa')"))
        return Resultado([], "no llega")

    falsa = Herramienta("escribe", "", EntradaDisponibilidad, escribe)
    monkeypatch.setattr(herramientas, "POR_NOMBRE", {"escribe": falsa})

    with pytest.raises(InternalError, match="read-only transaction"):
        herramientas.ejecutar(
            "escribe", {}, tenant_id=criadero.tenant, grupos=frozenset({"admin"})
        )
