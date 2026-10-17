"""Subida y descarga de documentos por URL prefirmada (#102).

Pedir una subida pueden admin y ventas (comprobantes y documentos de
exportacion); descargar, cualquier rol del criadero. Los bytes nunca pasan
por la API: van del navegador a S3.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import Identidad, require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.schemas.documentos import DescargaPreparada, PedidoDeSubida, SubidaPreparada
from app.services.documentos import almacenamiento, repositorio_documentos
from app.services.documentos.almacenamiento import Firmador

router = APIRouter(prefix="/api/v1/documentos", tags=["documentos"])


def obtener_firmador() -> Firmador:
    """Cliente de S3. Los tests lo reemplazan con dependency_overrides."""
    return almacenamiento.firmador_s3()


@router.post("/subidas", status_code=201, response_model=SubidaPreparada)
def preparar_subida(
    datos: PedidoDeSubida,
    # Antes que la sesion: un rol sin permiso no abre ninguna conexion.
    identidad: Identidad = Depends(require_role("admin", "ventas")),
    solicitud: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
    firmador: Firmador = Depends(obtener_firmador),
) -> JSONResponse:
    def preparar() -> SubidaPreparada:
        clave = almacenamiento.armar_clave(identidad.tenant_id, datos.nombre)
        # Se firma antes de registrar: sin bucket configurado no queda una
        # fila de un archivo que nunca se pudo subir.
        url = almacenamiento.firmar_subida(firmador, clave, datos.tipo)
        repositorio_documentos.registrar(sesion, clave, datos, identidad.user_id)
        return SubidaPreparada(
            url_subida=url,
            clave=clave,
            expira_en_segundos=almacenamiento.SEGUNDOS_SUBIDA,
        )

    return responder_una_vez(sesion, solicitud, preparar)


@router.get("/descarga", response_model=DescargaPreparada)
def preparar_descarga(
    clave: str = Query(min_length=1, max_length=300),
    sesion: Session = Depends(obtener_sesion),
    firmador: Firmador = Depends(obtener_firmador),
) -> DescargaPreparada:
    # Solo se firma lo que esta registrado en el criadero de la sesion (RLS):
    # conocer la key de otro no alcanza para bajarla.
    nombre = repositorio_documentos.nombre_de(sesion, clave)
    return DescargaPreparada(
        url_descarga=almacenamiento.firmar_descarga(firmador, clave, nombre),
        expira_en_segundos=almacenamiento.SEGUNDOS_DESCARGA,
    )
