"""Propuestas de altas de catalogos de produccion, nucleos y movimientos.

Mismo patron que propuestas.py: resuelven nombres, arman el cuerpo EXACTO
del endpoint REST, lo validan contra su schema Pydantic real y devuelven
una Propuesta. No escriben nada (corren en una transaccion de solo
lectura) y el resumen lo arma el servidor.

Lo que el endpoint rechazaria con un 409 que se puede ver antes (un nombre
repetido, un parque sin lugar) vuelve al modelo como error: asi corrige o
pregunta, en lugar de proponer algo que va a fallar al confirmar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from app.errores import ErrorDeApi
from app.schemas.asistente import AccionPropuesta, Propuesta
from app.schemas.produccion import (
    CAPACIDAD_BANCO_POR_DEFECTO,
    BancoCrear,
    ItemVendibleCrear,
    MaterialCrear,
    MovimientoCrear,
    NucleosLoteCrear,
    ParqueCrear,
    RazaCrear,
)
from app.services.ia.asistente import plan, resolucion
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.asistente.propuestas import (
    ROLES_PRODUCCION,
    EntradaPropuesta,
    armar_propuesta,
    exigir,
    validar_cuerpo,
    volcar_cuerpo,
)
from app.services.ia.asistente.resolucion import ErrorDeHerramienta, Opcion
from app.services.produccion import (
    repositorio_bancos,
    repositorio_materiales,
    repositorio_parques,
)
from app.services.produccion.bancos import PATRON_SERIE, identificador_siguiente


def _sin_repetir(
    ctx: Contexto,
    nombre: str,
    existentes: list[Opcion],
    accion: AccionPropuesta,
    que: str,
    donde: str,
) -> None:
    """El nombre no puede estar en el catalogo, ni siquiera dado de baja (el
    UNIQUE del criadero lo rechazaria con un 409 al confirmar), ni en otra
    propuesta del mismo turno."""
    repetido = resolucion.coincidencia_exacta(nombre, existentes)
    if repetido is not None:
        raise ErrorDeHerramienta(
            f'Ya existe {que} "{repetido.nombre}" en el criadero (puede estar '
            f"dado de baja: se reactiva desde la pantalla de {donde}). No se "
            "puede crear otro con el mismo nombre."
        )
    buscado = resolucion.normalizar(nombre)
    for _, otro in plan.pendientes_de(ctx.pendientes, accion):
        if resolucion.normalizar(otro) == buscado:
            raise ErrorDeHerramienta(
                f'Ya propusiste {que} "{otro}" en este turno: no lo repitas.'
            )


# --- crear_parque ---


class EntradaParque(EntradaPropuesta):
    nombre: str | None = Field(default=None, description="Nombre del parque.")
    ubicacion: str | None = Field(
        default=None, description="Dónde está (texto libre, por ejemplo el campo)."
    )
    capacidad_max: int | None = Field(
        default=None, description="Cuántos núcleos entran como máximo."
    )


def proponer_parque(ctx: Contexto, entrada: EntradaParque) -> Resultado:
    exigir(
        {
            "nombre": entrada.nombre,
            "ubicación": entrada.ubicacion,
            "capacidad máxima (en núcleos)": entrada.capacidad_max,
        }
    )
    adaptador = TypeAdapter(ParqueCrear)
    parque = validar_cuerpo(
        adaptador, entrada.model_dump(mode="json", exclude_none=True)
    )
    _sin_repetir(
        ctx,
        parque.nombre,
        resolucion.parques(ctx.sesion, incluir_inactivos=True),
        AccionPropuesta.CREAR_PARQUE,
        "un parque",
        "Parques",
    )
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_PARQUE,
        "/api/v1/parques",
        volcar_cuerpo(adaptador, parque),
        [
            ("Nombre", parque.nombre),
            ("Ubicación", parque.ubicacion),
            ("Capacidad", f"{parque.capacidad_max} núcleos"),
            ("Núcleos", "Ninguno todavía: se agregan después, por cantidad."),
        ],
        ROLES_PRODUCCION,
    )


# --- crear_nucleos ---


class EntradaNucleos(EntradaPropuesta):
    parque: str = Field(
        description="Nombre del parque, o su id si ya lo tenés (por ejemplo, "
        "del resultado de crear el parque)."
    )
    cantidad: int | None = Field(default=None, description="Núcleos a crear.")


def _como_uuid(texto: str) -> UUID | None:
    try:
        return UUID(texto.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class _Destino:
    """El parque de los nucleos: uno de la base o uno propuesto en el turno
    (entonces la ruta lleva el marcador del id que va a tener)."""

    id_en_ruta: str
    nombre: str
    capacidad_max: int
    nucleos_activos: int


def _de_la_propuesta(propuesta: Propuesta) -> _Destino:
    cuerpo = propuesta.cuerpo or {}
    return _Destino(
        plan.marcador(propuesta.id),
        str(cuerpo.get("nombre", "?")),
        int(cuerpo.get("capacidad_max", 0)),
        0,
    )


def _de_la_base(ctx: Contexto, parque_id: UUID) -> _Destino:
    try:
        parque = repositorio_parques.obtener(ctx.sesion, parque_id)
    except ErrorDeApi as error:
        raise ErrorDeHerramienta(error.message) from None
    if not parque.activo:
        raise ErrorDeHerramienta(
            f'El parque "{parque.nombre}" está dado de baja: no admite núcleos.'
        )
    return _Destino(
        str(parque.id), parque.nombre, parque.capacidad_max, parque.nucleos_activos
    )


def _parque(ctx: Contexto, texto: str) -> _Destino:
    """Por id (de la base o de una propuesta del turno) o por nombre:
    primero la base, despues lo propuesto en el turno. Si no esta en
    ninguno, el modelo tiene que proponer primero el parque."""
    parque_id = _como_uuid(texto)
    if parque_id is not None:
        propuesta = plan.pendiente(
            ctx.pendientes, parque_id, AccionPropuesta.CREAR_PARQUE
        )
        if propuesta is not None:
            return _de_la_propuesta(propuesta)
        return _de_la_base(ctx, parque_id)
    activos = resolucion.parques(ctx.sesion)
    pendientes = plan.pendientes_de(ctx.pendientes, AccionPropuesta.CREAR_PARQUE)
    buscado = resolucion.normalizar(texto)
    nombres = [o.nombre for o in activos] + [nombre for _, nombre in pendientes]
    if buscado and not any(buscado in resolucion.normalizar(n) for n in nombres):
        todos = resolucion.parques(ctx.sesion, incluir_inactivos=True)
        de_baja = resolucion.coincidencia_exacta(texto, todos)
        if de_baja is not None:
            raise ErrorDeHerramienta(
                f'El parque "{de_baja.nombre}" está dado de baja: no admite '
                "núcleos. Se reactiva desde la pantalla de Parques."
            )
        raise ErrorDeHerramienta(
            f'El parque "{texto}" todavía no existe en el criadero. Si el '
            "usuario lo quiere crear, proponé primero el parque con "
            "proponer_parque (en este mismo turno) y después los núcleos: "
            "quedan como un plan, con los núcleos dependiendo del parque."
        )
    elegido = plan.resolver(texto, activos, pendientes, "el parque")
    if elegido.pendiente is not None:
        return _de_la_propuesta(elegido.pendiente)
    return _de_la_base(ctx, UUID(elegido.id))


def proponer_nucleos(ctx: Contexto, entrada: EntradaNucleos) -> Resultado:
    parque = _parque(ctx, entrada.parque)
    exigir({"cantidad de núcleos": entrada.cantidad})
    adaptador = TypeAdapter(NucleosLoteCrear)
    lote = validar_cuerpo(adaptador, {"cantidad": entrada.cantidad})
    # La misma cuenta que hace el repositorio al crear el lote (#29).
    disponibles = parque.capacidad_max - parque.nucleos_activos
    if lote.cantidad > disponibles:
        raise ErrorDeHerramienta(
            f'En el parque "{parque.nombre}" entran {disponibles} núcleos más '
            f"(capacidad {parque.capacidad_max}, activos {parque.nucleos_activos}). "
            "Preguntale al usuario cuántos crear."
        )
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_NUCLEOS,
        f"/api/v1/parques/{parque.id_en_ruta}/nucleos",
        volcar_cuerpo(adaptador, lote),
        [
            ("Parque", parque.nombre),
            ("Núcleos a crear", str(lote.cantidad)),
            ("Ubicación", f"Una fila nueva, posiciones 1 a {lote.cantidad}"),
            (
                "Después del alta",
                f"{parque.nucleos_activos + lote.cantidad} de "
                f"{parque.capacidad_max} núcleos",
            ),
        ],
        ROLES_PRODUCCION,
    )


# --- crear_banco ---


class EntradaBanco(EntradaPropuesta):
    """Sin campos: el identificador y la capacidad los asigna el sistema."""


def proponer_banco(ctx: Contexto, entrada: EntradaBanco) -> Resultado:
    # Cuerpo vacio: el endpoint asigna el siguiente B-NN y la capacidad
    # estandar. Se valida igual contra el schema real.
    validar_cuerpo(TypeAdapter(BancoCrear), {})
    mayor = ctx.sesion.execute(
        repositorio_bancos.MAYOR_NUMERO, {"patron": PATRON_SERIE}
    ).scalar_one()
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_BANCO,
        "/api/v1/bancos",
        {},
        [
            (
                "Identificador",
                f"{identificador_siguiente(int(mayor))} (lo asigna el sistema "
                "al crearlo)",
            ),
            ("Capacidad", f"{CAPACIDAD_BANCO_POR_DEFECTO} reinas (la estándar)"),
        ],
        ROLES_PRODUCCION,
    )


# --- crear_raza ---


class EntradaRaza(EntradaPropuesta):
    nombre: str | None = Field(default=None, description="Nombre de la raza.")
    descripcion: str | None = Field(default=None, description="Opcional.")


def proponer_raza(ctx: Contexto, entrada: EntradaRaza) -> Resultado:
    exigir({"nombre de la raza": entrada.nombre})
    adaptador = TypeAdapter(RazaCrear)
    raza = validar_cuerpo(adaptador, entrada.model_dump(mode="json", exclude_none=True))
    _sin_repetir(
        ctx,
        raza.nombre,
        resolucion.razas(ctx.sesion, incluir_inactivas=True),
        AccionPropuesta.CREAR_RAZA,
        "una raza",
        "Razas",
    )
    resumen = [("Nombre", raza.nombre)]
    if raza.descripcion:
        resumen.append(("Descripción", raza.descripcion))
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_RAZA,
        "/api/v1/razas",
        volcar_cuerpo(adaptador, raza),
        resumen,
        ROLES_PRODUCCION,
    )


# --- crear_producto (item vendible; en la pantalla, "Productos") ---


class EntradaProducto(EntradaPropuesta):
    nombre: str | None = Field(
        default=None, description="Nombre del producto, por ejemplo 'Reina virgen'."
    )
    alicuota: Decimal | None = Field(
        default=None,
        description="Alícuota de IVA como fracción (0, 0.025, 0.05, 0.105, 0.21 "
        "o 0.27). Solo si el usuario la dice; sin ella, 21%.",
    )


def _porcentaje(alicuota: Decimal) -> str:
    return f"{(alicuota * 100).normalize():f}%".replace(".", ",")


def proponer_producto(ctx: Contexto, entrada: EntradaProducto) -> Resultado:
    exigir({"nombre del producto": entrada.nombre})
    adaptador = TypeAdapter(ItemVendibleCrear)
    producto = validar_cuerpo(
        adaptador, entrada.model_dump(mode="json", exclude_none=True)
    )
    _sin_repetir(
        ctx,
        producto.nombre,
        resolucion.productos(ctx.sesion, incluir_inactivos=True),
        AccionPropuesta.CREAR_PRODUCTO,
        "un producto",
        "Productos",
    )
    alicuota = Decimal(producto.alicuota)
    por_defecto = entrada.alicuota is None
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_PRODUCTO,
        "/api/v1/items-vendibles",
        volcar_cuerpo(adaptador, producto),
        [
            ("Nombre", producto.nombre),
            (
                "Alícuota de IVA",
                _porcentaje(alicuota) + (" (por defecto)" if por_defecto else ""),
            ),
            ("Qué vende", "Reinas: la raza se elige en cada línea del pedido."),
        ],
        ROLES_PRODUCCION,
    )


# --- crear_material ---


class EntradaMaterial(EntradaPropuesta):
    nombre: str | None = Field(default=None, description="Nombre del material.")
    unidad: str | None = Field(
        default=None, description="Unidad en que se cuenta (por ejemplo 'unidad')."
    )


def proponer_material(ctx: Contexto, entrada: EntradaMaterial) -> Resultado:
    exigir({"nombre del material": entrada.nombre, "unidad": entrada.unidad})
    adaptador = TypeAdapter(MaterialCrear)
    material = validar_cuerpo(
        adaptador, entrada.model_dump(mode="json", exclude_none=True)
    )
    _sin_repetir(
        ctx,
        material.nombre,
        resolucion.materiales(ctx.sesion, incluir_inactivos=True),
        AccionPropuesta.CREAR_MATERIAL,
        "un material",
        "Materiales",
    )
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_MATERIAL,
        "/api/v1/materiales",
        volcar_cuerpo(adaptador, material),
        [
            ("Nombre", material.nombre),
            ("Unidad", material.unidad),
            ("Stock inicial", "0: se suma con una compra."),
        ],
        ROLES_PRODUCCION,
    )


# --- registrar_movimiento_material ---


class EntradaMovimiento(EntradaPropuesta):
    material: str = Field(description="Nombre del material.")
    tipo: Literal["compra", "ajuste"] | None = Field(
        default=None,
        description="compra (suma stock) o ajuste (corrige, con signo y motivo). "
        "El consumo no se carga: lo registra el alta de la tanda.",
    )
    cantidad: Decimal | None = Field(
        default=None,
        description="Con signo: una compra en positivo; un ajuste, + o -.",
    )
    motivo: str | None = Field(
        default=None, description="Obligatorio en un ajuste; opcional en una compra."
    )
    fecha: date | None = Field(
        default=None, description="AAAA-MM-DD, no futura. Sin ella, hoy."
    )


def _material(ctx: Contexto, texto: str) -> tuple[str, str, str, Decimal]:
    """Id en la ruta, nombre, unidad y stock: de la base o de un material
    propuesto en el turno (que arranca en 0)."""
    elegido = plan.resolver(
        texto,
        resolucion.materiales(ctx.sesion),
        plan.pendientes_de(ctx.pendientes, AccionPropuesta.CREAR_MATERIAL),
        "el material",
    )
    if elegido.pendiente is not None:
        cuerpo = elegido.pendiente.cuerpo or {}
        return elegido.id, elegido.nombre, str(cuerpo.get("unidad", "")), Decimal(0)
    material = repositorio_materiales.obtener(ctx.sesion, UUID(elegido.id))
    return str(material.id), material.nombre, material.unidad, material.stock


def proponer_movimiento(ctx: Contexto, entrada: EntradaMovimiento) -> Resultado:
    id_en_ruta, nombre, unidad, stock = _material(ctx, entrada.material)
    exigir({"tipo (compra o ajuste)": entrada.tipo, "cantidad": entrada.cantidad})
    adaptador = TypeAdapter(MovimientoCrear)
    cuerpo: dict[str, Any] = entrada.model_dump(
        mode="json", exclude={"material"}, exclude_none=True
    )
    movimiento = validar_cuerpo(adaptador, cuerpo)
    queda = stock + movimiento.cantidad
    resumen = [
        ("Material", nombre),
        ("Tipo", movimiento.tipo),
        ("Cantidad", f"{movimiento.cantidad:+f} {unidad}"),
        (
            "Fecha",
            movimiento.fecha.isoformat() if movimiento.fecha else "hoy",
        ),
    ]
    if movimiento.motivo:
        resumen.append(("Motivo", movimiento.motivo))
    resumen.append(
        (
            "Stock",
            f"{stock:f} → {queda:f} {unidad}"
            + (" (queda negativo: se permite, con aviso)" if queda < 0 else ""),
        )
    )
    return armar_propuesta(
        ctx,
        AccionPropuesta.REGISTRAR_MOVIMIENTO_MATERIAL,
        f"/api/v1/materiales/{id_en_ruta}/movimientos",
        volcar_cuerpo(adaptador, movimiento),
        resumen,
        ROLES_PRODUCCION,
    )
