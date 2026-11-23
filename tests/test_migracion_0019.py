"""Ida y vuelta de la migracion 0019 (motivo 'asistente' en llamada_ia).

Cada prueba corre en una transaccion del duenio que se deshace al final: en
PostgreSQL el DDL es transaccional, asi que los demas tests siguen viendo la
base en head. La ida y vuelta completa con la base vacia ya la hace
conftest.py; aca se prueba con datos.
"""

from __future__ import annotations

import importlib.util
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


def _cargar_0019() -> ModuleType:
    """El archivo empieza con un numero: no se puede importar con import."""
    ruta = RAIZ / "migraciones" / "versions" / "0019_motivo_ia_asistente.py"
    spec = importlib.util.spec_from_file_location("migracion_0019", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


MIGRACION = _cargar_0019()


def _correr(conexion: Connection, paso: Callable[[], None]) -> None:
    with Operations.context(MigrationContext.configure(conexion)):
        paso()


@pytest.fixture
def conexion(motor_owner: Engine) -> Iterator[Connection]:
    """Conexion del duenio con un criadero propio; todo se deshace."""
    with motor_owner.connect() as c:
        transaccion = c.begin()
        try:
            tenant = uuid.uuid4()
            crear_tenant(c, tenant)
            fijar_tenant(c, tenant)
            yield c
        finally:
            transaccion.rollback()


def _llamada(c: Connection, motivo: str) -> None:
    c.execute(
        text("INSERT INTO llamada_ia (motivo) VALUES (:motivo)"), {"motivo": motivo}
    )


def test_en_head_acepta_asistente_y_rechaza_otro_motivo(conexion: Connection) -> None:
    _llamada(conexion, "borrador")
    _llamada(conexion, "asistente")

    with pytest.raises(IntegrityError, match="ck_llamada_ia_motivo"):
        _llamada(conexion, "otro")


def test_downgrade_y_upgrade_con_llamadas_del_borrador(conexion: Connection) -> None:
    _llamada(conexion, "borrador")

    _correr(conexion, MIGRACION.downgrade)
    punto = conexion.begin_nested()
    with pytest.raises(IntegrityError, match="ck_llamada_ia_motivo"):
        _llamada(conexion, "asistente")
    punto.rollback()

    _correr(conexion, MIGRACION.upgrade)
    _llamada(conexion, "asistente")


def test_el_downgrade_no_borra_llamadas_del_asistente(conexion: Connection) -> None:
    """El CHECK de la 0015 no admite 'asistente': falla y el registro de
    consumo no se pierde sin que alguien lo decida a mano."""
    _llamada(conexion, "asistente")

    with pytest.raises(IntegrityError, match="ck_llamada_ia_motivo"):
        _correr(conexion, MIGRACION.downgrade)
