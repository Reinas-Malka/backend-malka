"""Herramientas de ESCRITURA del asistente: arman propuestas, no ejecutan.

Cada herramienta resuelve los nombres a ids, arma el cuerpo EXACTO del
endpoint REST que hace esa escritura y lo valida contra su schema Pydantic
real. Si algo no valida, devuelve el error al modelo para que corrija o
pregunte. Si valida, devuelve una Propuesta: el front la muestra y, si el
usuario confirma, llama al endpoint normal con su token. Asi toda escritura
pasa por las mismas validaciones, roles e idempotencia.

Ninguna toca la base para escribir: las herramientas corren en una
transaccion de solo lectura (herramientas.py).

El resumen lo arma el servidor desde el cuerpo validado y los nombres
resueltos: nunca es texto del modelo.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.errores import ErrorDeApi
from app.routers.borradores import BorradorPedido
from app.schemas.asistente import AccionPropuesta, ParResumen, Propuesta
from app.schemas.clientes import ClienteCrear
from app.schemas.pedidos import PedidoCrear, PedidoRespuesta
from app.schemas.produccion import OperacionCrear, TandaCrear
from app.services.ia.asistente import resolucion
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.asistente.resolucion import ErrorDeHerramienta
from app.services.produccion import repositorio_tandas
from app.services.produccion.tandas import cupulas_para, fecha_de_traslarve_admitida
from app.services.ventas import repositorio_disponibilidad, repositorio_pedidos
from app.services.ventas.clientes import CondicionIva, TipoCliente
from app.services.ventas.pedidos import EstadoPedido, Incoterm, Moneda, validar_pedido

# Los mismos roles que exige cada endpoint (require_role en app/routers/).
ROLES_VENTAS = ["admin", "ventas"]
ROLES_PRODUCCION = ["admin", "produccion"]

NOTA = (
    "No se ejecutó nada. El usuario la ve en pantalla y tiene que confirmarla; "
    "decíselo con tus palabras."
)

_OPERACION: TypeAdapter[Any] = TypeAdapter(OperacionCrear)


class _Entrada(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _validar(adaptador: TypeAdapter[Any], cuerpo: dict[str, Any]) -> Any:
    """Valida el cuerpo como lo va a recibir el endpoint: en JSON.

    El error que vuelve al modelo dice el campo y la regla, sin el valor.
    """
    try:
        return adaptador.validate_json(json.dumps(cuerpo, default=str))
    except ValidationError as error:
        problemas = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'cuerpo'}: {e['msg']}"
            for e in error.errors()
        )
        raise ErrorDeHerramienta(f"Los datos no son válidos: {problemas}") from None


def _volcar(adaptador: TypeAdapter[Any], validado: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = adaptador.dump_python(
        validado, mode="json", exclude_none=True
    )
    return cuerpo


def _proponer(
    ctx: Contexto,
    accion: AccionPropuesta,
    ruta: str,
    cuerpo: dict[str, Any] | None,
    resumen: list[tuple[str, str]],
    roles: list[str],
) -> Resultado:
    propuesta = Propuesta(
        id=uuid4(),
        accion=accion,
        ruta=ruta,
        cuerpo=cuerpo,
        resumen=[ParResumen(etiqueta=e, valor=v) for e, v in resumen],
        roles_permitidos=roles,
    )
    datos = {
        "propuesta_id": propuesta.id,
        "accion": accion,
        "resumen": {par.etiqueta: par.valor for par in propuesta.resumen},
        # Si no puede, el boton aparece deshabilitado: conviene avisarle.
        "el_usuario_puede_confirmarla": bool(ctx.grupos & set(roles)),
        "nota": NOTA,
    }
    return Resultado(datos, f"Propuse {accion.value.replace('_', ' ')}", propuesta)


def _pedido(ctx: Contexto, pedido_id: UUID) -> PedidoRespuesta:
    try:
        return repositorio_pedidos.obtener(ctx.sesion, pedido_id)
    except ErrorDeApi as error:
        raise ErrorDeHerramienta(error.message) from None


def _etiqueta_pedido(ctx: Contexto, pedido: PedidoRespuesta) -> str:
    nombres = resolucion.nombres_de_clientes(ctx.sesion, [pedido.cliente_id])
    cliente = nombres.get(pedido.cliente_id, "?")
    return f"{cliente}, entrega {pedido.fecha_entrega_solicitada.isoformat()}"


# --- crear_cliente ---


class EntradaCliente(_Entrada):
    nombre: str = Field(description="Nombre o razón social.")
    pais: str = Field(description="Código ISO de 2 letras (AR para nacionales).")
    tipo: TipoCliente
    condicion_iva: CondicionIva = Field(
        description="cliente_exterior para exportación."
    )
    cuit_o_tax_id: str = Field(description="CUIT (nacional) o tax id (exterior).")


def proponer_cliente(ctx: Contexto, entrada: EntradaCliente) -> Resultado:
    adaptador = TypeAdapter(ClienteCrear)
    cliente = _validar(adaptador, entrada.model_dump(mode="json"))
    return _proponer(
        ctx,
        AccionPropuesta.CREAR_CLIENTE,
        "/api/v1/clientes",
        _volcar(adaptador, cliente),
        [
            ("Nombre", cliente.nombre),
            ("País", cliente.pais),
            ("Tipo", cliente.tipo.value),
            ("Condición frente al IVA", cliente.condicion_iva.value),
            ("CUIT o tax id", cliente.cuit_o_tax_id),
        ],
        ROLES_VENTAS,
    )


# --- crear_pedido ---


class LineaPropuesta(_Entrada):
    producto: str = Field(description="Nombre del ítem vendible.")
    raza: str = Field(description="Nombre de la raza.")
    cantidad: int = Field(description="Cantidad de reinas.")
    precio_unitario: Decimal = Field(
        description="Precio por reina que dijo el usuario (no hay lista de precios)."
    )


class EntradaPedido(_Entrada):
    cliente: str = Field(description="Nombre del cliente.")
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


def proponer_pedido(ctx: Contexto, entrada: EntradaPedido) -> Resultado:
    cliente = resolucion.cliente(ctx.sesion, entrada.cliente)
    tipo = TipoCliente(cliente.tipo)
    moneda = _moneda(tipo, entrada.moneda)
    productos = resolucion.productos(ctx.sesion)
    razas = resolucion.razas(ctx.sesion)
    lineas = [
        (
            resolucion.resolver(linea.producto, productos, "el producto"),
            resolucion.resolver(linea.raza, razas, "la raza"),
            linea,
        )
        for linea in entrada.items
    ]
    adaptador = TypeAdapter(PedidoCrear)
    pedido = _validar(
        adaptador,
        {
            "cliente_id": cliente.id,
            "moneda": moneda,
            "destino_pais": entrada.destino_pais,
            "incoterm": entrada.incoterm,
            "fecha_entrega_solicitada": entrada.fecha_entrega_solicitada,
            "items": [
                {
                    "item_vendible_id": producto.id,
                    "raza_id": raza.id,
                    "cantidad": linea.cantidad,
                    "precio_unitario": linea.precio_unitario,
                }
                for producto, raza, linea in lineas
            ],
        },
    )
    # La misma regla que aplica el repositorio al crear (dominio, Pedidos).
    try:
        validar_pedido(tipo, pedido.moneda, pedido.destino_pais, pedido.incoterm)
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
    return _proponer(
        ctx,
        AccionPropuesta.CREAR_PEDIDO,
        "/api/v1/pedidos",
        _volcar(adaptador, pedido),
        resumen,
        ROLES_VENTAS,
    )


# --- reservar_pedido ---


class EntradaReserva(_Entrada):
    pedido_id: UUID = Field(description="Id del pedido en borrador.")


def proponer_reserva(ctx: Contexto, entrada: EntradaReserva) -> Resultado:
    pedido = _pedido(ctx, entrada.pedido_id)
    if pedido.estado != EstadoPedido.BORRADOR:
        raise ErrorDeHerramienta(
            f"El pedido está {pedido.estado.value}: solo se reserva uno en borrador."
        )
    pedidas: dict[UUID, int] = {}
    for linea in pedido.items:
        pedidas[linea.raza_id] = pedidas.get(linea.raza_id, 0) + linea.cantidad
    disponibilidad = {
        d.raza_id: d for d in repositorio_disponibilidad.calcular(ctx.sesion)
    }
    resumen = [("Pedido", _etiqueta_pedido(ctx, pedido))]
    faltan = []
    for raza_id, cantidad in pedidas.items():
        fila = disponibilidad.get(raza_id)
        disponible = 0 if fila is None else fila.disponible
        nombre = "?" if fila is None else fila.raza
        if cantidad > disponible:
            faltan.append(f"{nombre}: pide {cantidad} y hay {disponible} disponibles")
        resumen.append(
            (f"Reinas {nombre}", f"{cantidad} (disponibles ahora: {disponible})")
        )
    if faltan:
        raise ErrorDeHerramienta(
            "No alcanza la disponibilidad para reservar: " + "; ".join(faltan) + "."
        )
    return _proponer(
        ctx,
        AccionPropuesta.RESERVAR_PEDIDO,
        f"/api/v1/pedidos/{pedido.id}/reservas",
        None,
        resumen,
        ROLES_VENTAS,
    )


# --- crear_tanda ---


class EntradaTanda(_Entrada):
    raza: str = Field(description="Nombre de la raza.")
    fecha_traslarve: date = Field(description="AAAA-MM-DD, no futura.")
    cuadros: int = Field(description="Cuadros de madera: 1, 2 o 3.")
    material: str | None = Field(
        default=None,
        description="Material de cúpulas. Si hay uno solo activo, se usa ese.",
    )
    fila_continuadora: int | None = None
    posicion_continuadora: int | None = None
    retroactivo: bool = Field(
        default=False, description="Carga tardía de una tanda ya hecha."
    )


def _material(ctx: Contexto, nombre: str | None) -> resolucion.Opcion:
    opciones = resolucion.materiales(ctx.sesion)
    if nombre is not None:
        return resolucion.resolver(nombre, opciones, "el material")
    if len(opciones) == 1:
        return opciones[0]
    nombres = ", ".join(f'"{o.nombre}"' for o in opciones) or "ninguno"
    raise ErrorDeHerramienta(
        f"Falta el material de cúpulas del que se descuenta el consumo. "
        f"Activos: {nombres}. Preguntale al usuario."
    )


def proponer_tanda(ctx: Contexto, entrada: EntradaTanda) -> Resultado:
    raza = resolucion.resolver(entrada.raza, resolucion.razas(ctx.sesion), "la raza")
    material = _material(ctx, entrada.material)
    adaptador = TypeAdapter(TandaCrear)
    tanda = _validar(
        adaptador,
        {
            "raza_id": raza.id,
            "material_id": material.id,
            "fecha_traslarve": entrada.fecha_traslarve,
            "cuadros": entrada.cuadros,
            "fila_continuadora": entrada.fila_continuadora,
            "posicion_continuadora": entrada.posicion_continuadora,
            "retroactivo": entrada.retroactivo,
        },
    )
    # La regla del domingo vive en el repositorio (tiene su propio codigo).
    if not fecha_de_traslarve_admitida(
        tanda.fecha_traslarve, retroactivo=tanda.retroactivo
    ):
        raise ErrorDeHerramienta(
            "El traslarve no se hace los domingos. Si es la carga tardía de una "
            "tanda ya hecha, tiene que ir como retroactiva: preguntale al usuario."
        )
    celdas = cupulas_para(tanda.cuadros)
    resumen = [
        ("Raza", raza.nombre),
        ("Fecha de traslarve", tanda.fecha_traslarve.isoformat()),
        ("Cuadros", str(tanda.cuadros)),
        ("Celdas a crear", str(celdas)),
        ("Material", f"{material.nombre} (se descuentan {celdas})"),
    ]
    if tanda.fila_continuadora is not None:
        resumen.append(
            (
                "Continuadora",
                f"fila {tanda.fila_continuadora}, "
                f"posición {tanda.posicion_continuadora}",
            )
        )
    if tanda.retroactivo:
        resumen.append(("Retroactiva", "sí"))
    return _proponer(
        ctx,
        AccionPropuesta.CREAR_TANDA,
        "/api/v1/tandas",
        _volcar(adaptador, tanda),
        resumen,
        ROLES_PRODUCCION,
    )


# --- operar_tanda ---

AccionTanda = Literal["descartar", "introducir", "cosechar", "pasar_a_banco"]


class EntradaOperacion(_Entrada):
    tanda_id: UUID = Field(description="Id de la tanda (sale de listar_tandas).")
    accion: AccionTanda
    cantidad: int = Field(description="Celdas o reinas del lote.")
    fecha: date | None = Field(
        default=None, description="AAAA-MM-DD. No va en pasar_a_banco."
    )
    parque: str | None = Field(default=None, description="Solo para introducir.")
    banco: str | None = Field(default=None, description="Solo para pasar_a_banco.")


def _cuerpo_operacion(
    ctx: Contexto, entrada: EntradaOperacion
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """El cuerpo de cada accion lleva solo sus campos (extra="forbid")."""
    cuerpo: dict[str, Any] = {"accion": entrada.accion, "cantidad": entrada.cantidad}
    resumen: list[tuple[str, str]] = []
    if entrada.accion != "pasar_a_banco":
        cuerpo["fecha"] = entrada.fecha
    if entrada.accion == "introducir":
        if entrada.parque is None:
            raise ErrorDeHerramienta("Falta el parque donde introducir las celdas.")
        parque = resolucion.resolver(
            entrada.parque, resolucion.parques(ctx.sesion), "el parque"
        )
        cuerpo["parque_id"] = parque.id
        resumen.append(("Parque", parque.nombre))
    if entrada.accion == "pasar_a_banco":
        if entrada.banco is None:
            raise ErrorDeHerramienta("Falta el banco al que pasar las reinas.")
        banco = resolucion.resolver(
            entrada.banco, resolucion.bancos(ctx.sesion), "el banco"
        )
        cuerpo["banco_id"] = banco.id
        resumen.append(("Banco", banco.nombre))
    return cuerpo, resumen


def proponer_operacion(ctx: Contexto, entrada: EntradaOperacion) -> Resultado:
    try:
        tanda = repositorio_tandas.obtener(ctx.sesion, entrada.tanda_id)
    except ErrorDeApi as error:
        raise ErrorDeHerramienta(error.message) from None
    cuerpo, extra = _cuerpo_operacion(ctx, entrada)
    operacion = _validar(_OPERACION, cuerpo)
    raza = {
        o.id: o.nombre for o in resolucion.razas(ctx.sesion, incluir_inactivas=True)
    }
    resumen = [
        (
            "Tanda",
            f"{raza.get(tanda.raza_id, '?')} del {tanda.fecha_traslarve.isoformat()}",
        ),
        ("Operación", operacion.accion.replace("_", " ")),
        ("Cantidad", str(operacion.cantidad)),
    ]
    if getattr(operacion, "fecha", None) is not None:
        resumen.append(("Fecha", operacion.fecha.isoformat()))
    return _proponer(
        ctx,
        AccionPropuesta.OPERAR_TANDA,
        f"/api/v1/tandas/{tanda.id}/operaciones",
        _volcar(_OPERACION, operacion),
        resumen + extra,
        ROLES_PRODUCCION,
    )


# --- pedir_borrador ---


class EntradaBorrador(_Entrada):
    pedido_id: UUID = Field(description="Id del pedido.")


def proponer_borrador(ctx: Contexto, entrada: EntradaBorrador) -> Resultado:
    pedido = _pedido(ctx, entrada.pedido_id)
    adaptador = TypeAdapter(BorradorPedido)
    cuerpo = _volcar(adaptador, _validar(adaptador, {"pedido_id": pedido.id}))
    return _proponer(
        ctx,
        AccionPropuesta.PEDIR_BORRADOR,
        "/api/v1/documentos/borradores",
        cuerpo,
        [
            ("Pedido", _etiqueta_pedido(ctx, pedido)),
            ("Qué pasa", "La IA redacta un borrador que una persona revisa y aprueba."),
        ],
        ROLES_VENTAS,
    )
