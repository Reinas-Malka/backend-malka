"""Acceso a la tabla documento (#102).

Ninguna consulta filtra por tenant_id: RLS se encarga (ADR 0009). La key de
otro criadero no se encuentra: responde igual que una que no existe.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errores import NoEncontradoError
from app.schemas.documentos import PedidoDeSubida

# tenant_id no se manda: lo completa PostgreSQL con el de la sesion.
INSERTAR = text(
    "INSERT INTO documento (archivo_s3_key, nombre, tipo_contenido, "
    "tamano_bytes, subido_por) "
    "VALUES (:clave, :nombre, :tipo, :tamano, :subido_por)"
)

BUSCAR_NOMBRE = text("SELECT nombre FROM documento WHERE archivo_s3_key = :clave")


def registrar(
    sesion: Session, clave: str, datos: PedidoDeSubida, subido_por: str
) -> None:
    """Guarda los datos del archivo que se va a subir."""
    sesion.execute(
        INSERTAR,
        {
            "clave": clave,
            "nombre": datos.nombre,
            "tipo": datos.tipo,
            "tamano": datos.tamano,
            "subido_por": subido_por,
        },
    )


def nombre_de(sesion: Session, clave: str) -> str:
    """El nombre original del documento. 404 si no es del criadero."""
    nombre = sesion.execute(BUSCAR_NOMBRE, {"clave": clave}).scalar_one_or_none()
    if nombre is None:
        raise NoEncontradoError(
            "No existe el documento.", code="documento_no_encontrado"
        )
    return str(nombre)
