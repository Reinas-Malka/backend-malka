"""El asistente conversacional registra sus llamadas a la IA con su motivo.

La 0015 creo llamada_ia con el CHECK ck_llamada_ia_motivo solo para
'borrador' (#40). El asistente (POST /api/v1/asistente) usaba ese motivo
mientras tanto; con esta migracion cada llamada dice de donde vino y el
consumo se puede separar por uso.

El tope por minuto y por dia sigue siendo uno solo por criadero, compartido
entre el borrador y el asistente: reservar_llamada (app/services/ia/limites.py)
cuenta todas las filas del criadero sin mirar el motivo. Es a proposito: el
limite acota el costo de Bedrock del criadero, no de cada pantalla.

Downgrade: falla si hay llamadas con motivo 'asistente' (el CHECK de la 0015
no las admite). Es a proposito: borrar o reetiquetar el registro de consumo
no puede pasar sin que alguien lo decida a mano.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOMBRE_CHECK = "ck_llamada_ia_motivo"

# Mismos valores que app/services/ia (MOTIVO_LIMITE del borrador y del
# asistente). Se copian a proposito: una migracion no importa codigo de la app.
MOTIVOS = ("borrador", "asistente")
MOTIVOS_0015 = ("borrador",)


def _condicion(motivos: Sequence[str]) -> str:
    return "motivo IN (" + ", ".join(f"'{m}'" for m in motivos) + ")"


def upgrade() -> None:
    op.drop_constraint(NOMBRE_CHECK, "llamada_ia", type_="check")
    op.create_check_constraint(NOMBRE_CHECK, "llamada_ia", _condicion(MOTIVOS))


def downgrade() -> None:
    op.drop_constraint(NOMBRE_CHECK, "llamada_ia", type_="check")
    # Con llamadas del asistente, esto falla y no se pierde nada.
    op.create_check_constraint(NOMBRE_CHECK, "llamada_ia", _condicion(MOTIVOS_0015))
