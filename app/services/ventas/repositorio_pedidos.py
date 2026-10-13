"""Acceso a las tablas pedido y pedido_item (#34).

Toda funcion recibe la sesion de obtener_sesion, que ya tiene el tenant fijado:
ninguna consulta filtra por tenant_id, de eso se encarga RLS (ADR 0009). Un
cliente, item o raza de otro criadero no aparece: responde igual que uno que
no existe, asi no se puede averiguar que ids hay en otro criadero.

Los ids del cuerpo que no existen son 422, no 404: el recurso de la URL
(/api/v1/pedidos) existe, lo que esta mal es un dato del pedido.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import ValidacionError
from app.schemas.pedidos import PedidoCrear, PedidoItemRespuesta, PedidoRespuesta
from app.services.ventas.clientes import TipoCliente
from app.services.ventas.pedidos import validar_pedido

# Las consultas se escriben completas, sin armar SQL con f-strings: los valores
# viajan siempre como parametros (:nombre), nunca pegados al texto.
LEER_CLIENTE = text("SELECT tipo, activo FROM cliente WHERE id = :id")

# Uno o dos selects por pedido, no uno por linea. Los inactivos no se venden.
LEER_ITEMS_VENDIBLES = text(
    "SELECT id, alicuota FROM item_vendible WHERE activo AND id = ANY(:ids)"
)
LEER_RAZAS = text("SELECT id FROM raza WHERE activo AND id = ANY(:ids)")

# estado no se manda: lo pone el default de la columna (borrador).
INSERTAR_PEDIDO = text(
    "INSERT INTO pedido (cliente_id, moneda, destino_pais, incoterm, "
    "fecha_entrega_solicitada) "
    "VALUES (:cliente_id, :moneda, :destino_pais, :incoterm, "
    ":fecha_entrega_solicitada) "
    "RETURNING id, cliente_id, estado, moneda, destino_pais, incoterm, "
    "fecha_entrega_solicitada"
)

INSERTAR_ITEM = text(
    "INSERT INTO pedido_item (pedido_id, item_vendible_id, raza_id, cantidad, "
    "precio_unitario, alicuota) "
    "VALUES (:pedido_id, :item_vendible_id, :raza_id, :cantidad, "
    ":precio_unitario, :alicuota) "
    "RETURNING id, item_vendible_id, raza_id, cantidad, precio_unitario, alicuota"
)


def _validar_cliente(sesion: Session, datos: PedidoCrear) -> None:
    """El cliente existe, esta activo y el pedido es coherente con su tipo."""
    cliente = sesion.execute(LEER_CLIENTE, {"id": datos.cliente_id}).one_or_none()
    if cliente is None:
        raise ValidacionError("No existe el cliente.", code="cliente_inexistente")
    if not cliente.activo:
        raise ValidacionError(
            "El cliente esta dado de baja: no se le pueden tomar pedidos.",
            code="cliente_inactivo",
        )

    try:
        validar_pedido(
            TipoCliente(cliente.tipo),
            datos.moneda,
            datos.destino_pais,
            datos.incoterm,
        )
    except ValueError as error:
        raise ValidacionError(str(error), code="pedido_incoherente") from None


def _alicuotas(sesion: Session, datos: PedidoCrear) -> dict[UUID, Any]:
    """Alicuota de cada item vendible del pedido, leida del catalogo.

    Tambien verifica que existan y esten activas las razas pedidas.
    """
    ids_items = {item.item_vendible_id for item in datos.items}
    filas = sesion.execute(LEER_ITEMS_VENDIBLES, {"ids": list(ids_items)})
    alicuotas = {fila.id: fila.alicuota for fila in filas}
    faltantes = ids_items - alicuotas.keys()
    if faltantes:
        raise ValidacionError(
            "Algun item vendible no existe o esta dado de baja.",
            code="item_vendible_inexistente",
            details={"ids": sorted(str(id_) for id_ in faltantes)},
        )

    ids_razas = {item.raza_id for item in datos.items}
    encontradas = set(sesion.execute(LEER_RAZAS, {"ids": list(ids_razas)}).scalars())
    faltantes = ids_razas - encontradas
    if faltantes:
        raise ValidacionError(
            "Alguna raza no existe o esta dada de baja.",
            code="raza_inexistente",
            details={"ids": sorted(str(id_) for id_ in faltantes)},
        )

    return alicuotas


def _a_respuesta(pedido: Row[Any], items: list[Row[Any]]) -> PedidoRespuesta:
    return PedidoRespuesta(
        **pedido._mapping,
        items=[PedidoItemRespuesta.model_validate(dict(i._mapping)) for i in items],
    )


def crear(sesion: Session, datos: PedidoCrear) -> PedidoRespuesta:
    """Valida contra la base y guarda el pedido con sus lineas.

    Todo pasa en la transaccion de obtener_sesion: si una linea falla, no
    queda ni el pedido ni las lineas anteriores.
    """
    _validar_cliente(sesion, datos)
    alicuotas = _alicuotas(sesion, datos)

    pedido = sesion.execute(
        INSERTAR_PEDIDO, datos.model_dump(mode="json", exclude={"items"})
    ).one()

    items = [
        sesion.execute(
            INSERTAR_ITEM,
            {
                **item.model_dump(),
                "pedido_id": pedido.id,
                # Se copia: si el catalogo cambia, el pedido no se recalcula.
                "alicuota": alicuotas[item.item_vendible_id],
            },
        ).one()
        for item in datos.items
    ]
    return _a_respuesta(pedido, items)