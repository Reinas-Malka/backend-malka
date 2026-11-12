"""Aprobacion y descarte de borradores, con bitacora inmutable (#42).

Reglas en docs/dominio.md ("Aprobacion de borradores", "Regla general") y en
el issue (corregido el 17/10):
- Estados nuevos del borrador: aprobado (lo aprueba una persona, nunca la IA)
  y descartado (una persona lo rechaza en la revision). descartado no es
  rechazado: rechazado lo decide el validador o el modelo y no tiene
  contenido; descartado tiene contenido y un motivo escrito por alguien.
- Al aprobar se congela el snapshot (exactamente lo aprobado), el hash del
  contenido aprobado y la key del PDF interno en S3. El PDF es el del
  borrador aprobado: nunca un comprobante fiscal (lo emite ARCA).
- El comprobante transcripto (tabla comprobante, #108) solo se referencia:
  su UNIQUE y su transcripcion viven alla.
- Un borrador aprobado o descartado es inmutable: un trigger rechaza
  UPDATE y DELETE, tambien para el duenio de la tabla. La correccion es un
  borrador nuevo que apunta al anterior con corrige_a.
- revision_borrador es la bitacora: quien, cuando y sobre que version
  (hash) se aprobo o descarto. Append-only: malka_app tiene SELECT e INSERT,
  sin UPDATE ni DELETE.

Downgrade: falla si hay borradores aprobados o descartados (los CHECK de la
0016 no los admiten). Es a proposito: borrar esa historia no puede pasar
sin que alguien lo decida a mano.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rol de aplicacion sin privilegios, creado en la 0002 (#19).
ROL_APP = "malka_app"
# La bitacora no se edita ni se borra.
PERMISOS_REVISION = "SELECT, INSERT"

# Mismos valores que app/services/ia/generacion.py. Se copian a proposito:
# una migracion no importa codigo de la app.
ESTADOS = ("generando", "borrador", "rechazado", "aprobado", "descartado")
ESTADOS_SQL = ", ".join(f"'{e}'" for e in ESTADOS)
ESTADOS_0016_SQL = "'generando', 'borrador', 'rechazado'"
ACCIONES = ("aprobado", "descartado")
ACCIONES_SQL = ", ".join(f"'{a}'" for a in ACCIONES)
# Largo del motivo de descarte: el mismo que valida el schema.
MOTIVO_MINIMO = 3
MOTIVO_MAXIMO = 500
# sha256 en hexadecimal.
HASH_SQL = "'^[0-9a-f]{64}$'"

# El codigo de error del trigger; app/errores.py lo traduce a 409.
SQLSTATE_INMUTABLE = "MK409"

# Misma expresion que la 0001: sin app.tenant_id definido, cero filas.
TENANT_ACTUAL = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

CHECKS_0016 = {
    "ck_borrador_estado": f"estado IN ({ESTADOS_0016_SQL})",
    "ck_borrador_contenido_segun_estado": (
        "(estado = 'borrador') = (contenido IS NOT NULL) "
        "AND (estado = 'rechazado') = (motivo_rechazo IS NOT NULL)"
    ),
}

CHECKS_NUEVOS = {
    "ck_borrador_estado": f"estado IN ({ESTADOS_SQL})",
    # Lo redactado sigue ahi despues de aprobarlo o descartarlo.
    "ck_borrador_contenido_segun_estado": (
        "(estado IN ('borrador', 'aprobado', 'descartado')) = (contenido IS NOT NULL) "
        "AND (estado = 'rechazado') = (motivo_rechazo IS NOT NULL)"
    ),
    # Cada columna por separado: con un AND de las tres, un borrador sin
    # aprobar podria tener un snapshot suelto.
    "ck_borrador_aprobado": (
        "(estado = 'aprobado') = (snapshot IS NOT NULL) "
        "AND (estado = 'aprobado') = (hash_contenido IS NOT NULL) "
        "AND (estado = 'aprobado') = (pdf_s3_key IS NOT NULL) "
        "AND (comprobante_id IS NULL OR estado = 'aprobado')"
    ),
    "ck_borrador_hash_contenido": (
        f"hash_contenido IS NULL OR hash_contenido ~ {HASH_SQL}"
    ),
    # La key es la del propio borrador en su criadero: no se puede apuntar
    # al PDF de otro aunque se conozca su key.
    "ck_borrador_pdf_del_tenant": (
        "pdf_s3_key IS NULL OR pdf_s3_key = "
        "'documentos/' || tenant_id::text || '/borradores/' || id::text || '.pdf'"
    ),
    "ck_borrador_descartado": (
        "(estado = 'descartado') = (motivo_descarte IS NOT NULL) "
        "AND (motivo_descarte IS NULL OR char_length(motivo_descarte) "
        f"BETWEEN {MOTIVO_MINIMO} AND {MOTIVO_MAXIMO})"
    ),
    "ck_borrador_no_se_corrige_a_si_mismo": "corrige_a IS NULL OR corrige_a <> id",
}


def _uuid(nombre: str, *, nullable: bool) -> sa.Column:
    return sa.Column(nombre, postgresql.UUID(as_uuid=True), nullable=nullable)


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


def _columnas_del_borrador() -> None:
    op.add_column("borrador", sa.Column("snapshot", postgresql.JSONB(), nullable=True))
    op.add_column("borrador", sa.Column("hash_contenido", sa.Text(), nullable=True))
    op.add_column("borrador", sa.Column("pdf_s3_key", sa.Text(), nullable=True))
    op.add_column("borrador", _uuid("comprobante_id", nullable=True))
    op.add_column("borrador", _uuid("corrige_a", nullable=True))
    op.add_column("borrador", sa.Column("motivo_descarte", sa.Text(), nullable=True))

    # Las FK compuestas necesitan un UNIQUE (tenant_id, id) en el padre.
    op.create_unique_constraint(
        "uq_comprobante_tenant_id", "comprobante", ["tenant_id", "id"]
    )
    op.create_unique_constraint(
        "uq_borrador_tenant_id", "borrador", ["tenant_id", "id"]
    )
    op.create_foreign_key(
        "fk_borrador_comprobante_mismo_tenant",
        "borrador",
        "comprobante",
        ["tenant_id", "comprobante_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_borrador_corrige_a_mismo_tenant",
        "borrador",
        "borrador",
        ["tenant_id", "corrige_a"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    for nombre in CHECKS_0016:
        op.drop_constraint(nombre, "borrador", type_="check")
    for nombre, condicion in CHECKS_NUEVOS.items():
        op.create_check_constraint(nombre, "borrador", condicion)


def _trigger_de_inmutabilidad() -> None:
    # Un trigger y no solo el GRANT: tambien frena al duenio de la tabla y a
    # cualquier UPDATE que se escape de la API. Solo un ALTER TABLE ...
    # DISABLE TRIGGER (DDL, del duenio) lo saltea, y eso queda a la vista.
    op.execute(
        f"""
        CREATE FUNCTION borrador_inmutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.estado IN ('aprobado', 'descartado') THEN
                RAISE EXCEPTION 'el borrador % esta %: no se modifica ni se borra',
                    OLD.id, OLD.estado
                    USING ERRCODE = '{SQLSTATE_INMUTABLE}';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER tg_borrador_inmutable BEFORE UPDATE OR DELETE ON borrador "
        "FOR EACH ROW EXECUTE FUNCTION borrador_inmutable()"
    )


