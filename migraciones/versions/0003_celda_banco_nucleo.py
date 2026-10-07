"""Celda como entidad central: madre, banco, nucleo y celda; fuera tanda_evento.

Alinea el esquema con docs/dominio.md (issue #26). La base no tiene datos de
negocio, asi que no hace falta migrar datos: se dropea lo que no corresponde.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"

# Tablas nuevas de negocio con los permisos minimos del rol de aplicacion.
# Ninguna lleva DELETE: madre, banco y nucleo usan borrado logico (#30) y una
# celda nunca se borra, se cierra con un estado terminal (#28).
PERMISOS_TABLAS_NUEVAS = {
    "madre": "SELECT, INSERT, UPDATE",
    "banco": "SELECT, INSERT, UPDATE",
    "nucleo": "SELECT, INSERT, UPDATE",
    "celda": "SELECT, INSERT, UPDATE",
}

# Estados de la celda (docs/dominio.md, ciclo de cria). Dos terminales de
# perdida con causas distintas: descartada (nunca se introdujo) y extraviada
# (nacio y no volvio del vuelo nupcial).
ESTADOS_CELDA = (
    "trasladada",
    "descartada",
    "introducida",
    "nacida",
    "madura",
    "fecundada",
    "extraviada",
    "enjaulada",
    "despachada",
)
ESTADOS_CELDA_SQL = ", ".join(f"'{estado}'" for estado in ESTADOS_CELDA)

# Etapas del modelo viejo de la 0001, solo para recrearlo en el downgrade.
ETAPAS_VIEJAS = (
    "traslarve",
    "iniciadora",
    "continuadora",
    "parque",
    "banco",
    "cerrada",
)
ETAPAS_VIEJAS_SQL = ", ".join(f"'{etapa}'" for etapa in ETAPAS_VIEJAS)

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
    # --- tenant: el CUIT es atributo del tenant (un solo CUIT por cabania) ---
    op.add_column("tenant", sa.Column("cuit", sa.Text(), nullable=False))
    op.create_check_constraint("ck_tenant_cuit", "tenant", "cuit ~ '^[0-9]{11}$'")
    op.create_unique_constraint("uq_tenant_cuit", "tenant", ["cuit"])

    # --- tanda_evento y las etapas de tanda: fuera ---
    # El historial queda en las fechas de cada celda.
    op.drop_table("tanda_evento")
    op.drop_constraint("ck_tanda_etapa_actual", "tanda", type_="check")
    op.drop_column("tanda", "etapa_actual")

    # --- madre (madre reina): una sola por tanda ---
    op.create_table(
        "madre",
        _id(),
        _tenant_id(),
        sa.Column("identificacion", sa.Text(), nullable=False),
        # Borrado logico: una madre con tandas historicas no se borra (#30).
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.UniqueConstraint(
            "tenant_id", "identificacion", name="uq_madre_tenant_identificacion"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_madre_tenant_id"),
    )

    # La base no tiene tandas, asi que la columna entra directo como NOT NULL.
    op.add_column(
        "tanda",
        sa.Column("madre_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_foreign_key(
        "fk_tanda_madre_mismo_tenant",
        "tanda",
        "madre",
        ["tenant_id", "madre_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_tanda_tenant_madre", "tanda", ["tenant_id", "madre_id"])

    # --- banco -> nucleo: una sola jerarquia de ubicacion ---
    op.create_table(
        "banco",
        _id(),
        _tenant_id(),
        sa.Column("nombre", sa.Text(), nullable=False),
        # Medida en nucleos por banco (#29).
        sa.Column("capacidad_max", sa.Integer(), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint("capacidad_max > 0", name="ck_banco_capacidad_max"),
        sa.UniqueConstraint("tenant_id", "nombre", name="uq_banco_tenant_nombre"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_banco_tenant_id"),
    )

    op.create_table(
        "nucleo",
        _id(),
        _tenant_id(),
        sa.Column("banco_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Ubicacion del nucleo dentro del banco.
        sa.Column("fila", sa.Integer(), nullable=False),
        sa.Column("posicion", sa.Integer(), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint("fila > 0 AND posicion > 0", name="ck_nucleo_ubicacion"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "banco_id"],
            ["banco.tenant_id", "banco.id"],
            name="fk_nucleo_banco_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # Una posicion del banco no puede tener dos nucleos (#29 responde 409).
        sa.UniqueConstraint(
            "tenant_id", "banco_id", "fila", "posicion", name="uq_nucleo_ubicacion"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_nucleo_tenant_id"),
    )

    # --- celda: entidad central, una fila por cupula traslarvada ---
    # La cantidad de celdas de una tanda es COUNT(*), nunca una columna.
    op.create_table(
        "celda",
        _id(),
        _tenant_id(),
        sa.Column("tanda_id", postgresql.UUID(as_uuid=True), nullable=False),
        # NULL hasta la introduccion al nucleo (~D10).
        sa.Column("nucleo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "estado",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'trasladada'"),
        ),
        # Fechas biologicas: dias calendario, sin validacion de dia habil.
        sa.Column("fecha_introduccion", sa.Date(), nullable=True),
        sa.Column("fecha_nacimiento", sa.Date(), nullable=True),
        sa.Column("fecha_fecundacion", sa.Date(), nullable=True),
        sa.Column("fecha_enjaulado", sa.Date(), nullable=True),
        _creado_en(),
        _actualizado_en(),
        sa.CheckConstraint(f"estado IN ({ESTADOS_CELDA_SQL})", name="ck_celda_estado"),
        # Desde la introduccion en adelante, la celda tiene nucleo y fecha.
        sa.CheckConstraint(
            "estado IN ('trasladada', 'descartada') "
            "OR (nucleo_id IS NOT NULL AND fecha_introduccion IS NOT NULL)",
            name="ck_celda_introducida_con_nucleo",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tanda_id"],
            ["tanda.tenant_id", "tanda.id"],
            name="fk_celda_tanda_mismo_tenant",
            ondelete="RESTRICT",
        ),
        # Con nucleo_id en NULL la clave foranea no se verifica (MATCH SIMPLE).
        sa.ForeignKeyConstraint(
            ["tenant_id", "nucleo_id"],
            ["nucleo.tenant_id", "nucleo.id"],
            name="fk_celda_nucleo_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_celda_tenant_tanda", "celda", ["tenant_id", "tanda_id"])
    op.create_index("ix_celda_tenant_nucleo", "celda", ["tenant_id", "nucleo_id"])
    # Para las metricas por estado (#31).
    op.create_index("ix_celda_tenant_estado", "celda", ["tenant_id", "estado"])

    for tabla, permisos in PERMISOS_TABLAS_NUEVAS.items():
        _aislar_por_tenant(tabla, permisos)


def downgrade() -> None:
    # Al borrar cada tabla se borran tambien sus indices, politicas y GRANT.
    op.drop_table("celda")
    op.drop_table("nucleo")
    op.drop_table("banco")

    op.drop_index("ix_tanda_tenant_madre", table_name="tanda")
    op.drop_constraint("fk_tanda_madre_mismo_tenant", "tanda", type_="foreignkey")
    op.drop_column("tanda", "madre_id")
    op.drop_table("madre")

    # Vuelve el modelo de etapas de tanda, igual que en la 0001.
    op.add_column(
        "tanda",
        sa.Column(
            "etapa_actual",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'traslarve'"),
        ),
    )
    op.create_check_constraint(
        "ck_tanda_etapa_actual", "tanda", f"etapa_actual IN ({ETAPAS_VIEJAS_SQL})"
    )

    op.create_table(
        "tanda_evento",
        _id(),
        _tenant_id(),
        sa.Column("tanda_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("etapa", sa.Text(), nullable=False),
        sa.Column("cant_entrada", sa.Integer(), nullable=False),
        sa.Column("cant_aceptada", sa.Integer(), nullable=True),
        sa.Column("registrado_por", sa.Text(), nullable=False),
        sa.Column(
            "ocurrido_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("observacion", sa.Text(), nullable=True),
        _creado_en(),
        sa.CheckConstraint(
            "tipo IN ('transicion', 'ajuste')", name="ck_tanda_evento_tipo"
        ),
        sa.CheckConstraint(
            f"etapa IN ({ETAPAS_VIEJAS_SQL})", name="ck_tanda_evento_etapa"
        ),
        sa.CheckConstraint("cant_entrada >= 0", name="ck_tanda_evento_entrada"),
        sa.CheckConstraint(
            "cant_aceptada IS NULL OR "
            "(cant_aceptada >= 0 AND cant_aceptada <= cant_entrada)",
            name="ck_tanda_evento_aceptada",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tanda_id"],
            ["tanda.tenant_id", "tanda.id"],
            name="fk_tanda_evento_tanda_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_tanda_evento_tenant_tanda",
        "tanda_evento",
        ["tenant_id", "tanda_id", "ocurrido_en"],
    )
    op.create_index(
        "ix_tanda_evento_tenant_etapa",
        "tanda_evento",
        ["tenant_id", "etapa", "ocurrido_en"],
    )
    # Mismos permisos que le daba la 0002: historia inmutable, sin UPDATE.
    _aislar_por_tenant("tanda_evento", "SELECT, INSERT")

    op.drop_constraint("uq_tenant_cuit", "tenant", type_="unique")
    op.drop_constraint("ck_tenant_cuit", "tenant", type_="check")
    op.drop_column("tenant", "cuit")
