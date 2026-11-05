"""Hash canonico y snapshot del borrador aprobado (#42), sin base."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from app.services.ia.borrador import BorradorValidado
from app.services.ia.snapshot import (
    a_json,
    armar_snapshot,
    hash_contenido,
    json_canonico,
    sha256_canonico,
)

CONTENIDO = BorradorValidado(encabezado="Nota de venta", cuerpo="Señor — ok")


def test_el_json_canonico_no_depende_del_orden_ni_de_los_espacios() -> None:
    assert json_canonico({"b": 1, "a": "ñ"}) == json_canonico({"a": "ñ", "b": 1})
    # Compacto y en UTF-8 sin escapes: el front lo arma igual.
    assert json_canonico({"b": 1, "a": "ñ"}) == '{"a":"ñ","b":1}'.encode()


def test_el_hash_del_contenido_es_sha256_del_json_canonico() -> None:
    esperado = hashlib.sha256(
        '{"cuerpo":"Señor — ok","encabezado":"Nota de venta","notas":""}'.encode()
    ).hexdigest()

    assert hash_contenido(CONTENIDO) == esperado
    assert sha256_canonico(CONTENIDO.model_dump()) == esperado


def test_otra_version_da_otro_hash() -> None:
    otra = CONTENIDO.model_copy(update={"notas": "una nota"})

    assert hash_contenido(otra) != hash_contenido(CONTENIDO)


def test_a_json_no_pierde_precision() -> None:
    identificador = uuid.uuid4()

    assert a_json(Decimal("35000.50")) == "35000.50"
    assert a_json(identificador) == str(identificador)
    assert a_json(date(2026, 11, 20)) == "2026-11-20"
    assert a_json(datetime(2026, 11, 5, 15, tzinfo=UTC)) == "2026-11-05T15:00:00+00:00"
    assert a_json(3) == 3


def test_en_exportacion_el_snapshot_va_sin_iva() -> None:
    pedido = {
        "id": uuid.uuid4(),
        "cliente_id": uuid.uuid4(),
        "cliente_nombre": "Bienenzucht GmbH",
        "cliente_pais": "DE",
        "cliente_tipo": "exportacion",
        "cliente_condicion_iva": "cliente_exterior",
        "cliente_cuit_o_tax_id": "DE123456789",
        "moneda": "EUR",
        "fecha_entrega_solicitada": date(2026, 11, 30),
        "destino_pais": "DE",
        "incoterm": "FCA",
    }
    linea = {
        "numero_linea": 1,
        "cantidad": 10,
        "item": "Reina fecundada",
        "raza": "Carniola",
        "precio_unitario": Decimal("35.00"),
        "alicuota": Decimal("0.210"),
    }

    snapshot = armar_snapshot(
        borrador={
            "id": uuid.uuid4(),
            "modelo": "m",
            "version_prompt": "borrador_v3",
            "corrige_a": None,
        },
        contenido=CONTENIDO,
        pedido=pedido,
        lineas=[linea],
        comprobante=None,
        usuario="sub",
        momento=datetime(2026, 11, 5, tzinfo=UTC),
    )

    assert snapshot["pedido"]["totales"] == {
        "exportacion": True,
        "por_alicuota": [{"alicuota": "0", "neto": "350.00", "iva": "0.00"}],
        "neto": "350.00",
        "iva": "0.00",
        "total": "350.00",
    }
    # La alicuota de la linea queda como se copio; el 0% es del calculo.
    assert snapshot["pedido"]["lineas"][0]["alicuota"] == "0.21"
    assert snapshot["pedido"]["incoterm"] == "FCA"
