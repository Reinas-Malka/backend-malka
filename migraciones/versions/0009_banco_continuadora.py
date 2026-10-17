"""Banco, continuadora y fechas de perdida de la celda (dominio 16/10).

Reglas en docs/dominio.md (confirmado 16/10) y ADR 0014:
- El banco vuelve a la fase 1: guarda reinas enjauladas listas para despacho.
  Su existencia se cuenta desde las celdas (celda.banco_id), nunca se guarda.
- La ubicacion de los primeros 10 dias va en la tanda (fila y posicion de la
  continuadora), sin tablas para iniciadoras ni continuadoras.
- Las madres quedan fuera de alcance: tanda.madre_id pasa a ser opcional.
- La celda guarda en que banco se guardo y la fecha de su perdida
  (descartada o extraviada).

Todo es aditivo salvo el DROP NOT NULL de tanda.madre_id. Los CHECK nuevos
validan las filas existentes: tanda y celda no tienen endpoints todavia
(#27, #28), asi que estan vacias. Si hubiera datos que no cumplen, la
migracion falla entera dentro de su transaccion y no deja nada a medias.

El downgrade vuelve madre_id a NOT NULL: falla si ya hay tandas sin madre.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"

# Sin DELETE: un banco se da de baja (activo = false), como el parque.
PERMISOS_BANCO = "SELECT, INSERT, UPDATE"

# Capacidad por defecto confirmada por la clienta el 16/10, en reinas.
CAPACIDAD_BANCO_POR_DEFECTO = 276

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant_id() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _creado_en() -> sa.Column:
    return sa.Column(
        "creado_en",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _actualizado_en() -> sa.Column:
    return sa.Column(
        "actualizado_en",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _aislar_por_tenant(tabla: str, permisos: str) -> None:
    """Aplica a una tabla de negocio la convencion de la #19."""
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    # FORCE: las politicas se aplican tambien al duenio de la tabla.
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY aislamiento_por_tenant ON {tabla} "
        f"USING (tenant_id = {TENANT_ACTUAL}) "
        f"WITH CHECK (tenant_id = {TENANT_ACTUAL})"
    )
    op.execute(f"GRANT {permisos} ON {tabla} TO {ROL_APP}")
    # Sin SET LOCAL el default es NULL y el NOT NULL rechaza el insert.
    op.execute(
        f"ALTER TABLE {tabla} ALTER COLUMN tenant_id SET DEFAULT {TENANT_ACTUAL}"
    )


def upgrade() -> None:
    # --- banco: reinas enjauladas listas para despacho (no es el parque) ---
    op.create_table(
        "banco",
        _id(),
        _tenant_id(),
        sa.Column("identificador", sa.Text(), nullable=False),
        # Medida en reinas (no en nucleos, como el parque).
        sa.Column(
            "capacidad_max",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(str(CAPACIDAD_BANCO_POR_DEFECTO)),
        ),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint("capacidad_max > 0", name="ck_banco_capacidad_max"),
        sa.UniqueConstraint(
            "tenant_id", "identificador", name="uq_banco_tenant_identificador"
        ),
        # Permite que celda referencie (tenant_id, id) sin cruzar criaderos.
        sa.UniqueConstraint("tenant_id", "id", name="uq_banco_tenant_id"),
    )
    _aislar_por_tenant("banco", PERMISOS_BANCO)

    # --- tanda: madre opcional y ubicacion en la continuadora ---
    op.alter_column("tanda", "madre_id", nullable=True)
    op.add_column("tanda", sa.Column("fila_continuadora", sa.Integer()))
    op.add_column("tanda", sa.Column("posicion_continuadora", sa.Integer()))
    # Las dos o ninguna (una tanda retroactiva puede no saberlas), y positivas.
    # Los IS NOT NULL son necesarios: con una sola en NULL, "> 0" da NULL y un
    # CHECK en NULL pasa.
    op.create_check_constraint(
        "ck_tanda_continuadora",
        "tanda",
        "(fila_continuadora IS NULL AND posicion_continuadora IS NULL) "
        "OR (fila_continuadora IS NOT NULL AND posicion_continuadora IS NOT NULL "
        "AND fila_continuadora > 0 AND posicion_continuadora > 0)",
    )

    # --- celda: banco y fechas de perdida ---
    op.add_column("celda", sa.Column("banco_id", postgresql.UUID(as_uuid=True)))
    op.add_column("celda", sa.Column("fecha_descarte", sa.Date()))
    op.add_column("celda", sa.Column("fecha_extravio", sa.Date()))
    # MATCH SIMPLE, como nucleo_id: con banco_id NULL la FK no se evalua.
    op.create_foreign_key(
        "fk_celda_banco_mismo_tenant",
        "celda",
        "banco",
        ["tenant_id", "banco_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_celda_tenant_banco", "celda", ["tenant_id", "banco_id"])
    # La fecha de perdida va si y solo si la celda se perdio por esa causa.
    op.create_check_constraint(
        "ck_celda_fecha_descarte",
        "celda",
        "(estado = 'descartada') = (fecha_descarte IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_celda_fecha_extravio",
        "celda",
        "(estado = 'extraviada') = (fecha_extravio IS NOT NULL)",
    )
    # Solo una reina enjaulada (o ya despachada) puede estar en un banco.
    op.create_check_constraint(
        "ck_celda_banco_al_enjaular",
        "celda",
        "banco_id IS NULL OR estado IN ('enjaulada', 'despachada')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_celda_banco_al_enjaular", "celda", type_="check")
    op.drop_constraint("ck_celda_fecha_extravio", "celda", type_="check")
    op.drop_constraint("ck_celda_fecha_descarte", "celda", type_="check")
    op.drop_index("ix_celda_tenant_banco", table_name="celda")
    op.drop_constraint("fk_celda_banco_mismo_tenant", "celda", type_="foreignkey")
    op.drop_column("celda", "fecha_extravio")
    op.drop_column("celda", "fecha_descarte")
    op.drop_column("celda", "banco_id")

    op.drop_constraint("ck_tanda_continuadora", "tanda", type_="check")
    op.drop_column("tanda", "posicion_continuadora")
    op.drop_column("tanda", "fila_continuadora")
    # Falla si hay tandas sin madre: no se inventa una madre para volver atras.
    op.alter_column("tanda", "madre_id", nullable=False)

    # La politica, el grant y los indices se van con la tabla.
    op.drop_table("banco")
