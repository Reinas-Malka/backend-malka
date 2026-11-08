"""Herramientas de LECTURA del asistente.

Corren en el servidor con la sesion del tenant del usuario (RLS) y llaman a
los mismos repositorios que los endpoints GET, no a la API por HTTP. Cada
una devuelve un resumen compacto (hasta MAX_FILAS filas, sin CUIT ni datos
que el asistente no necesita) para no inflar los tokens.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.schemas.kpis import Agrupacion
from app.services.exportacion import repositorio_embarques
from app.services.exportacion.embarques import EstadoEmbarque, TipoDocumentoEmbarque
from app.services.facturacion import repositorio_comprobantes
from app.services.ia.asistente import resolucion
from app.services.ia.asistente.base import MAX_FILAS, PAGINA_CORTA, Contexto, Resultado
from app.services.produccion import (
    repositorio_bancos,
    repositorio_items_vendibles,
    repositorio_kpis,
    repositorio_materiales,
    repositorio_parques,
    repositorio_razas,
    repositorio_tandas,
)
from app.services.ventas import (
    repositorio_disponibilidad,
    repositorio_pedidos,
    repositorio_reservas,
)
from app.services.ventas.pedidos import EstadoPedido

TOTAL_DOCUMENTOS = len(TipoDocumentoEmbarque)


class _Entrada(BaseModel):
    # "ignore": un campo de mas que agregue el modelo no corta la conversacion.
    model_config = ConfigDict(extra="ignore")


# --- nombres para mostrar (los ids solos no le dicen nada al usuario) ---


def _nombres_de_razas(sesion: Session) -> dict[UUID, str]:
    return {o.id: o.nombre for o in resolucion.razas(sesion, incluir_inactivas=True)}


def _nombres_de_productos(sesion: Session) -> dict[UUID, str]:
    pagina = repositorio_items_vendibles.listar(
        sesion, resolucion.PAGINA_CATALOGO, incluir_inactivos=True
    )
    return {item.id: item.nombre for item in pagina.items}


def _nombres_de_parques(sesion: Session) -> dict[UUID, str]:
    pagina = repositorio_parques.listar(
        sesion, resolucion.PAGINA_CATALOGO, incluir_inactivos=True
    )
    return {parque.id: parque.nombre for parque in pagina.items}


def _raza_opcional(sesion: Session, raza: str | None) -> resolucion.Opcion | None:
    if raza is None:
        return None
    return resolucion.resolver(
        raza, resolucion.razas(sesion, incluir_inactivas=True), "la raza"
    )


def _sufijo(*partes: str | None) -> str:
    texto = ", ".join(p for p in partes if p)
    return f" ({texto})" if texto else ""


# --- disponibilidad ---


class EntradaDisponibilidad(_Entrada):
    raza: str | None = Field(
        default=None, description="Nombre de la raza. Sin raza, todas."
    )


def ver_disponibilidad(ctx: Contexto, entrada: EntradaDisponibilidad) -> Resultado:
    raza = _raza_opcional(ctx.sesion, entrada.raza)
    filas = repositorio_disponibilidad.calcular(
        ctx.sesion, None if raza is None else raza.id
    )
    datos = [
        {
            "raza": f.raza,
            "enjauladas": f.enjauladas,
            "reservadas": f.reservadas,
            "disponible": f.disponible,
            "bancos": [
                {"banco": b.identificador or "sin banco", "reinas": b.reinas}
                for b in f.bancos
            ],
        }
        for f in filas
    ]
    nombre = None if raza is None else raza.nombre
    return Resultado(datos, f"Consulté la disponibilidad{_sufijo(nombre)}")


# --- pedidos ---


class EntradaBuscarPedidos(_Entrada):
    estado: EstadoPedido | None = Field(default=None, description="Estado del pedido.")
    cliente: str | None = Field(default=None, description="Nombre del cliente.")


def buscar_pedidos(ctx: Contexto, entrada: EntradaBuscarPedidos) -> Resultado:
    cliente = (
        None
        if entrada.cliente is None
        else resolucion.cliente(ctx.sesion, entrada.cliente, incluir_inactivos=True)
    )
    pagina = repositorio_pedidos.listar(
        ctx.sesion,
        PAGINA_CORTA,
        estado=entrada.estado,
        cliente_id=None if cliente is None else cliente.id,
    )
    clientes = resolucion.nombres_de_clientes(
        ctx.sesion, (p.cliente_id for p in pagina.items)
    )
    razas = _nombres_de_razas(ctx.sesion)
    pedidos = []
    for p in pagina.items:
        por_raza: dict[str, int] = {}
        for linea in p.items:
            nombre = razas.get(linea.raza_id, "?")
            por_raza[nombre] = por_raza.get(nombre, 0) + linea.cantidad
        pedidos.append(
            {
                "id": p.id,
                "cliente": clientes.get(p.cliente_id, "?"),
                "estado": p.estado,
                "moneda": p.moneda,
                "destino_pais": p.destino_pais,
                "fecha_entrega_solicitada": p.fecha_entrega_solicitada,
                "reinas_por_raza": por_raza,
                "total": p.total,
            }
        )
    resumen = "Consulté los pedidos" + _sufijo(
        None if entrada.estado is None else str(entrada.estado),
        None if cliente is None else cliente.nombre,
    )
    return Resultado(
        {"pedidos": pedidos, "hay_mas": pagina.siguiente is not None}, resumen
    )


class EntradaVerPedido(_Entrada):
    pedido_id: UUID = Field(description="Id del pedido (sale de buscar_pedidos).")


def ver_pedido(ctx: Contexto, entrada: EntradaVerPedido) -> Resultado:
    p = repositorio_pedidos.obtener(ctx.sesion, entrada.pedido_id)
    cliente = resolucion.nombres_de_clientes(ctx.sesion, [p.cliente_id]).get(
        p.cliente_id, "?"
    )
    razas = _nombres_de_razas(ctx.sesion)
    productos = _nombres_de_productos(ctx.sesion)
    reservas = repositorio_reservas.listar(ctx.sesion, p.id)
    datos = {
        "id": p.id,
        "cliente": cliente,
        "estado": p.estado,
        "moneda": p.moneda,
        "destino_pais": p.destino_pais,
        "incoterm": p.incoterm,
        "fecha_entrega_solicitada": p.fecha_entrega_solicitada,
        "lineas": [
            {
                "producto": productos.get(linea.item_vendible_id, "?"),
                "raza": razas.get(linea.raza_id, "?"),
                "cantidad": linea.cantidad,
                "precio_unitario": linea.precio_unitario,
                "neto": linea.neto,
            }
            for linea in p.items
        ],
        "neto": p.neto,
        "iva": p.iva,
        "total": p.total,
        "reservas": [
            {
                "raza": razas.get(r.raza_id, "?"),
                "cantidad": r.cantidad,
                "estado": r.estado,
            }
            for r in reservas
        ],
    }
    return Resultado(datos, f"Consulté el pedido de {cliente}")


# --- tandas ---


class EntradaListarTandas(_Entrada):
    raza: str | None = Field(default=None, description="Nombre de la raza.")
    desde: date | None = Field(
        default=None, description="Fecha de traslarve mínima (AAAA-MM-DD)."
    )
    hasta: date | None = Field(
        default=None, description="Fecha de traslarve máxima (AAAA-MM-DD)."
    )


def listar_tandas(ctx: Contexto, entrada: EntradaListarTandas) -> Resultado:
    raza = _raza_opcional(ctx.sesion, entrada.raza)
    pagina = repositorio_tandas.listar(
        ctx.sesion,
        PAGINA_CORTA,
        raza_id=None if raza is None else raza.id,
        desde=entrada.desde,
        hasta=entrada.hasta,
    )
    razas = _nombres_de_razas(ctx.sesion)
    tandas = [
        {
            "id": t.id,
            "raza": razas.get(t.raza_id, "?"),
            "fecha_traslarve": t.fecha_traslarve,
            "retroactivo": t.retroactivo,
            "celdas": t.celdas,
            "celdas_por_estado": t.celdas_por_estado,
        }
        for t in pagina.items
    ]
    resumen = "Consulté las tandas" + _sufijo(None if raza is None else raza.nombre)
    return Resultado(
        {"tandas": tandas, "hay_mas": pagina.siguiente is not None}, resumen
    )


class EntradaVerTanda(_Entrada):
    tanda_id: UUID = Field(description="Id de la tanda (sale de listar_tandas).")


def ver_tanda(ctx: Contexto, entrada: EntradaVerTanda) -> Resultado:
    t = repositorio_tandas.detalle(ctx.sesion, entrada.tanda_id)
    raza = _nombres_de_razas(ctx.sesion).get(t.raza_id, "?")
    parques = _nombres_de_parques(ctx.sesion)
    por_parque: dict[str, dict[str, int]] = {}
    for n in t.nucleos:
        grupo = por_parque.setdefault(
            parques.get(n.parque_id, "?"), {"nucleos": 0, "celdas": 0}
        )
        grupo["nucleos"] += 1
        grupo["celdas"] += n.celdas
    datos = {
        "id": t.id,
        "raza": raza,
        "fecha_traslarve": t.fecha_traslarve,
        "retroactivo": t.retroactivo,
        "celdas": t.celdas,
        "celdas_por_estado": t.celdas_por_estado,
        "en_parques": [{"parque": nombre, **g} for nombre, g in por_parque.items()],
        "bancos": [{"banco": b.identificador, "reinas": b.reinas} for b in t.bancos],
    }
    return Resultado(datos, f"Consulté la tanda de {raza} del {t.fecha_traslarve}")


# --- indicadores ---


class EntradaIndicadores(_Entrada):
    agrupar: Agrupacion = Field(default="tanda", description="Por tanda o por raza.")
    desde: date | None = Field(
        default=None, description="Fecha de traslarve mínima (AAAA-MM-DD)."
    )
    hasta: date | None = Field(
        default=None, description="Fecha de traslarve máxima (AAAA-MM-DD)."
    )


def ver_indicadores(ctx: Contexto, entrada: EntradaIndicadores) -> Resultado:
    filas = repositorio_kpis.calcular(
        ctx.sesion, entrada.agrupar, entrada.desde, entrada.hasta
    )
    # Por raza, tanda_id y fecha_traslarve vienen en null: no aportan nada.
    sobran = {"raza_id"} | (
        {"tanda_id", "fecha_traslarve"} if entrada.agrupar == "raza" else set()
    )
    indicadores = [fila.model_dump(exclude=sobran) for fila in filas[:MAX_FILAS]]
    return Resultado(
        {"indicadores": indicadores, "hay_mas": len(filas) > MAX_FILAS},
        f"Consulté los indicadores de producción por {entrada.agrupar}",
    )


# --- clientes ---


class EntradaBuscarClientes(_Entrada):
    nombre: str | None = Field(
        default=None, description="Parte del nombre. Sin nombre, todos."
    )
    incluir_inactivos: bool = Field(
        default=False, description="Sumar los clientes dados de baja."
    )


def buscar_clientes(ctx: Contexto, entrada: EntradaBuscarClientes) -> Resultado:
    # Sin CUIT: el asistente no lo necesita para conversar.
    filas = resolucion.buscar_clientes(
        ctx.sesion,
        entrada.nombre,
        incluir_inactivos=entrada.incluir_inactivos,
        limite=MAX_FILAS + 1,
    )
    clientes = [
        {
            "id": f.id,
            "nombre": f.nombre,
            "pais": f.pais,
            "tipo": f.tipo,
            "condicion_iva": f.condicion_iva,
            "activo": f.activo,
        }
        for f in filas[:MAX_FILAS]
    ]
    return Resultado(
        {"clientes": clientes, "hay_mas": len(filas) > MAX_FILAS},
        "Consulté los clientes" + _sufijo(entrada.nombre),
    )


# --- catalogos ---

Catalogo = Literal["razas", "productos", "parques", "bancos", "materiales"]


class EntradaCatalogo(_Entrada):
    catalogo: Catalogo = Field(
        description="razas, productos (ítems vendibles), parques de fecundación, "
        "bancos o materiales."
    )
    incluir_inactivos: bool = Field(
        default=False, description="Sumar los dados de baja."
    )


_LISTADOS: dict[str, Any] = {
    "razas": repositorio_razas.listar,
    "productos": repositorio_items_vendibles.listar,
    "parques": repositorio_parques.listar,
    "bancos": repositorio_bancos.listar,
    "materiales": repositorio_materiales.listar,
}


def listar_catalogo(ctx: Contexto, entrada: EntradaCatalogo) -> Resultado:
    pagina = _LISTADOS[entrada.catalogo](
        ctx.sesion, PAGINA_CORTA, incluir_inactivos=entrada.incluir_inactivos
    )
    return Resultado(
        {
            entrada.catalogo: [item.model_dump() for item in pagina.items],
            "hay_mas": pagina.siguiente is not None,
        },
        f"Consulté el catálogo de {entrada.catalogo}",
    )


# --- embarques y comprobantes ---


class EntradaEmbarques(_Entrada):
    estado: EstadoEmbarque | None = Field(
        default=None, description="Estado del embarque."
    )


def listar_embarques(ctx: Contexto, entrada: EntradaEmbarques) -> Resultado:
    pagina = repositorio_embarques.listar(
        ctx.sesion, PAGINA_CORTA, estado=entrada.estado
    )
    embarques = [
        {
            "id": e.id,
            "destino_pais": e.destino_pais,
            "fecha_vuelo": e.fecha_vuelo,
            "awb": e.awb,
            "estado": e.estado,
            "documentos": f"{e.documentos_cargados}/{TOTAL_DOCUMENTOS}",
        }
        for e in pagina.items
    ]
    return Resultado(
        {"embarques": embarques, "hay_mas": pagina.siguiente is not None},
        "Consulté los embarques"
        + _sufijo(None if entrada.estado is None else str(entrada.estado)),
    )


class EntradaComprobantes(_Entrada):
    cliente: str | None = Field(default=None, description="Nombre del cliente.")


def listar_comprobantes(ctx: Contexto, entrada: EntradaComprobantes) -> Resultado:
    cliente = (
        None
        if entrada.cliente is None
        else resolucion.cliente(ctx.sesion, entrada.cliente, incluir_inactivos=True)
    )
    pagina = repositorio_comprobantes.listar(
        ctx.sesion, PAGINA_CORTA, cliente_id=None if cliente is None else cliente.id
    )
    clientes = resolucion.nombres_de_clientes(
        ctx.sesion, (c.cliente_id for c in pagina.items)
    )
    # Tal como se transcribieron de ARCA: el asistente los lee, no los genera.
    comprobantes = [
        {
            "id": c.id,
            "tipo": c.tipo,
            "cliente": clientes.get(c.cliente_id, "?"),
            "punto_venta": c.punto_venta,
            "numero": c.numero,
            "fecha_emision": c.fecha_emision,
            "moneda": c.moneda,
            "importe": c.importe,
        }
        for c in pagina.items
    ]
    return Resultado(
        {"comprobantes": comprobantes, "hay_mas": pagina.siguiente is not None},
        "Consulté los comprobantes"
        + _sufijo(None if cliente is None else cliente.nombre),
    )
