"""Disponibilidad de reinas por raza, calculada desde las celdas (#33).

Hay un solo stock: el de las celdas (ADR 0014). Nada de esto se guarda.
- enjauladas: celdas 'enjaulada' de tandas de la raza, esten o no en un banco.
- reservadas: reservas activas de la raza. En cero hasta que exista la tabla
  de reservas (#35); la reserva reusa este calculo dentro de su transaccion.
- disponible = enjauladas - reservadas.
- bancos: donde estan las enjauladas. Es ubicacion, no disponible por banco.

RLS filtra raza, tanda, celda y banco, asi que todo es del criadero de la
sesion.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.disponibilidad import DisponibilidadReinas, ExistenciaEnBanco

# Razas activas aunque no tengan reinas, y las inactivas solo si les quedan.
# El estado va en el ON y no en el WHERE: si no, el LEFT JOIN se volveria
# INNER y las razas sin reinas desaparecerian.
POR_RAZA = text(
    "SELECT r.id AS raza_id, r.nombre AS raza, count(c.id) AS enjauladas "
    "FROM raza r "
    "LEFT JOIN tanda t ON t.raza_id = r.id "
    "LEFT JOIN celda c ON c.tanda_id = t.id AND c.estado = 'enjaulada' "
    "WHERE (:todas OR r.id = CAST(:raza_id AS uuid)) "
    "GROUP BY r.id "
    "HAVING r.activo OR count(c.id) > 0 "
    "ORDER BY r.nombre, r.id"
)

POR_BANCO = text(
    "SELECT t.raza_id, c.banco_id, b.identificador, count(*) AS reinas "
    "FROM celda c "
    "JOIN tanda t ON t.id = c.tanda_id "
    "LEFT JOIN banco b ON b.id = c.banco_id "
    "WHERE c.estado = 'enjaulada' "
    "AND (:todas OR t.raza_id = CAST(:raza_id AS uuid)) "
    "GROUP BY t.raza_id, c.banco_id, b.identificador "
    "ORDER BY b.identificador NULLS LAST"
)


def calcular(
    sesion: Session, raza_id: UUID | None = None
) -> list[DisponibilidadReinas]:
    parametros = {"todas": raza_id is None, "raza_id": raza_id}

    bancos: dict[UUID, list[ExistenciaEnBanco]] = defaultdict(list)
    for fila in sesion.execute(POR_BANCO, parametros):
        bancos[fila.raza_id].append(
            ExistenciaEnBanco(
                banco_id=fila.banco_id,
                identificador=fila.identificador,
                reinas=fila.reinas,
            )
        )

    resultado = []
    for fila in sesion.execute(POR_RAZA, parametros):
        reservadas = 0  # hasta #35
        resultado.append(
            DisponibilidadReinas(
                raza_id=fila.raza_id,
                raza=fila.raza,
                enjauladas=fila.enjauladas,
                reservadas=reservadas,
                disponible=fila.enjauladas - reservadas,
                bancos=bancos[fila.raza_id],
            )
        )
    return resultado
