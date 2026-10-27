"""Cambio de estado de un pedido: entregar y cancelar (#36, ADR 0014).

borrador -> reservado no pasa por aca: lo hace repositorio_reservas (#35).
Todo pasa en la transaccion de obtener_sesion: si algo falla, no queda ni una
celda despachada ni una reserva cerrada.

1. El pedido se lee con FOR UPDATE: un entregar y un cancelar simultaneos del
   mismo pedido corren uno despues del otro, y el segundo ve el estado nuevo.
2. puede_pasar decide si la transicion vale; si no, 409 transicion_invalida.
3. Entregar:
   - validar_despacho compara lo pedido en el cuerpo con las reservas
     activas, linea por linea (422 despacho_invalido).
   - Se toma el mismo lock por (tenant, raza) que la reserva, en el mismo
     orden: una reserva y un despacho de la misma raza no se cruzan.
   - Por cada linea y banco, las celdas enjauladas mas viejas de la raza de
     la linea pasan a despachada, con la linea y la fecha. Si el banco no
     tiene tantas, 409 stock_insuficiente_en_banco y se deshace todo.
   - Las reservas del pedido pasan a consumida. Las celdas y la reserva
     bajan juntas: el disponible no se descuenta dos veces (ADR 0014).
4. Cancelar: las reservas activas pasan a liberada (en borrador no hay).
5. El pedido cambia de estado y se devuelve como lo devuelve GET.

Como en el resto de los repositorios, ninguna consulta filtra por tenant:
RLS se encarga (ADR 0009). Un pedido de otro criadero responde 404; un banco
de otro criadero, 422 como uno inexistente.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errores import ConflictoError, NoEncontradoError, ValidacionError
from app.fechas import hoy
from app.schemas.pedidos import PedidoRespuesta
from app.schemas.transiciones import Cancelar, Entregar
from app.services.ventas import repositorio_pedidos
from app.services.ventas.repositorio_reservas import BLOQUEAR_RAZA, LEER_PEDIDO
from app.services.ventas.transiciones import puede_pasar, validar_despacho

# Las reservas activas del pedido, una por linea, con su raza y cantidad.
LEER_RESERVAS = text(
    "SELECT r.pedido_item_id, r.raza_id, r.cantidad FROM reserva r "
    "JOIN pedido_item pi ON pi.id = r.pedido_item_id "
    "WHERE pi.pedido_id = :pedido_id AND r.estado = 'activa'"
)

LEER_BANCOS = text("SELECT id FROM banco WHERE id = ANY(:ids)")

# Elige las celdas en el CTE y las actualiza en el UPDATE.
# - MATERIALIZED: el CTE se calcula una sola vez. Como subconsulta del WHERE,
#   segun el plan Postgres puede volver a ejecutarla por cada fila; cada vuelta
#   saltea las celdas que ya despacho, elige otras y el UPDATE se lleva mas
#   de :cantidad.
# - IS NOT DISTINCT FROM: con banco_id NULL compara "sin banco" (= no sirve).
# - Las mas viejas primero; el id desempata para que sea determinista.
# - FOR UPDATE OF c: bloquea las celdas elegidas, no las tandas.
# - RETURNING: si vuelven menos filas que :cantidad, el banco no alcanzaba.
DESPACHAR = text(
    "WITH elegidas AS MATERIALIZED ("
    "SELECT c.id FROM celda c JOIN tanda t ON t.id = c.tanda_id "
    "WHERE c.estado = 'enjaulada' AND t.raza_id = :raza_id "
    "AND c.banco_id IS NOT DISTINCT FROM CAST(:banco_id AS uuid) "
    "ORDER BY c.fecha_enjaulado, c.id LIMIT :cantidad FOR UPDATE OF c"
    ") "
    "UPDATE celda SET estado = 'despachada', pedido_item_id = :pedido_item_id, "
    "fecha_despacho = :fecha, actualizado_en = now() "
    "FROM elegidas WHERE celda.id = elegidas.id RETURNING celda.id"
)

# Consumir (entregar) o liberar (cancelar): las dos cierran las activas.
CERRAR_RESERVAS = text(
    "UPDATE reserva SET estado = :estado, actualizado_en = now() "
    "WHERE estado = 'activa' AND pedido_item_id IN "
    "(SELECT id FROM pedido_item WHERE pedido_id = :pedido_id)"
)

CAMBIAR_ESTADO = text(
    "UPDATE pedido SET estado = :estado, actualizado_en = now() WHERE id = :id"
)


def cambiar_estado(
    sesion: Session, pedido_id: UUID, datos: Entregar | Cancelar
) -> PedidoRespuesta:
    """Pasa el pedido a entregado o cancelado, o no cambia nada."""
    estado = sesion.execute(LEER_PEDIDO, {"id": pedido_id}).scalar_one_or_none()
    if estado is None:
        raise NoEncontradoError("No existe el pedido.", code="pedido_no_encontrado")
    if not puede_pasar(estado, datos.estado):
        raise ConflictoError(
            f"Un pedido {estado} no puede pasar a {datos.estado.value}.",
            code="transicion_invalida",
            details={"desde": estado, "hacia": datos.estado.value},
        )

    if isinstance(datos, Entregar):
        _entregar(sesion, pedido_id, datos)
    else:
        sesion.execute(
            CERRAR_RESERVAS,
            {"pedido_id": pedido_id, "estado": "liberada"},
        )

    sesion.execute(CAMBIAR_ESTADO, {"id": pedido_id, "estado": datos.estado.value})
    return repositorio_pedidos.obtener(sesion, pedido_id)


def _entregar(sesion: Session, pedido_id: UUID, datos: Entregar) -> None:
    reservas = sesion.execute(LEER_RESERVAS, {"pedido_id": pedido_id}).all()
    reservadas = {r.pedido_item_id: r.cantidad for r in reservas}
    raza_de = {r.pedido_item_id: r.raza_id for r in reservas}

    # El schema ya rechazo las lineas repetidas: una entrada por linea.
    despachadas = {
        linea.pedido_item_id: sum(salida.cantidad for salida in linea.bancos)
        for linea in datos.lineas
    }
    try:
        validar_despacho(reservadas, despachadas)
    except ValueError as error:
        raise ValidacionError(str(error), code="despacho_invalido") from error

    _validar_bancos(sesion, datos)

    # Siempre en el mismo orden que la reserva: si no, deadlock.
    for raza_id in sorted(set(raza_de.values())):
        sesion.execute(BLOQUEAR_RAZA, {"raza_id": raza_id})

    fecha = datos.fecha or hoy()
    for linea in datos.lineas:
        for salida in linea.bancos:
            _despachar(
                sesion,
                linea.pedido_item_id,
                raza_de[linea.pedido_item_id],
                salida.banco_id,
                salida.cantidad,
                fecha,
            )

    sesion.execute(CERRAR_RESERVAS, {"pedido_id": pedido_id, "estado": "consumida"})


def _validar_bancos(sesion: Session, datos: Entregar) -> None:
    """Cada banco del cuerpo existe en el criadero. None es "sin banco"."""
    pedidos = {
        salida.banco_id
        for linea in datos.lineas
        for salida in linea.bancos
        if salida.banco_id is not None
    }
    if not pedidos:
        return
    existen = set(sesion.execute(LEER_BANCOS, {"ids": list(pedidos)}).scalars().all())
    faltan = pedidos - existen
    if faltan:
        raise ValidacionError(
            "No existe el banco.",
            code="banco_no_encontrado",
            details={"banco_id": str(min(faltan))},
        )


def _despachar(
    sesion: Session,
    pedido_item_id: UUID,
    raza_id: UUID,
    banco_id: UUID | None,
    cantidad: int,
    fecha: date,
) -> None:
    """Pasa `cantidad` celdas del banco a despachada, o corta con 409."""
    despachadas = sesion.execute(
        DESPACHAR,
        {
            "pedido_item_id": pedido_item_id,
            "raza_id": raza_id,
            "banco_id": banco_id,
            "cantidad": cantidad,
            "fecha": fecha,
        },
    ).all()
    if len(despachadas) < cantidad:
        # La excepcion deshace tambien lo que ya se habia despachado.
        raise ConflictoError(
            "El banco no tiene tantas reinas enjauladas de la raza de la linea.",
            code="stock_insuficiente_en_banco",
            details={
                "pedido_item_id": str(pedido_item_id),
                "banco_id": None if banco_id is None else str(banco_id),
                "pedidas": cantidad,
                "hay": len(despachadas),
            },
        )
