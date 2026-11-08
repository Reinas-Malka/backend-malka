"""Catalogo de herramientas del asistente y su ejecucion.

Cada herramienta tiene un nombre, una descripcion para el modelo, un schema
de entrada (Pydantic: de ahi sale el inputSchema de Bedrock) y la funcion
que la ejecuta. Todas corren en una transaccion propia de SOLO LECTURA con
el tenant del usuario: RLS filtra los datos y PostgreSQL rechaza cualquier
escritura, incluso por un bug. Las de escritura solo arman propuestas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from app.db import sesion_de_tenant
from app.errores import ErrorDeApi
from app.services.ia.asistente import lectura, propuestas
from app.services.ia.asistente.base import Contexto, Resultado
from app.services.ia.asistente.resolucion import ErrorDeHerramienta

# Despues de fijar el tenant: desde aca la transaccion no puede escribir.
SOLO_LECTURA = text("SET TRANSACTION READ ONLY")


@dataclass(frozen=True)
class Herramienta:
    nombre: str
    descripcion: str
    entrada: type[BaseModel]
    ejecutar: Callable[[Contexto, Any], Resultado]
    escritura: bool = False


CATALOGO: tuple[Herramienta, ...] = (
    Herramienta(
        "ver_disponibilidad",
        "Reinas enjauladas, reservadas y disponibles para vender, por raza, y en "
        "qué bancos están.",
        lectura.EntradaDisponibilidad,
        lectura.ver_disponibilidad,
    ),
    Herramienta(
        "buscar_pedidos",
        "Pedidos del más nuevo al más viejo, con su id, cliente, estado y reinas "
        "por raza. Filtros opcionales: estado y nombre del cliente.",
        lectura.EntradaBuscarPedidos,
        lectura.buscar_pedidos,
    ),
    Herramienta(
        "ver_pedido",
        "Un pedido con sus líneas, importes calculados por el sistema y reservas.",
        lectura.EntradaVerPedido,
        lectura.ver_pedido,
    ),
    Herramienta(
        "listar_tandas",
        "Tandas de cría de la más nueva a la más vieja, con su id y sus celdas "
        "por estado. Filtros opcionales: raza y rango de fechas de traslarve.",
        lectura.EntradaListarTandas,
        lectura.listar_tandas,
    ),
    Herramienta(
        "ver_tanda",
        "Una tanda con sus celdas por estado, en qué parques y bancos están.",
        lectura.EntradaVerTanda,
        lectura.ver_tanda,
    ),
    Herramienta(
        "ver_indicadores",
        "KPIs de producción calculados por el sistema: % de cosecha y % de "
        "pérdida en el parque, por tanda o por raza.",
        lectura.EntradaIndicadores,
        lectura.ver_indicadores,
    ),
    Herramienta(
        "buscar_clientes",
        "Clientes por nombre (o todos), con su id, país, tipo y condición IVA.",
        lectura.EntradaBuscarClientes,
        lectura.buscar_clientes,
    ),
    Herramienta(
        "listar_catalogo",
        "Catálogos del criadero: razas, productos (ítems vendibles), parques de "
        "fecundación, bancos o materiales.",
        lectura.EntradaCatalogo,
        lectura.listar_catalogo,
    ),
    Herramienta(
        "listar_embarques",
        "Embarques de exportación con su estado y cuántos de los 6 documentos "
        "tienen cargados.",
        lectura.EntradaEmbarques,
        lectura.listar_embarques,
    ),
    Herramienta(
        "listar_comprobantes",
        "Comprobantes ya transcriptos de ARCA (solo lectura). Filtro opcional: "
        "nombre del cliente.",
        lectura.EntradaComprobantes,
        lectura.listar_comprobantes,
    ),
    Herramienta(
        "proponer_cliente",
        "Arma la propuesta de alta de un cliente. No lo crea: el usuario confirma.",
        propuestas.EntradaCliente,
        propuestas.proponer_cliente,
        escritura=True,
    ),
    Herramienta(
        "proponer_pedido",
        "Arma la propuesta de un pedido de reinas (queda en borrador). Acepta "
        "nombres de cliente, producto y raza. No lo crea: el usuario confirma.",
        propuestas.EntradaPedido,
        propuestas.proponer_pedido,
        escritura=True,
    ),
    Herramienta(
        "proponer_reserva",
        "Arma la propuesta de reservar las reinas de un pedido en borrador. No "
        "reserva: el usuario confirma.",
        propuestas.EntradaReserva,
        propuestas.proponer_reserva,
        escritura=True,
    ),
    Herramienta(
        "proponer_tanda",
        "Arma la propuesta de alta de una tanda (raza, fecha de traslarve, "
        "cuadros). No la crea: el usuario confirma.",
        propuestas.EntradaTanda,
        propuestas.proponer_tanda,
        escritura=True,
    ),
    Herramienta(
        "proponer_operacion_tanda",
        "Arma la propuesta de una operación por lote sobre una tanda: descartar, "
        "introducir en un parque, cosechar o pasar a un banco. No la ejecuta: el "
        "usuario confirma.",
        propuestas.EntradaOperacion,
        propuestas.proponer_operacion,
        escritura=True,
    ),
    Herramienta(
        "proponer_borrador",
        "Arma la propuesta de pedirle a la IA el borrador del documento de un "
        "pedido. No lo pide: el usuario confirma.",
        propuestas.EntradaBorrador,
        propuestas.proponer_borrador,
        escritura=True,
    ),
)

POR_NOMBRE: Mapping[str, Herramienta] = {h.nombre: h for h in CATALOGO}


def _sin_titulos(schema: Any) -> Any:
    """El schema sin los "title" que agrega Pydantic: son tokens que el
    modelo no necesita (los nombres de los campos ya dicen lo mismo)."""
    if isinstance(schema, dict):
        return {
            clave: _sin_titulos(valor)
            for clave, valor in schema.items()
            if not (clave == "title" and isinstance(valor, str))
        }
    if isinstance(schema, list):
        return [_sin_titulos(valor) for valor in schema]
    return schema


def especificaciones() -> list[dict[str, Any]]:
    """El toolConfig.tools de Converse, generado desde los schemas."""
    return [
        {
            "toolSpec": {
                "name": h.nombre,
                "description": h.descripcion,
                "inputSchema": {"json": _sin_titulos(h.entrada.model_json_schema())},
            }
        }
        for h in CATALOGO
    ]


def _entrada(herramienta: Herramienta, datos: Any) -> BaseModel:
    try:
        return herramienta.entrada.model_validate(datos)
    except ValidationError as error:
        problemas = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'entrada'}: {e['msg']}"
            for e in error.errors()
        )
        raise ErrorDeHerramienta(f"Entrada inválida: {problemas}") from None


def ejecutar(
    nombre: str, datos: Any, *, tenant_id: UUID, grupos: frozenset[str]
) -> tuple[Herramienta, Resultado]:
    """Corre una herramienta, o ErrorDeHerramienta para devolverle al modelo.

    Los errores de dominio de los repositorios (404 de un id que no existe,
    422 de un periodo invalido) tambien vuelven al modelo: los puede corregir.
    Cualquier otra excepcion es un bug y sigue de largo (500).
    """
    herramienta = POR_NOMBRE.get(nombre)
    if herramienta is None:
        raise ErrorDeHerramienta(f"No existe la herramienta {nombre!r}.")
    entrada = _entrada(herramienta, datos)
    try:
        with sesion_de_tenant(tenant_id) as sesion:
            sesion.execute(SOLO_LECTURA)
            resultado = herramienta.ejecutar(Contexto(sesion, grupos), entrada)
    except ErrorDeApi as error:
        raise ErrorDeHerramienta(error.message) from None
    return herramienta, resultado
