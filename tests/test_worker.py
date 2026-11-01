"""Tests del worker de SQS (#68).

El contrato critico: el log de recepcion nunca contiene el cuerpo del
mensaje (puede llevar datos del tenant), y el circuito de veneno a la DLQ
se mantiene intacto.
"""

import logging

import pytest

from app import worker
from app.worker import handler


def _evento(cuerpo: str, id_mensaje: str = "id-1") -> dict:
    return {
        "Records": [
            {
                "body": cuerpo,
                "messageId": id_mensaje,
                "eventSourceARN": "arn:aws:sqs:us-east-1:1:documentos",
            }
        ]
    }


def test_el_log_no_contiene_el_cuerpo(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        respuesta = handler(_evento('{"documento": "privado-del-tenant"}'), None)

    assert respuesta == {"mensajes_procesados": 1}
    assert "privado-del-tenant" not in caplog.text
    assert "id_mensaje=id-1" in caplog.text
    assert "cola=documentos" in caplog.text


def test_el_mensaje_veneno_sigues_yendo_a_la_dlq(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="veneno"):
            handler(_evento("veneno"), None)

    assert "veneno" not in caplog.text


def test_un_mensaje_de_borrador_se_genera_con_el_modelo(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#40: el worker resuelve el borrador; el log sigue sin el cuerpo."""
    llamados: list[tuple[dict, object]] = []
    modelo = object()
    monkeypatch.setattr(
        worker.generacion, "procesar", lambda m, c: llamados.append((m, c))
    )
    monkeypatch.setattr(worker, "cliente_ia", lambda: modelo)
    cuerpo = '{"borrador_id": "b-1", "tenant_id": "t-1"}'

    with caplog.at_level(logging.INFO):
        respuesta = handler(_evento(cuerpo), None)

    assert respuesta == {"mensajes_procesados": 1}
    assert llamados == [({"borrador_id": "b-1", "tenant_id": "t-1"}, modelo)]
    assert "b-1" not in caplog.text
