"""Hash y snapshot de un borrador aprobado (#42), sin base ni FastAPI.

El snapshot es exactamente lo que se aprobo: el borrador (lo que redacto la
IA), el pedido con los totales que calcula el sistema (ADR 0008), el
comprobante transcripto si se referencia (tal como esta en su tabla, nada
calculado) y quien aprobo, cuando y sobre que version. El PDF se renderiza
desde el snapshot, no desde la base: lo que se ve es lo que quedo congelado.

Los Decimal viajan como texto ("35000.50"), nunca como float, y las fechas
en ISO 8601. El hash es sha256 del JSON canonico (claves ordenadas,
separadores compactos, UTF-8 sin escapes): la misma version da el mismo hash
en cualquier lado, y el front puede calcularlo igual.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.documentos.importes import (
    MONEDA_LOCAL,
    Alicuota,
    Linea,
    calcular_totales_pedido,
)
from app.services.ia.borrador import BorradorValidado

# Sube si cambia la forma del snapshot: el PDF de uno viejo se sigue leyendo.
VERSION_SNAPSHOT = 1


def json_canonico(datos: Any) -> bytes:
    """El JSON que se hashea: mismo dato, mismos bytes."""
    return json.dumps(
        datos, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_canonico(datos: Any) -> str:
    return hashlib.sha256(json_canonico(datos)).hexdigest()


def hash_contenido(contenido: BorradorValidado) -> str:
    """La version del borrador que ve quien aprueba (encabezado, cuerpo, notas)."""
    return sha256_canonico(contenido.model_dump())


def a_json(valor: Any) -> Any:
    """Un valor de la base, listo para JSON sin perder precision."""
    if isinstance(valor, Decimal | UUID):
        return str(valor)
    if isinstance(valor, datetime | date):
        return valor.isoformat()
    return valor


def _fila(datos: Mapping[str, Any]) -> dict[str, Any]:
    return {clave: a_json(valor) for clave, valor in datos.items()}


def _pedido(
    pedido: Mapping[str, Any], lineas: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """El pedido con lineas y totales calculados por el sistema (ADR 0008)."""
    exportacion = pedido["moneda"] != MONEDA_LOCAL
    totales = calcular_totales_pedido(
        [
            Linea(
                linea["cantidad"],
                linea["precio_unitario"],
                Alicuota(linea["alicuota"]),
            )
            for linea in lineas
        ],
        moneda=pedido["moneda"],
        exportacion=exportacion,
    )
    return {
        "id": str(pedido["id"]),
        "cliente": {
            "id": str(pedido["cliente_id"]),
            "nombre": pedido["cliente_nombre"],
            "pais": pedido["cliente_pais"],
            "tipo": pedido["cliente_tipo"],
            "condicion_iva": pedido["cliente_condicion_iva"],
            "cuit_o_tax_id": pedido["cliente_cuit_o_tax_id"],
        },
        "moneda": pedido["moneda"],
        "fecha_entrega_solicitada": a_json(pedido["fecha_entrega_solicitada"]),
        "destino_pais": pedido["destino_pais"],
        "incoterm": pedido["incoterm"],
        "lineas": [
            {
                "numero_linea": linea["numero_linea"],
                "cantidad": linea["cantidad"],
                "item": linea["item"],
                "raza": linea["raza"],
                "precio_unitario": str(linea["precio_unitario"]),
                "alicuota": str(Alicuota(linea["alicuota"]).value),
                "neto": str(neto),
            }
            for linea, neto in zip(lineas, totales.lineas_neto, strict=True)
        ],
        "totales": {
            "exportacion": exportacion,
            "por_alicuota": [
                {
                    "alicuota": str(s.alicuota.value),
                    "neto": str(s.neto),
                    "iva": str(s.iva),
                }
                for s in totales.por_alicuota
            ],
            "neto": str(totales.neto),
            "iva": str(totales.iva),
            "total": str(totales.total),
        },
    }


def armar_snapshot(
    *,
    borrador: Mapping[str, Any],
    contenido: BorradorValidado,
    pedido: Mapping[str, Any],
    lineas: Sequence[Mapping[str, Any]],
    comprobante: Mapping[str, Any] | None,
    usuario: str,
    momento: datetime,
) -> dict[str, Any]:
    """Todo lo aprobado, en un dict JSON. Nada se recalcula despues."""
    return {
        "version": VERSION_SNAPSHOT,
        "borrador": {
            "id": str(borrador["id"]),
            "modelo": borrador["modelo"],
            "version_prompt": borrador["version_prompt"],
            "corrige_a": a_json(borrador["corrige_a"]),
            "contenido": contenido.model_dump(),
        },
        "pedido": _pedido(pedido, lineas),
        # Tal como se transcribio (#108): ni un campo calculado.
        "comprobante": None if comprobante is None else _fila(comprobante),
        "aprobacion": {
            "usuario": usuario,
            "momento": momento.isoformat(),
            "hash_contenido": hash_contenido(contenido),
        },
    }
