"""Metricas EMF y request_id de punta a punta (#44).

Las metricas son lineas JSON en stdout: se prueban leyendo lo que el codigo
escribe, sin AWS. Lo que toca la base (reservas, transiciones, generacion)
se prueba en sus propios tests contra Postgres; aca, el formato, los
errores de conexion y el viaje del request_id de la API al worker.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InterfaceError, OperationalError

from app import contexto, metricas, worker
from app.services.ia import generacion
from app.services.ia.cliente import pedir_al_modelo
from tests.conftest import crear_app_de_prueba


def lineas_emf(salida: str) -> list[dict[str, Any]]:
    return [
        json.loads(renglon)
        for renglon in salida.splitlines()
        if renglon.startswith("{") and "_aws" in renglon
    ]


class _Sqlstate(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("detalle interno")
        self.sqlstate = sqlstate


def _operacional(sqlstate: str | None) -> OperationalError:
    return OperationalError("SELECT 1", {}, _Sqlstate(sqlstate))


# --- formato ---


def test_contar_escribe_una_linea_emf_con_el_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    marca = contexto.request_id.set("req-emf")
    try:
        metricas.contar(metricas.TRANSICIONES_INVALIDAS, entidad="pedido")
    finally:
        contexto.request_id.reset(marca)

    [linea] = lineas_emf(capsys.readouterr().out)
    [definicion] = linea["_aws"]["CloudWatchMetrics"]
    assert definicion["Namespace"] == "MalkaSuite"
    # Sin dimensiones: las alarmas miran el total.
    assert definicion["Dimensions"] == [[]]
    assert definicion["Metrics"] == [
        {"Name": "transiciones_invalidas", "Unit": "Count"}
    ]
    assert linea["transiciones_invalidas"] == 1
    assert linea["request_id"] == "req-emf"
    assert linea["entidad"] == "pedido"
    assert isinstance(linea["_aws"]["Timestamp"], int)


def test_una_llamada_a_bedrock_emite_latencia_y_tokens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Modelo:
        def converse(self, **_: Any) -> dict[str, Any]:
            return {
                "output": {"message": {"content": [{"text": "{}"}]}},
                "usage": {"inputTokens": 100, "outputTokens": 40},
            }

    pedir_al_modelo(Modelo(), "sistema", "mensaje")

    [linea] = lineas_emf(capsys.readouterr().out)
    nombres = {
        m["Name"]: m["Unit"] for m in linea["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    }
    assert nombres == {"bedrock_latency_ms": "Milliseconds", "bedrock_tokens": "Count"}
    assert linea["bedrock_tokens"] == 140
    assert linea["uso"] == "borrador"
    # Ni el prompt ni la respuesta van a la metrica.
    assert "mensaje" not in json.dumps(linea)


def test_una_metrica_que_no_se_puede_escribir_no_rompe_nada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def falla(_: str) -> int:
        raise OSError("stdout cerrado")

    monkeypatch.setattr(metricas.sys.stdout, "write", falla)
    metricas.contar(metricas.DOCUMENTOS_GENERADOS)


# --- errores de conexion con RDS ---


@pytest.mark.parametrize(
    ("error", "es_de_conexion"),
    [
        (_operacional(None), True),
        (_operacional("08006"), True),
        (InterfaceError("SELECT 1", {}, _Sqlstate(None)), True),
        # lock_timeout y deadlock: la base anda, es otra operacion.
        (_operacional("55P03"), False),
        (_operacional("40P01"), False),
        (RuntimeError("otra cosa"), False),
    ],
)
def test_que_cuenta_como_error_de_conexion(
    error: BaseException, es_de_conexion: bool
) -> None:
    assert metricas.es_error_de_conexion(error) is es_de_conexion


def test_la_api_cuenta_la_base_caida_y_responde_500(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = crear_app_de_prueba()

    @app.get("/base-caida")
    def base_caida() -> None:
        raise _operacional(None)

    respuesta = TestClient(app).get("/base-caida", headers={"x-request-id": "req-rds"})

    assert respuesta.status_code == 500
    [linea] = [
        linea
        for linea in lineas_emf(capsys.readouterr().out)
        if "errores_conexion_rds" in linea
    ]
    assert linea["request_id"] == "req-rds"
    assert linea["origen"] == "api"


def test_un_lock_ocupado_no_cuenta_como_base_caida(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = crear_app_de_prueba()

    @app.get("/ocupada")
    def ocupada() -> None:
        raise _operacional("55P03")

    assert TestClient(app).get("/ocupada").status_code == 409
    assert lineas_emf(capsys.readouterr().out) == []


# --- request_id de la API al worker ---


class _Cola:
    def __init__(self) -> None:
        self.mensajes: list[dict[str, Any]] = []

    def enviar(self, cuerpo: dict[str, Any]) -> None:
        self.mensajes.append(cuerpo)


def test_el_mensaje_de_sqs_lleva_el_request_id() -> None:
    cola = _Cola()
    borrador = uuid.uuid4()

    generacion.encolar(cola, uuid.uuid4(), borrador, "req-api")

    [mensaje] = cola.mensajes
    assert mensaje["request_id"] == "req-api"
    assert mensaje["borrador_id"] == str(borrador)
    # El contexto vuelve a como estaba.
    assert contexto.request_id_actual() == contexto.SIN_REQUEST_ID


def test_el_worker_procesa_con_el_request_id_del_mensaje(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    vistos: list[str] = []
    monkeypatch.setattr(
        worker.generacion,
        "procesar",
        lambda m, c: vistos.append(contexto.request_id_actual()),
    )
    monkeypatch.setattr(worker, "cliente_ia", lambda: object())
    cuerpo = json.dumps(
        {"borrador_id": "b-1", "tenant_id": "t-1", "request_id": "req-api"}
    )

    with caplog.at_level(logging.INFO):
        worker.handler(
            {
                "Records": [
                    {
                        "body": cuerpo,
                        "messageId": "m-1",
                        "eventSourceARN": "a:documentos",
                    }
                ]
            },
            None,
        )

    assert vistos == ["req-api"]
    assert "request_id=req-api" in caplog.text
    assert contexto.request_id_actual() == contexto.SIN_REQUEST_ID


def test_sin_request_id_valido_el_worker_usa_el_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vistos: list[str] = []
    monkeypatch.setattr(
        worker.generacion,
        "procesar",
        lambda m, c: vistos.append(contexto.request_id_actual()),
    )
    monkeypatch.setattr(worker, "cliente_ia", lambda: object())
    cuerpo = json.dumps(
        {"borrador_id": "b-1", "tenant_id": "t-1", "request_id": "no valido!"}
    )

    worker.handler({"Records": [{"body": cuerpo, "messageId": "m-9"}]}, None)

    assert vistos == ["m-9"]


def test_el_worker_cuenta_la_base_caida_y_deja_reintentar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def falla(m: dict[str, Any], c: object) -> None:
        raise _operacional(None)

    monkeypatch.setattr(worker.generacion, "procesar", falla)
    monkeypatch.setattr(worker, "cliente_ia", lambda: object())
    cuerpo = json.dumps(
        {"borrador_id": "b-1", "tenant_id": "t-1", "request_id": "req-w"}
    )

    # Se relanza: SQS reintenta y, tras 3 fallas, el mensaje va a la DLQ.
    with pytest.raises(OperationalError):
        worker.handler({"Records": [{"body": cuerpo, "messageId": "m-1"}]}, None)

    [linea] = lineas_emf(capsys.readouterr().out)
    assert linea["origen"] == "worker"
    assert linea["request_id"] == "req-w"
