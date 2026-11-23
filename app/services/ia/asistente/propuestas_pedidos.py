"""Propuestas de ventas sobre pedidos: crear, reservar, pedir el borrador y
cancelar.

Mismo patron que propuestas.py. Un pedido puede ser parte de un plan del
mismo turno: su cliente, producto o raza pueden estar propuestos (todavia no
existen) y entonces el cuerpo lleva el marcador {{ref:<id>.id}} del paso que
los crea; y reservarlo o pedir su borrador puede apuntar a un pedido
propuesto en el turno (pedido_id = el propuesta_id de ese paso).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field, TypeAdapter

from app.errores import ErrorDeApi
from app.paginacion import ParametrosDePagina
from app.schemas.asistente import AccionPropuesta, Propuesta
from app.schemas.borradores import BorradorPedido
from app.schemas.pedidos import PedidoCrear, PedidoRespuesta
from app.schemas.transiciones import CambioDeEstado
from app.services.ia import generacion
from app.services.ia.asistente import plan, resolucion
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.asistente.propuestas import (
    ROLES_VENTAS,
    EntradaPropuesta,
    armar_propuesta,
    exigir,
    validar_con_referencias,
    validar_cuerpo,
    volcar_cuerpo,
)
from app.services.ia.asistente.resolucion import ErrorDeHerramienta, Opcion
from app.services.ia.generacion import EstadoBorrador
from app.services.ventas import repositorio_disponibilidad, repositorio_pedidos
from app.services.ventas.clientes import TipoCliente
from app.services.ventas.pedidos import EstadoPedido, Incoterm, Moneda, validar_pedido
from app.services.ventas.transiciones import puede_pasar

ID_DEL_PEDIDO = (
    "Id del pedido (sale de buscar_pedidos), o el propuesta_id de un "
    "crear_pedido propuesto en este mismo turno."
)


def _pedido(ctx: Contexto, pedido_id: UUID) -> PedidoRespuesta:
    try:
        return repositorio_pedidos.obtener(ctx.sesion, pedido_id)
    except ErrorDeApi as error:
        raise ErrorDeHerramienta(error.message) from None


def _etiqueta_pedido(ctx: Contexto, pedido: PedidoRespuesta) -> str:
    nombres = resolucion.nombres_de_clientes(ctx.sesion, [pedido.cliente_id])
    cliente = nombres.get(pedido.cliente_id, "?")
    return f"{cliente}, entrega {pedido.fecha_entrega_solicitada.isoformat()}"


def _etiqueta_propuesto(propuesta: Propuesta) -> str:
    cliente = plan.valor_del_resumen(propuesta, "Cliente")
    entrega = plan.valor_del_resumen(propuesta, "Entrega solicitada")
    return f"{cliente}, entrega {entrega} (el del paso {propuesta.paso})"


def _pedido_propuesto(ctx: Contexto, pedido_id: UUID) -> Propuesta | None:
    return plan.pendiente(ctx.pendientes, pedido_id, AccionPropuesta.CREAR_PEDIDO)


# --- crear_pedido ---


class LineaPropuesta(EntradaPropuesta):
    producto: str = Field(description="Nombre del ítem vendible.")
    raza: str = Field(description="Nombre de la raza.")
    cantidad: int = Field(description="Cantidad de reinas.")
    precio_unitario: Decimal = Field(
        description="Precio por reina que dijo el usuario (no hay lista de precios)."
    )


class EntradaPedido(EntradaPropuesta):
    cliente: str = Field(
        description="Nombre del cliente (uno existente o uno propuesto en este turno)."
    )
    fecha_entrega_solicitada: date = Field(description="AAAA-MM-DD.")
    items: list[LineaPropuesta] = Field(min_length=1)
    moneda: Moneda | None = Field(
        default=None,
        description="Nacional: ARS (se completa sola). Exportación: USD o EUR.",
    )
    destino_pais: str | None = Field(
        default=None, description="Solo exportación: país ISO de 2 letras."
    )
    incoterm: Incoterm | None = Field(
        default=None, description="Solo exportación, opcional."
    )


def _moneda(tipo: TipoCliente, moneda: Moneda | None) -> Moneda:
    """Nacional va en ARS; exportacion en USD o EUR, y eso lo dice el usuario."""
    if moneda is not None:
        return moneda
    if tipo == TipoCliente.NACIONAL:
        return Moneda.ARS
    raise ErrorDeHerramienta(
        "Falta la moneda: un pedido de exportación va en USD o en EUR. "
        "Preguntale al usuario."
    )


@dataclass(frozen=True)
class _Cliente:
    id: str
    nombre: str
    tipo: TipoCliente


def _cliente(ctx: Contexto, nombre: str) -> _Cliente:
    """Un cliente de la base o uno propuesto en el turno."""
    filas = resolucion.buscar_clientes(ctx.sesion, nombre)
    elegido = plan.resolver(
        nombre,
        [Opcion(f.id, f.nombre) for f in filas],
        plan.pendientes_de(ctx.pendientes, AccionPropuesta.CREAR_CLIENTE),
        "el cliente",
    )
    if elegido.pendiente is not None:
        tipo = str((elegido.pendiente.cuerpo or {}).get("tipo", ""))
        return _Cliente(elegido.id, elegido.nombre, TipoCliente(tipo))
    fila = next(f for f in filas if str(f.id) == elegido.id)
    return _Cliente(elegido.id, fila.nombre, TipoCliente(fila.tipo))


def proponer_pedido(ctx: Contexto, entrada: EntradaPedido) -> Resultado:
    cliente = _cliente(ctx, entrada.cliente)
    moneda = _moneda(cliente.tipo, entrada.moneda)
    productos = resolucion.productos(ctx.sesion)
    razas = resolucion.razas(ctx.sesion)
    productos_del_turno = plan.pendientes_de(
        ctx.pendientes, AccionPropuesta.CREAR_PRODUCTO
    )
    razas_del_turno = plan.pendientes_de(ctx.pendientes, AccionPropuesta.CREAR_RAZA)
    lineas = [
        (
            plan.resolver(
                linea.producto, productos, productos_del_turno, "el producto"
            ),
            plan.resolver(linea.raza, razas, razas_del_turno, "la raza"),
            linea,
        )
        for linea in entrada.items
    ]
    adaptador = TypeAdapter(PedidoCrear)
    pedido, cuerpo = validar_con_referencias(
        adaptador,
        {
            "cliente_id": cliente.id,
            "moneda": moneda.value,
            "destino_pais": entrada.destino_pais,
            "incoterm": None if entrada.incoterm is None else entrada.incoterm.value,
            "fecha_entrega_solicitada": entrada.fecha_entrega_solicitada.isoformat(),
            "items": [
                {
                    "item_vendible_id": producto.id,
                    "raza_id": raza.id,
                    "cantidad": linea.cantidad,
                    "precio_unitario": str(linea.precio_unitario),
                }
                for producto, raza, linea in lineas
            ],
        },
    )
    # La misma regla que aplica el repositorio al crear (dominio, Pedidos).
    try:
        validar_pedido(
            cliente.tipo, pedido.moneda, pedido.destino_pais, pedido.incoterm
        )
    except ValueError as error:
        raise ErrorDeHerramienta(str(error)) from None

    resumen = [("Cliente", cliente.nombre), ("Moneda", pedido.moneda.value)]
    if pedido.destino_pais:
        resumen.append(("Destino", pedido.destino_pais))
    if pedido.incoterm:
        resumen.append(("Incoterm", pedido.incoterm.value))
    resumen.append(("Entrega solicitada", pedido.fecha_entrega_solicitada.isoformat()))
    for numero, ((producto, raza, _), item) in enumerate(
        zip(lineas, pedido.items, strict=True), start=1
    ):
        resumen.append(
            (
                f"Línea {numero}",
                f"{item.cantidad} reinas {raza.nombre} ({producto.nombre}) a "
                f"{item.precio_unitario} {pedido.moneda.value} c/u",
            )
        )
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_PEDIDO,
        "/api/v1/pedidos",
        cuerpo,
        resumen,
        ROLES_VENTAS,
    )


# --- reservar_pedido ---


class EntradaReserva(EntradaPropuesta):
    pedido_id: UUID = Field(description=ID_DEL_PEDIDO + " Tiene que estar en borrador.")


def _reinas_por_raza(items: list[Any]) -> dict[str, int]:
    pedidas: dict[str, int] = {}
    for linea in items:
        pedidas[linea["raza_id"]] = pedidas.get(linea["raza_id"], 0) + int(
            linea["cantidad"]
        )
    return pedidas


def proponer_reserva(ctx: Contexto, entrada: EntradaReserva) -> Resultado:
    propuesto = _pedido_propuesto(ctx, entrada.pedido_id)
    if propuesto is not None:
        # Un pedido nuevo nace en borrador: se reserva despues de crearlo.
        etiqueta = _etiqueta_propuesto(propuesto)
        items = list((propuesto.cuerpo or {}).get("items", []))
        ruta_id = plan.marcador(propuesto.id)
    else:
        pedido = _pedido(ctx, entrada.pedido_id)
        if pedido.estado != EstadoPedido.BORRADOR:
            raise ErrorDeHerramienta(
                f"El pedido está {pedido.estado.value}: solo se reserva uno en "
                "borrador."
            )
        etiqueta = _etiqueta_pedido(ctx, pedido)
        items = [
            {"raza_id": str(linea.raza_id), "cantidad": linea.cantidad}
            for linea in pedido.items
        ]
        ruta_id = str(pedido.id)
    disponibilidad = {
        str(d.raza_id): d for d in repositorio_disponibilidad.calcular(ctx.sesion)
    }
    resumen = [("Pedido", etiqueta)]
    faltan = []
    for raza_id, cantidad in _reinas_por_raza(items).items():
        fila = disponibilidad.get(raza_id)
        disponible = 0 if fila is None else fila.disponible
        nombre = "nueva" if fila is None else fila.raza
        if cantidad > disponible:
            faltan.append(f"{nombre}: pide {cantidad} y hay {disponible} disponibles")
        resumen.append(
            (f"Reinas {nombre}", f"{cantidad} (disponibles ahora: {disponible})")
        )
    if faltan:
        raise ErrorDeHerramienta(
            "No alcanza la disponibilidad para reservar: " + "; ".join(faltan) + "."
        )
    return armar_propuesta(
        ctx,
        AccionPropuesta.RESERVAR_PEDIDO,
        f"/api/v1/pedidos/{ruta_id}/reservas",
        None,
        resumen,
        ROLES_VENTAS,
    )


# --- pedir_borrador ---


class EntradaBorrador(EntradaPropuesta):
    pedido_id: UUID = Field(description=ID_DEL_PEDIDO)
    corrige_a: UUID | None = Field(
        default=None,
        description="Solo para corregir: id del borrador APROBADO del mismo "
        "pedido (sale de ver_pedido). El aprobado no cambia.",
    )


def _corregido(ctx: Contexto, pedido_id: UUID, borrador_id: UUID) -> None:
    """Lo mismo que valida el endpoint (generacion._validar_correccion),
    antes de proponer: asi el modelo corrige sin que el usuario confirme algo
    que va a fallar."""
    try:
        corregido = generacion.obtener(ctx.sesion, borrador_id)
    except ErrorDeApi:
        corregido = None
    if (
        corregido is None
        or corregido.pedido_id != pedido_id
        or corregido.estado != EstadoBorrador.APROBADO
    ):
        raise ErrorDeHerramienta(
            "Solo se corrige un borrador aprobado del mismo pedido."
        )


def proponer_borrador(ctx: Contexto, entrada: EntradaBorrador) -> Resultado:
    propuesto = _pedido_propuesto(ctx, entrada.pedido_id)
    if propuesto is not None:
        if entrada.corrige_a is not None:
            raise ErrorDeHerramienta(
                "Un pedido que todavía no existe no tiene borradores para corregir."
            )
        pedido_id, etiqueta = (
            plan.marcador(propuesto.id),
            _etiqueta_propuesto(propuesto),
        )
    else:
        pedido = _pedido(ctx, entrada.pedido_id)
        if entrada.corrige_a is not None:
            _corregido(ctx, pedido.id, entrada.corrige_a)
        pedido_id, etiqueta = str(pedido.id), _etiqueta_pedido(ctx, pedido)
    corrige_a = None if entrada.corrige_a is None else str(entrada.corrige_a)
    _, cuerpo = validar_con_referencias(
        TypeAdapter(BorradorPedido), {"pedido_id": pedido_id, "corrige_a": corrige_a}
    )
    resumen = [("Pedido", etiqueta)]
    if entrada.corrige_a is not None:
        resumen.append(("Corrige", "El borrador aprobado del pedido (no cambia)"))
    resumen.append(
        ("Qué pasa", "La IA redacta un borrador que una persona revisa y aprueba.")
    )
    return armar_propuesta(
        ctx,
        AccionPropuesta.PEDIR_BORRADOR,
        "/api/v1/documentos/borradores",
        cuerpo,
        resumen,
        ROLES_VENTAS,
    )


# --- cancelar_pedido ---

_CAMBIO_DE_ESTADO: TypeAdapter[Any] = TypeAdapter(CambioDeEstado)
CANCELABLES = (EstadoPedido.BORRADOR, EstadoPedido.RESERVADO)
# Pedidos de un cliente que se miran para resolver "el pedido de X".
PAGINA_PEDIDOS = ParametrosDePagina(limite=50, cursor=None)


class EntradaCancelacion(EntradaPropuesta):
    pedido_id: UUID | None = Field(
        default=None, description="Id del pedido (sale de buscar_pedidos)."
    )
    cliente: str | None = Field(
        default=None,
        description="Nombre del cliente, si el usuario no dio el pedido: sirve "
        "cuando tiene un solo pedido en borrador o reservado.",
    )


def _pedido_a_cancelar(ctx: Contexto, entrada: EntradaCancelacion) -> PedidoRespuesta:
    """El pedido por id o, si el cliente tiene uno solo cancelable, ese."""
    if entrada.pedido_id is not None:
        return _pedido(ctx, entrada.pedido_id)
    exigir({"el pedido (o el cliente del pedido)": entrada.cliente})
    assert entrada.cliente is not None
    cliente = resolucion.cliente(ctx.sesion, entrada.cliente, incluir_inactivos=True)
    pagina = repositorio_pedidos.listar(
        ctx.sesion, PAGINA_PEDIDOS, cliente_id=cliente.id
    )
    cancelables = [p for p in pagina.items if p.estado in CANCELABLES]
    if len(cancelables) == 1:
        return cancelables[0]
    if not cancelables:
        raise ErrorDeHerramienta(
            f"{cliente.nombre} no tiene pedidos en borrador ni reservados: no "
            "hay nada que cancelar."
        )
    opciones = "; ".join(
        f"id {p.id} ({p.estado.value}, entrega "
        f"{p.fecha_entrega_solicitada.isoformat()})"
        for p in cancelables[: resolucion.MAX_OPCIONES_EN_ERROR]
    )
    raise ErrorDeHerramienta(
        f"{cliente.nombre} tiene {len(cancelables)} pedidos que se pueden "
        f"cancelar: {opciones}. Preguntale al usuario cuál."
    )


def proponer_cancelacion(ctx: Contexto, entrada: EntradaCancelacion) -> Resultado:
    pedido = _pedido_a_cancelar(ctx, entrada)
    # La misma regla que aplica el repositorio al cambiar el estado (#36).
    if not puede_pasar(pedido.estado, EstadoPedido.CANCELADO):
        raise ErrorDeHerramienta(
            f"El pedido está {pedido.estado.value}: solo se cancela uno en "
            "borrador o reservado."
        )
    cuerpo = volcar_cuerpo(
        _CAMBIO_DE_ESTADO,
        validar_cuerpo(_CAMBIO_DE_ESTADO, {"estado": EstadoPedido.CANCELADO}),
    )
    reinas = sum(linea.cantidad for linea in pedido.items)
    resumen = [
        ("Pedido", _etiqueta_pedido(ctx, pedido)),
        ("Estado actual", pedido.estado.value),
        ("Reinas del pedido", str(reinas)),
    ]
    if pedido.estado == EstadoPedido.RESERVADO:
        resumen.append(
            ("Reservas", "Se liberan: esas reinas vuelven a estar disponibles.")
        )
    resumen.append(
        (
            "Atención",
            "Cancelar es final: el pedido no se puede reactivar ni editar después.",
        )
    )
    return armar_propuesta(
        ctx,
        AccionPropuesta.CANCELAR_PEDIDO,
        f"/api/v1/pedidos/{pedido.id}/estado",
        cuerpo,
        resumen,
        ROLES_VENTAS,
    )
