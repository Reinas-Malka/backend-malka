"""La semilla de criaderos demo de la 0006 corre solo en dev (#104).

La base de los tests se migra como prod (conftest.py): ahi no tiene que haber
criaderos demo. Para dev se corre la migracion 0006 sobre esa misma base,
dentro de una transaccion que se deshace al final, asi no cambia lo que ven
los demas tests.
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

from tests.conftest import RAIZ, crear_tenant


def _cargar_0006() -> ModuleType:
    """El archivo empieza con un numero: no se puede importar con import."""
    ruta = RAIZ / "migraciones" / "versions" / "0006_seed_tenants.py"
    spec = importlib.util.spec_from_file_location("migracion_0006", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


SEMILLA = _cargar_0006()
IDS_DEMO = [tenant_id for tenant_id, _, _ in SEMILLA.TENANTS]


def _contar_demo(conexion: Connection) -> int:
    return int(
        conexion.execute(
            text("SELECT count(*) FROM tenant WHERE id::text = ANY(:ids)"),
            {"ids": IDS_DEMO},
        ).scalar_one()
    )


def _correr(conexion: Connection, paso: Callable[[], None]) -> None:
    """Ejecuta upgrade() o downgrade() de la 0006 sobre esta conexion."""
    with Operations.context(MigrationContext.configure(conexion)):
        paso()


@pytest.fixture
def conexion(motor_owner: Engine) -> Iterator[Connection]:
    """Conexion del duenio en una transaccion que siempre se deshace."""
    with motor_owner.connect() as c:
        transaccion = c.begin()
        try:
            yield c
        finally:
            transaccion.rollback()


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        pytest.param("dev", True, id="dev"),
        pytest.param("prod", False, id="prod"),
        pytest.param("staging", False, id="otro"),
        pytest.param("DEV", False, id="mayusculas"),
        pytest.param("", False, id="vacio"),
        pytest.param(None, False, id="sin_variable"),
    ],
)
def test_solo_dev_siembra_y_sin_entorno_no(
    monkeypatch: pytest.MonkeyPatch, valor: str | None, esperado: bool
) -> None:
    if valor is None:
        monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("APP_ENVIRONMENT", valor)
    assert SEMILLA.corresponde_sembrar() is esperado


@pytest.mark.integration
def test_upgrade_head_en_prod_no_crea_tenants_demo(motor_owner: Engine) -> None:
    """DoD del #104: la base se migro hasta head con APP_ENVIRONMENT=prod."""
    with motor_owner.connect() as c:
        assert _contar_demo(c) == 0


@pytest.mark.integration
def test_en_prod_la_0006_no_siembra(
    conexion: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "prod")
    _correr(conexion, SEMILLA.upgrade)
    assert _contar_demo(conexion) == 0


@pytest.mark.integration
def test_en_dev_la_0006_siembra_y_el_downgrade_la_saca(
    conexion: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "dev")
    _correr(conexion, SEMILLA.upgrade)
    assert _contar_demo(conexion) == len(IDS_DEMO)

    # Idempotente: correrla de nuevo no duplica ni falla.
    _correr(conexion, SEMILLA.upgrade)
    assert _contar_demo(conexion) == len(IDS_DEMO)

    _correr(conexion, SEMILLA.downgrade)
    assert _contar_demo(conexion) == 0


@pytest.mark.integration
def test_fuera_de_dev_el_downgrade_no_borra_nada(
    conexion: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fuera de dev la 0006 no creo filas, asi que tampoco borra."""
    crear_tenant(conexion, uuid.UUID(IDS_DEMO[0]))
    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    _correr(conexion, SEMILLA.downgrade)
    assert _contar_demo(conexion) == 1
