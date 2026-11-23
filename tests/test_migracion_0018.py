"""Ida y vuelta de la migracion 0018 sobre la base de los tests (#42).

Cada prueba corre en una transaccion del duenio que se deshace al final: en
PostgreSQL el DDL es transaccional, asi que los demas tests siguen viendo la
base en head. La ida y vuelta completa (base -> head -> base -> head) ya la
hace conftest.py con la base vacia; aca se prueba con datos.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import RAIZ, crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration


def _cargar_0018() -> ModuleType:
    """El archivo empieza con un numero: no se puede importar con import."""
    ruta = RAIZ / "migraciones" / "versions" / "0018_aprobacion_borrador.py"
    spec = importlib.util.spec_from_file_location("migracion_0018", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


MIGRACION = _cargar_0018()


def _correr(conexion: Connection, paso: Callable[[], None]) -> None:
    with Operations.context(MigrationContext.configure(conexion)):
        paso()


@pytest.fixture
def conexion(motor_owner: Engine) -> Iterator[Connection]:
    """Conexion del duenio con un criadero y un borrador; todo se deshace."""
    with motor_owner.connect() as c:
        transaccion = c.begin()
        try:
            yield c
        finally:
            transaccion.rollback()


def _borrador(c: Connection, estado: str) -> uuid.UUID:
    tenant = uuid.uuid4()
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    cliente = c.execute(
        text(
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
            "VALUES ('Apicola', 'AR', 'nacional', 'monotributo', '20123456786') "
            "RETURNING id"
        )
    ).scalar_one()
    pedido = c.execute(
        text(
            "INSERT INTO pedido (cliente_id, moneda, fecha_entrega_solicitada) "
            "VALUES (:cliente, 'ARS', '2026-11-20') RETURNING id"
        ),
        {"cliente": cliente},
    ).scalar_one()
    borrador = uuid.uuid4()
    aprobado = estado == "aprobado"
    c.execute(
        text(
            "INSERT INTO borrador (id, pedido_id, estado, contenido, snapshot, "
            "hash_contenido, pdf_s3_key, solicitado_por) VALUES (:id, :pedido, "
            ":estado, CAST(:contenido AS jsonb), CAST(:snapshot AS jsonb), :hash, "
            ":clave, 'sub')"
        ),
        {
            "id": borrador,
            "pedido": pedido,
            "estado": estado,
            "contenido": json.dumps({"encabezado": "x", "cuerpo": "y", "notas": ""}),
            "snapshot": "{}" if aprobado else None,
            "hash": "a" * 64 if aprobado else None,
            "clave": (
                f"documentos/{tenant}/borradores/{borrador}.pdf" if aprobado else None
            ),
        },
    )
    return borrador


def _columnas(c: Connection) -> set[str]:
    return set(
        c.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'borrador'"
            )
        ).scalars()
    )


def test_downgrade_y_upgrade_con_borradores_en_revision(conexion: Connection) -> None:
    _borrador(conexion, "borrador")

    _correr(conexion, MIGRACION.downgrade)
    assert "snapshot" not in _columnas(conexion)
    assert (
        conexion.execute(text("SELECT to_regclass('revision_borrador')")).scalar()
        is None
    )

    _correr(conexion, MIGRACION.upgrade)
    assert {
        "snapshot",
        "hash_contenido",
        "pdf_s3_key",
        "comprobante_id",
        "corrige_a",
        "motivo_descarte",
    } <= _columnas(conexion)
    assert conexion.execute(text("SELECT to_regclass('revision_borrador')")).scalar()


@pytest.mark.parametrize("estado", ["aprobado", "descartado"])
def test_el_downgrade_no_borra_borradores_revisados(
    conexion: Connection, estado: str
) -> None:
    """Los CHECK de la 0016 no admiten aprobado ni descartado: falla y no se
    pierde la historia sin que alguien lo decida a mano."""
    borrador = _borrador(conexion, "borrador")
    if estado == "descartado":
        conexion.execute(
            text(
                "UPDATE borrador SET estado = 'descartado', motivo_descarte = "
                "'No sirve' WHERE id = :id"
            ),
            {"id": borrador},
        )
    else:
        _borrador(conexion, "aprobado")

    with pytest.raises(IntegrityError, match="ck_borrador_"):
        _correr(conexion, MIGRACION.downgrade)