def _bitacora() -> None:
    op.create_table(
        "revision_borrador",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _uuid("borrador_id", nullable=False),
        sa.Column("accion", sa.Text(), nullable=False),
        # El sub de Cognito de quien aprobo o descarto.
        sa.Column("usuario", sa.Text(), nullable=False),
        sa.Column(
            "momento",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # La version del contenido que vio quien decidio.
        sa.Column("hash_contenido", sa.Text(), nullable=False),
        # Solo al aprobar: el hash del snapshot congelado.
        sa.Column("hash_snapshot", sa.Text(), nullable=True),
        sa.Column("version_prompt", sa.Text(), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        _uuid("comprobante_id", nullable=True),
        sa.CheckConstraint(
            f"accion IN ({ACCIONES_SQL})", name="ck_revision_borrador_accion"
        ),
        sa.CheckConstraint(
            f"hash_contenido ~ {HASH_SQL} "
            f"AND (hash_snapshot IS NULL OR hash_snapshot ~ {HASH_SQL})",
            name="ck_revision_borrador_hashes",
        ),
        sa.CheckConstraint(
            "(accion = 'aprobado') = (hash_snapshot IS NOT NULL) "
            "AND (accion = 'descartado') = (motivo IS NOT NULL) "
            "AND (comprobante_id IS NULL OR accion = 'aprobado')",
            name="ck_revision_borrador_segun_accion",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "borrador_id"],
            ["borrador.tenant_id", "borrador.id"],
            name="fk_revision_borrador_borrador_mismo_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "comprobante_id"],
            ["comprobante.tenant_id", "comprobante.id"],
            name="fk_revision_borrador_comprobante_mismo_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_revision_borrador_tenant_borrador_momento",
        "revision_borrador",
        ["tenant_id", "borrador_id", "momento"],
    )
    _aislar_por_tenant("revision_borrador", PERMISOS_REVISION)


def upgrade() -> None:
    _columnas_del_borrador()
    _trigger_de_inmutabilidad()
    _bitacora()


def downgrade() -> None:
    # La politica, el indice y el permiso se van con la tabla.
    op.drop_table("revision_borrador")
    op.execute("DROP TRIGGER tg_borrador_inmutable ON borrador")
    op.execute("DROP FUNCTION borrador_inmutable()")

    for nombre in CHECKS_NUEVOS:
        op.drop_constraint(nombre, "borrador", type_="check")
    # Con borradores aprobados o descartados, esto falla y no se pierde nada.
    for nombre, condicion in CHECKS_0016.items():
        op.create_check_constraint(nombre, "borrador", condicion)

    op.drop_constraint("fk_borrador_corrige_a_mismo_tenant", "borrador")
    op.drop_constraint("fk_borrador_comprobante_mismo_tenant", "borrador")
    op.drop_constraint("uq_borrador_tenant_id", "borrador")
    op.drop_constraint("uq_comprobante_tenant_id", "comprobante")
    for columna in (
        "motivo_descarte",
        "corrige_a",
        "comprobante_id",
        "pdf_s3_key",
        "hash_contenido",
        "snapshot",
    ):
        op.drop_column("borrador", columna)
