"""Reserva de las reinas de un pedido, sin sobreventa (#35, ADR 0014).

Todo pasa en la transaccion de obtener_sesion. Nunca se lee el disponible en
un request y se escribe en otro:

1. El pedido se lee con FOR UPDATE: dos reservas del mismo pedido no corren a
   la vez, y la segunda lo ve ya reservado.
2. Se toma un lock por cada raza del pedido (pg_advisory_xact_lock). Otra
   reserva de la misma raza espera a que esta confirme o se deshaga.
3. Recien con los locks tomados se calcula el disponible, con el mismo
   calculo que GET /disponibilidad: asi ve las reservas que la anterior
   acaba de confirmar.
4. Si alguna raza no alcanza, 409 stock_insuficiente y no se guarda nada.
   Si alcanzan todas, una reserva por linea y el pedido pasa a reservado.

Los locks se sueltan solos al terminar la transaccion. RLS filtra pedido,
pedido_item y reserva: un pedido de otro criadero responde 404.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, NoEncontradoError
from app.schemas.reservas import ReservaRespuesta
from app.services.ventas import repositorio_disponibilidad, repositorio_pedidos
from app.services.ventas.pedidos import EstadoPedido
from app.services.ventas.reservas import faltantes

LEER_PEDIDO = text("SELECT estado FROM pedido WHERE id = :id FOR UPDATE")

LEER_LINEAS = text(
    "SELECT id, raza_id, cantidad FROM pedido_item "
    "WHERE pedido_id = :pedido_id ORDER BY numero_linea"
)

# La clave sale de (tenant, raza): el mismo id de raza en dos criaderos no se
# bloquea entre si. El tenant se lee de la sesion, no viaja como parametro.
# hashtextextended da el bigint que pide pg_advisory_xact_lock.
BLOQUEAR_RAZA = text(
    "SELECT pg_advisory_xact_lock(hashtextextended("
    "'reserva:' || current_setting('app.tenant_id') || ':' "
    "|| CAST(:raza_id AS text), 0))"
)

# estado no se manda: lo pone el default de la columna (activa).
INSERTAR_RESERVA = text(
    "INSERT INTO reserva (pedido_item_id, raza_id, cantidad) "
    "VALUES (:pedido_item_id, :raza_id, :cantidad) "
    "RETURNING id, pedido_item_id, raza_id, cantidad, estado"
)

PASAR_A_RESERVADO = text(
    "UPDATE pedido SET estado = :estado, actualizado_en = now() WHERE id = :id"
)

# Las reservas del pedido en cualquier estado, en el orden de sus lineas.
LISTAR_RESERVAS = text(
    "SELECT r.id, r.pedido_item_id, r.raza_id, r.cantidad, r.estado "
    "FROM reserva r JOIN pedido_item pi ON pi.id = r.pedido_item_id "
    "WHERE pi.pedido_id = :pedido_id "
    "ORDER BY pi.numero_linea, r.creado_en, r.id"
)


def _disponible(sesion: Session, raza_id: UUID) -> int:
    """El disponible de una raza, con el mismo calculo que el endpoint."""
    # Vacio si la raza esta inactiva y no le quedan reinas: no hay nada.
    filas = repositorio_disponibilidad.calcular(sesion, raza_id)
    return filas[0].disponible if filas else 0


def reservar(sesion: Session, pedido_id: UUID) -> list[ReservaRespuesta]:
    """Reserva todas las lineas del pedido, o ninguna."""
    estado = sesion.execute(LEER_PEDIDO, {"id": pedido_id}).scalar_one_or_none()
    if estado is None:
        raise NoEncontradoError("No existe el pedido.", code="pedido_no_encontrado")
    if estado != EstadoPedido.BORRADOR:
        raise ConflictoError(
            "Solo se puede reservar un pedido en borrador.",
            code="pedido_no_reservable",
            details={"estado": estado},
        )

    lineas = sesion.execute(LEER_LINEAS, {"pedido_id": pedido_id}).all()
    pedidas: Counter[UUID] = Counter()
    for linea in lineas:
        pedidas[linea.raza_id] += linea.cantidad

    # Siempre en el mismo orden: si dos pedidos con las mismas razas tomaran
    # los locks en orden distinto, cada uno esperaria al otro (deadlock).
    for raza_id in sorted(pedidas):
        sesion.execute(BLOQUEAR_RAZA, {"raza_id": raza_id})

    disponibles = {raza_id: _disponible(sesion, raza_id) for raza_id in pedidas}
    faltan = faltantes(pedidas, disponibles)
    if faltan:
        raise ConflictoError(
            "No hay reinas suficientes para reservar el pedido.",
            code="stock_insuficiente",
            details={
                "razas": [
                    {
                        "raza_id": str(raza_id),
                        "pedidas": pedidas[raza_id],
                        "disponible": disponibles[raza_id],
                        "faltan": faltan[raza_id],
                    }
                    for raza_id in sorted(faltan)
                ]
            },
        )

    reservas = [
        sesion.execute(
            INSERTAR_RESERVA,
            {
                "pedido_item_id": linea.id,
                "raza_id": linea.raza_id,
                "cantidad": linea.cantidad,
            },
        ).one()
        for linea in lineas
    ]
    sesion.execute(
        PASAR_A_RESERVADO, {"id": pedido_id, "estado": EstadoPedido.RESERVADO.value}
    )
    return [ReservaRespuesta.model_validate(dict(r._mapping)) for r in reservas]


def listar(sesion: Session, pedido_id: UUID) -> list[ReservaRespuesta]:
    """Las reservas del pedido: activas, consumidas (entregado) o liberadas.

    Una por linea como mucho: un pedido se reserva una sola vez, porque solo
    se reserva desde borrador. Vacia si nunca se reservo.
    """
    repositorio_pedidos.exigir_existente(sesion, pedido_id)
    filas = sesion.execute(LISTAR_RESERVAS, {"pedido_id": pedido_id})
    return [ReservaRespuesta.model_validate(dict(r._mapping)) for r in filas]
