"""Tests del worker de SQS (#68).

El contrato critico: el log de recepcion nunca contiene el cuerpo del
mensaje (puede llevar datos del tenant), y el circuito de veneno a la DLQ
se mantiene intacto.
"""

import logging

import pytest

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
