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
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.errores import ErrorDeApi
from app.schemas.asistente import AccionPropuesta, ParResumen, Propuesta
from app.schemas.clientes import ClienteCrear
from app.schemas.produccion import OperacionCrear, TandaCrear
from app.services.ia.asistente import plan, resolucion
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.asistente.resolucion import ErrorDeHerramienta
from app.services.produccion import repositorio_tandas
from app.services.produccion.tandas import cupulas_para, fecha_de_traslarve_admitida
from app.services.ventas.clientes import CondicionIva, TipoCliente

# Los mismos roles que exige cada endpoint (require_role en app/routers/).
ROLES_VENTAS = ["admin", "ventas"]
ROLES_PRODUCCION = ["admin", "produccion"]

NOTA = (
    "No se ejecutó nada. El usuario la ve en pantalla y tiene que confirmarla; "
    "decíselo con tus palabras."
)

_OPERACION: TypeAdapter[Any] = TypeAdapter(OperacionCrear)


class EntradaPropuesta(BaseModel):
    model_config = ConfigDict(extra="ignore")


def validar_cuerpo(adaptador: TypeAdapter[Any], cuerpo: dict[str, Any]) -> Any:
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


def exigir(faltantes: dict[str, object]) -> None:
    """ErrorDeHerramienta con los datos obligatorios que no vinieron.

    Las entradas dejan opcionales los datos que el modelo no puede deducir,
    asi puede llamar sin inventarlos y la herramienta le dice que preguntar.
    """
    faltan = [que for que, valor in faltantes.items() if valor is None]
    if faltan:
        raise ErrorDeHerramienta(
            f"Faltan datos obligatorios: {', '.join(faltan)}. Preguntale al "
            "usuario; no los completes por tu cuenta."
        )


def volcar_cuerpo(adaptador: TypeAdapter[Any], validado: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = adaptador.dump_python(
        validado, mode="json", exclude_none=True
    )
    return cuerpo


def armar_propuesta(
    ctx: Contexto,
    accion: AccionPropuesta,
    ruta: str,
    cuerpo: dict[str, Any] | None,
    resumen: list[tuple[str, str]],
    roles: list[str],
) -> Resultado:
    if len(ctx.pendientes) >= plan.MAX_PROPUESTAS:
        raise ErrorDeHerramienta(
            f"Ya hay {plan.MAX_PROPUESTAS} propuestas en este turno, el tope. "
            "Decile al usuario que confirme estas y proponé el resto después."
        )
    depende_de = plan.dependencias(ruta, cuerpo)
    propuesta = Propuesta(
        id=uuid4(),
        accion=accion,
        ruta=ruta,
        cuerpo=cuerpo,
        resumen=[ParResumen(etiqueta=e, valor=v) for e, v in resumen],
        roles_permitidos=roles,
        paso=len(ctx.pendientes) + 1,
        depende_de=depende_de,
    )
    datos = {
        "propuesta_id": propuesta.id,
        "accion": accion,
        "paso": propuesta.paso,
        "resumen": {par.etiqueta: par.valor for par in propuesta.resumen},
        # Si no puede, el boton aparece deshabilitado: conviene avisarle.
        "el_usuario_puede_confirmarla": bool(ctx.grupos & set(roles)),
        "nota": NOTA,
    }
    if depende_de:
        pasos = {p.id: p.paso for p in ctx.pendientes}
        datos["depende_de_los_pasos"] = [pasos.get(i) for i in depende_de]
    return Resultado(datos, f"Propuse {accion.value.replace('_', ' ')}", propuesta)


def validar_con_referencias(
    adaptador: TypeAdapter[Any], cuerpo: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """Valida un cuerpo que puede traer marcadores {{ref:...}} de pasos del
    mismo turno: cada uno se cambia por un uuid de prueba para validar
    contra el schema real, y el cuerpo que vuelve los tiene de nuevo."""
    prueba, inverso = plan.con_uuids_de_prueba(cuerpo)
    validado = validar_cuerpo(adaptador, prueba)
    volcado: dict[str, Any] = plan.sustituir(
        volcar_cuerpo(adaptador, validado), inverso
    )
    return validado, volcado


# --- crear_cliente ---


class EntradaCliente(EntradaPropuesta):
    nombre: str = Field(description="Nombre o razón social.")
    pais: str = Field(description="Código ISO de 2 letras (AR para nacionales).")
    tipo: TipoCliente
    condicion_iva: CondicionIva = Field(
        description="cliente_exterior para exportación."
    )
    cuit_o_tax_id: str = Field(description="CUIT (nacional) o tax id (exterior).")


def proponer_cliente(ctx: Contexto, entrada: EntradaCliente) -> Resultado:
    adaptador = TypeAdapter(ClienteCrear)
    cliente = validar_cuerpo(adaptador, entrada.model_dump(mode="json"))
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_CLIENTE,
        "/api/v1/clientes",
        volcar_cuerpo(adaptador, cliente),
        [
            ("Nombre", cliente.nombre),
            ("País", cliente.pais),
            ("Tipo", cliente.tipo.value),
            ("Condición frente al IVA", cliente.condicion_iva.value),
            ("CUIT o tax id", cliente.cuit_o_tax_id),
        ],
        ROLES_VENTAS,
    )


# --- crear_tanda ---


class EntradaTanda(EntradaPropuesta):
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
    tanda = validar_cuerpo(
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
    return armar_propuesta(
        ctx,
        AccionPropuesta.CREAR_TANDA,
        "/api/v1/tandas",
        volcar_cuerpo(adaptador, tanda),
        resumen,
        ROLES_PRODUCCION,
    )


# --- operar_tanda ---

AccionTanda = Literal["descartar", "introducir", "cosechar", "pasar_a_banco"]


class EntradaOperacion(EntradaPropuesta):
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
    operacion = validar_cuerpo(_OPERACION, cuerpo)
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
    return armar_propuesta(
        ctx,
        AccionPropuesta.OPERAR_TANDA,
        f"/api/v1/tandas/{tanda.id}/operaciones",
        volcar_cuerpo(_OPERACION, operacion),
        resumen + extra,
        ROLES_PRODUCCION,
    )
