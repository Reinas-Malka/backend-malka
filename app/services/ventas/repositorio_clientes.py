"""Acceso a la tabla cliente (#32).

Toda funcion recibe la sesion de obtener_sesion, que ya tiene el tenant fijado:
ninguna consulta filtra por tenant_id, de eso se encarga RLS (ADR 0009). Un
cliente de otro criadero no aparece, asi que se responde 404 y no 403: decir
"no tenes permiso" confirmaria que existe.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import Row, TextClause, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errores import ConflictoError, NoEncontradoError
from app.schemas.clientes import ClienteActualizar, ClienteCrear, ClienteRespuesta

# Nombre de la restriccion en la migracion 0005.
CUIT_UNICO = "uq_cliente_tenant_cuit_o_tax_id"

# Las consultas se escriben completas, sin armar SQL con f-strings: los valores
# viajan siempre como parametros (:nombre), nunca pegados al texto.
INSERTAR = text(
    "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
    "VALUES (:nombre, :pais, :tipo, :condicion_iva, :cuit_o_tax_id) "
    "RETURNING id, nombre, pais, tipo, condicion_iva, cuit_o_tax_id, activo"
)

LISTAR = text(
    "SELECT id, nombre, pais, tipo, condicion_iva, cuit_o_tax_id, activo "
    "FROM cliente WHERE activo OR :incluir_inactivos ORDER BY nombre, id"
)

OBTENER = text(
    "SELECT id, nombre, pais, tipo, condicion_iva, cuit_o_tax_id, activo "
    "FROM cliente WHERE id = :id"
)

# FOR UPDATE: si dos PATCH llegan juntos, el segundo espera al primero y
# valida sobre lo que el primero guardo.
OBTENER_PARA_EDITAR = text(
    "SELECT id, nombre, pais, tipo, condicion_iva, cuit_o_tax_id, activo "
    "FROM cliente WHERE id = :id FOR UPDATE"
)

ACTUALIZAR = text(
    "UPDATE cliente SET nombre = :nombre, pais = :pais, tipo = :tipo, "
    "condicion_iva = :condicion_iva, cuit_o_tax_id = :cuit_o_tax_id, "
    "activo = :activo, actualizado_en = now() WHERE id = :id "
    "RETURNING id, nombre, pais, tipo, condicion_iva, cuit_o_tax_id, activo"
)

DESACTIVAR = text(
    "UPDATE cliente SET activo = false, actualizado_en = now() "
    "WHERE id = :id RETURNING id"
)


def _a_respuesta(fila: Row[Any]) -> ClienteRespuesta:
    return ClienteRespuesta.model_validate(dict(fila._mapping))


def _no_encontrado() -> NoEncontradoError:
    return NoEncontradoError("No existe el cliente.", code="cliente_no_encontrado")


def _escribir(
    sesion: Session, sentencia: TextClause, parametros: dict[str, Any]
) -> Row[Any]:
    """Ejecuta un INSERT o UPDATE y traduce el CUIT repetido a 409.

    Cualquier otra violacion (un CHECK, por ejemplo) es un bug: no se traduce
    y termina en 500, que queda en el log con su traza.
    """
    try:
        fila = sesion.execute(sentencia, parametros).one_or_none()
    except IntegrityError as error:
        diagnostico = getattr(error.orig, "diag", None)
        if getattr(diagnostico, "constraint_name", None) == CUIT_UNICO:
            raise ConflictoError(
                "Ya hay un cliente con ese CUIT o tax id.", code="cliente_duplicado"
            ) from None
        raise
    if fila is None:
        raise _no_encontrado()
    return fila


def crear(sesion: Session, datos: ClienteCrear) -> ClienteRespuesta:
    return _a_respuesta(_escribir(sesion, INSERTAR, datos.model_dump(mode="json")))


def listar(
    sesion: Session, *, incluir_inactivos: bool = False
) -> list[ClienteRespuesta]:
    filas = sesion.execute(LISTAR, {"incluir_inactivos": incluir_inactivos})
    return [_a_respuesta(fila) for fila in filas]


def obtener(sesion: Session, cliente_id: UUID) -> ClienteRespuesta:
    fila = sesion.execute(OBTENER, {"id": cliente_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    return _a_respuesta(fila)


def actualizar(
    sesion: Session, cliente_id: UUID, cambios: ClienteActualizar
) -> ClienteRespuesta:
    """Aplica los cambios sobre el cliente guardado y valida el resultado.

    Asi un PATCH que solo trae {"tipo": "exportacion"} se rechaza si el pais
    guardado es AR: las reglas son las de ClienteCrear, sin duplicarlas.
    """
    fila = sesion.execute(OBTENER_PARA_EDITAR, {"id": cliente_id}).one_or_none()
    if fila is None:
        raise _no_encontrado()
    actual = _a_respuesta(fila)

    pedidos = cambios.model_dump(exclude_unset=True, exclude={"activo"})
    guardados = actual.model_dump(include=set(ClienteCrear.model_fields))
    try:
        validado = ClienteCrear.model_validate({**guardados, **pedidos})
    except ValidationError as error:
        # Mismo 422 y misma forma que si el error viniera en el body de un POST.
        raise RequestValidationError(error.errors()) from None

    activo = actual.activo if cambios.activo is None else cambios.activo
    parametros = {
        **validado.model_dump(mode="json"),
        "activo": activo,
        "id": cliente_id,
    }
    return _a_respuesta(_escribir(sesion, ACTUALIZAR, parametros))


def desactivar(sesion: Session, cliente_id: UUID) -> None:
    """Borrado logico: el cliente sigue existiendo para sus pedidos y facturas."""
    _escribir(sesion, DESACTIVAR, {"id": cliente_id})
