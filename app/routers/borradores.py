"""Borradores de documentos con IA (#40) y su revision humana (#42).

Pedir pueden admin y ventas; leer, cualquier rol del criadero. El pedido
responde 202: el borrador se genera en el worker y se consulta despues
(GET por id) hasta que sale de `generando`.

Revision (docs/dominio.md, "Aprobacion de borradores"): aprobar puede solo
admin (nunca la IA); descartar, admin y ventas. Los dos llevan
Idempotency-Key y dejan su fila en la bitacora. El PDF del aprobado es un
documento interno, nunca un comprobante fiscal.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import Identidad, require_role
from app.db import obtener_sesion
from app.idempotencia import PedidoIdempotente, pedido_idempotente, responder_una_vez
from app.paginacion import Pagina, ParametrosDePagina, parametros_de_pagina
from app.routers.documentos import obtener_firmador
from app.schemas.borradores import (
    Aprobacion,
    BorradorPedido,
    Descarte,
    RevisionRespuesta,
)
from app.schemas.documentos import DescargaPreparada
from app.services.documentos import almacenamiento
from app.services.documentos.almacenamiento import Firmador, Subidor
from app.services.ia import aprobacion, generacion
from app.services.ia.generacion import BorradorRespuesta, Cola, ColaSqs

router = APIRouter(prefix="/api/v1/documentos/borradores", tags=["borradores"])


def obtener_cola() -> Cola:
    """La cola SQS de documentos. Los tests la reemplazan."""
    return ColaSqs()


def obtener_subidor() -> Subidor:
    """El cliente de S3 que guarda el PDF aprobado. Los tests lo reemplazan."""
    return almacenamiento.subidor_s3()


@router.post("", status_code=202, response_model=BorradorRespuesta)
def pedir_borrador(
    datos: BorradorPedido,
    tareas: BackgroundTasks,
    # Antes que la sesion: un rol sin permiso no abre ninguna conexion.
    identidad: Identidad = Depends(require_role("admin", "ventas")),
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
    cola: Cola = Depends(obtener_cola),
) -> JSONResponse:
    def crear() -> BorradorRespuesta:
        borrador = generacion.pedir(
            sesion, datos.pedido_id, identidad.user_id, corrige_a=datos.corrige_a
        )
        # Despues del commit: el worker tiene que encontrar el borrador. Solo
        # la primera vez; un reintento con la misma clave no vuelve a encolar.
        tareas.add_task(generacion.encolar, cola, identidad.tenant_id, borrador.id)
        return borrador

    return responder_una_vez(sesion, pedido, crear, status_code=202)


@router.get("", response_model=Pagina[BorradorRespuesta])
def listar_borradores(
    pedido_id: UUID | None = None,
    pagina: ParametrosDePagina = Depends(parametros_de_pagina),
    sesion: Session = Depends(obtener_sesion),
) -> Pagina[BorradorRespuesta]:
    return generacion.listar(sesion, pagina, pedido_id=pedido_id)


@router.get("/{borrador_id}", response_model=BorradorRespuesta)
def obtener_borrador(
    borrador_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> BorradorRespuesta:
    return generacion.obtener(sesion, borrador_id)


@router.post("/{borrador_id}/aprobar", response_model=BorradorRespuesta)
def aprobar_borrador(
    borrador_id: UUID,
    datos: Aprobacion,
    # Solo admin: nunca la IA ni otro rol (dominio, "Aprobacion de borradores").
    identidad: Identidad = Depends(require_role("admin")),
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
    subidor: Subidor = Depends(obtener_subidor),
) -> JSONResponse:
    # Un reintento con la misma clave devuelve lo aprobado sin volver a subir.
    # La respuesta no lleva URL prefirmada: vence, y se guardaria vencida.
    return responder_una_vez(
        sesion,
        pedido,
        lambda: aprobacion.aprobar(
            sesion, borrador_id, datos, identidad.user_id, subidor
        ),
        status_code=200,
    )


@router.post("/{borrador_id}/rechazar", response_model=BorradorRespuesta)
def rechazar_borrador(
    borrador_id: UUID,
    datos: Descarte,
    identidad: Identidad = Depends(require_role("admin", "ventas")),
    pedido: PedidoIdempotente = Depends(pedido_idempotente),
    sesion: Session = Depends(obtener_sesion),
) -> JSONResponse:
    return responder_una_vez(
        sesion,
        pedido,
        lambda: aprobacion.descartar(sesion, borrador_id, datos, identidad.user_id),
        status_code=200,
    )


@router.get("/{borrador_id}/revisiones", response_model=list[RevisionRespuesta])
def revisiones_del_borrador(
    borrador_id: UUID, sesion: Session = Depends(obtener_sesion)
) -> list[RevisionRespuesta]:
    return aprobacion.revisiones(sesion, borrador_id)


@router.get("/{borrador_id}/pdf", response_model=DescargaPreparada)
def descargar_pdf(
    borrador_id: UUID,
    # true: el navegador lo muestra en la pestaña en vez de bajarlo.
    en_linea: bool = False,
    sesion: Session = Depends(obtener_sesion),
    firmador: Firmador = Depends(obtener_firmador),
) -> DescargaPreparada:
    # Una URL nueva en cada pedido: vence en SEGUNDOS_DESCARGA.
    clave = aprobacion.clave_del_pdf(sesion, borrador_id)
    return DescargaPreparada(
        url_descarga=almacenamiento.firmar_descarga(
            firmador, clave, f"borrador-{borrador_id}.pdf", en_linea=en_linea
        ),
        expira_en_segundos=almacenamiento.SEGUNDOS_DESCARGA,
    )
