"""Herramientas de LECTURA del asistente sobre los borradores de documentos.

El asistente puede ver en que estado esta un borrador (generando, borrador,
rechazado, aprobado o descartado, #42) y leer su texto, pero nunca aprobarlo
ni descartarlo: la aprobacion es de una persona (docs/dominio.md). Por eso no
hay herramientas de escritura de borradores, ni siquiera como propuesta.

Lo que no le sirve para conversar queda afuera: el hash de la version, el
sub de Cognito de quien aprobo y la key del PDF.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.paginacion import ParametrosDePagina
from app.services.ia import generacion
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.generacion import BorradorRespuesta

# Los ultimos borradores de un pedido que se muestran en ver_pedido: alcanza
# para ver el vigente y sus correcciones, sin inflar los tokens.
BORRADORES_POR_PEDIDO = 5
PAGINA_DE_BORRADORES = ParametrosDePagina(limite=BORRADORES_POR_PEDIDO, cursor=None)

# El cuerpo de un borrador puede ser largo: el modelo ve hasta aca.
MAX_CARACTERES_CUERPO = 1500


class _Entrada(BaseModel):
    # Como en lectura.py: un campo de mas que agregue el modelo no corta.
    model_config = ConfigDict(extra="ignore")


def _resumen(b: BorradorRespuesta) -> dict[str, Any]:
    """Lo comun a la lista y al detalle: estado y por que termino asi."""
    return {
        "id": b.id,
        "estado": b.estado,
        "creado_en": b.creado_en,
        "motivo_rechazo": b.motivo_rechazo,
        "motivo_descarte": b.motivo_descarte,
        "corrige_a": b.corrige_a,
        "aprobado_en": b.aprobado_en,
        "pdf_disponible": b.pdf_disponible,
    }


def borradores_del_pedido(sesion: Session, pedido_id: UUID) -> dict[str, Any]:
    """Los ultimos borradores del pedido, del mas nuevo al mas viejo."""
    pagina = generacion.listar(sesion, PAGINA_DE_BORRADORES, pedido_id=pedido_id)
    return {
        "borradores": [_resumen(b) for b in pagina.items],
        "hay_mas_borradores": pagina.siguiente is not None,
    }


class EntradaVerBorrador(_Entrada):
    borrador_id: UUID = Field(
        description="Id del borrador (sale de ver_pedido, en borradores)."
    )


def ver_borrador(ctx: Contexto, entrada: EntradaVerBorrador) -> Resultado:
    b = generacion.obtener(ctx.sesion, entrada.borrador_id)
    datos = {**_resumen(b), "pedido_id": b.pedido_id, "contenido": None}
    if b.contenido is not None:
        cuerpo = b.contenido.cuerpo
        datos["contenido"] = {
            "encabezado": b.contenido.encabezado,
            "cuerpo": cuerpo[:MAX_CARACTERES_CUERPO],
            "cuerpo_recortado": len(cuerpo) > MAX_CARACTERES_CUERPO,
            "notas": b.contenido.notas,
        }
    return Resultado(datos, f"Consulté un borrador ({b.estado})")
