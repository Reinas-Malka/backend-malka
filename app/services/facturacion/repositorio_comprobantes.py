"""Acceso a la tabla comprobante (#108).

Ninguna consulta filtra por tenant_id: RLS se encarga (ADR 0009). Un
comprobante, cliente o archivo de otro criadero responde igual que uno que
no existe.

Reglas, ademas de las del schema:
- El cliente existe en el criadero (422 cliente_inexistente). Un cliente
  dado de baja conserva su facturacion: la factura ya la emitio ARCA.
- La letra es coherente con el cliente: exterior E, responsable inscripto
  A, resto B (422 letra_incoherente).
- El PDF esta subido y registrado en el criadero (422 archivo_inexistente).
- (tipo, punto_venta, numero) no se repite en el criadero (409
  comprobante_duplicado), como la clave natural de ARCA.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, ErrorDeApi, NoEncontradoError, ValidacionError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.comprobantes import ComprobanteCrear, ComprobanteRespuesta
from app.services.facturacion.comprobantes import letra_esperada
from app.services.repositorio import escribir
from app.services.ventas.clientes import CondicionIva, TipoCliente

LEER_CLIENTE = text("SELECT tipo, condicion_iva FROM cliente WHERE id = :id")

EXISTE_ARCHIVO = text("SELECT 1 FROM documento WHERE archivo_s3_key = :clave")

# Las consultas se escriben completas, sin armar SQL con f-strings: los
# valores viajan siempre como parametros (:nombre), nunca pegados al texto.

# tenant_id lo pone el default de la columna (el de la sesion).
INSERTAR = text(
    "INSERT INTO comprobante (tipo, cliente_id, punto_venta, numero, "
    "fecha_emision, moneda, importe, tipo_cambio, archivo_s3_key, cargado_por) "
    "VALUES (:tipo, :cliente_id, :punto_venta, :numero, :fecha_emision, "
    ":moneda, :importe, :tipo_cambio, :archivo_s3_key, :cargado_por) "
    "RETURNING id, tipo, cliente_id, punto_venta, numero, fecha_emision, moneda, "
    "importe, tipo_cambio, archivo_s3_key, cargado_por, creado_en"
)

# De la emision mas nueva a la mas vieja; el id desempata. Las dos columnas
# van DESC: la pagina siguiente sigue con (fecha_emision, id) < cursor. El
# CAST del cliente es obligatorio: un NULL sin tipo hace fallar a PostgreSQL.
LISTAR = text(
    "SELECT id, tipo, cliente_id, punto_venta, numero, fecha_emision, moneda, "
    "importe, tipo_cambio, archivo_s3_key, cargado_por, creado_en "
    "FROM comprobante "
    "WHERE (CAST(:cliente_id AS uuid) IS NULL OR cliente_id = :cliente_id) "
    "AND (:sin_cursor OR (fecha_emision, id) < "
    "(CAST(:c_fecha AS date), CAST(:c_id AS uuid))) "
    "ORDER BY fecha_emision DESC, id DESC LIMIT :limite"
)

OBTENER = text(
    "SELECT id, tipo, cliente_id, punto_venta, numero, fecha_emision, moneda, "
    "importe, tipo_cambio, archivo_s3_key, cargado_por, creado_en "
    "FROM comprobante WHERE id = :id"
)


def no_encontrado() -> ErrorDeApi:
    return NoEncontradoError(
        "No existe el comprobante.", code="comprobante_no_encontrado"
    )


def _duplicado() -> ErrorDeApi:
    return ConflictoError(
        "Ya hay un comprobante con ese tipo, punto de venta y numero.",
        code="comprobante_duplicado",
    )


# Nombre de la restriccion en la migracion 0014.
CONFLICTOS = {"uq_comprobante_tenant_tipo_punto_venta_numero": _duplicado}


def _a_respuesta(fila: Row[Any]) -> ComprobanteRespuesta:
    return ComprobanteRespuesta.model_validate(dict(fila._mapping))


def _validar_cliente(sesion: Session, datos: ComprobanteCrear) -> None:
    """El cliente es del criadero y la letra transcripta es la que le toca."""
    cliente = sesion.execute(LEER_CLIENTE, {"id": datos.cliente_id}).one_or_none()
    if cliente is None:
        # 422 y no 404: el recurso de la URL existe, el dato del cuerpo no.
        raise ValidacionError("No existe el cliente.", code="cliente_inexistente")
    esperada = letra_esperada(
        TipoCliente(cliente.tipo), CondicionIva(cliente.condicion_iva)
    )
    if datos.tipo != esperada:
        raise ValidacionError(
            f"A este cliente ARCA le emite factura {esperada}, no {datos.tipo}.",
            code="letra_incoherente",
            details={"esperada": str(esperada)},
        )


def _validar_archivo(sesion: Session, clave: str) -> None:
    """El PDF se subio con POST /api/v1/documentos/subidas en este criadero."""
    if sesion.execute(EXISTE_ARCHIVO, {"clave": clave}).first() is None:
        raise ValidacionError(
            "El archivo no esta subido en este criadero.",
            code="archivo_inexistente",
        )


def crear(
    sesion: Session, datos: ComprobanteCrear, cargado_por: str
) -> ComprobanteRespuesta:
    """Registra lo transcripto. Los numeros se guardan tal cual llegaron."""
    _validar_cliente(sesion, datos)
    _validar_archivo(sesion, datos.archivo_s3_key)
    fila = escribir(
        sesion,
        INSERTAR,
        {**datos.model_dump(), "cargado_por": cargado_por},
        conflictos=CONFLICTOS,
        no_encontrado=no_encontrado,
    )
    return _a_respuesta(fila)


def listar(
    sesion: Session,
    pagina: ParametrosDePagina,
    *,
    cliente_id: UUID | None = None,
) -> Pagina[ComprobanteRespuesta]:
    c_fecha, c_id = valores_del_cursor(pagina, (date.fromisoformat, UUID))
    filas = sesion.execute(
        LISTAR,
        {
            "cliente_id": cliente_id,
            "sin_cursor": pagina.cursor is None,
            "c_fecha": c_fecha,
            "c_id": c_id,
            # Uno de mas: si viene, hay pagina siguiente.
            "limite": pagina.limite + 1,
        },
    )
    return armar_pagina(
        [_a_respuesta(fila) for fila in filas],
        pagina.limite,
        lambda comprobante: (comprobante.fecha_emision.isoformat(), comprobante.id),
    )


def obtener(sesion: Session, comprobante_id: UUID) -> ComprobanteRespuesta:
    """Un comprobante. 404 si no existe o es de otro criadero."""
    fila = sesion.execute(OBTENER, {"id": comprobante_id}).one_or_none()
    if fila is None:
        raise no_encontrado()
    return _a_respuesta(fila)
