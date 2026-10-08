"""Migracion 0004 contra un PostgreSQL real (#30): materiales e items vendibles.

Cubre el aislamiento por tenant de las tres tablas nuevas, incluido el caso de
fuga entre tenants, y las reglas de docs/dominio.md ("Materiales y consumo")
que hace cumplir la propia base: el CHECK de signo por tipo de movimiento y
el stock como suma, que puede quedar negativo, y las alicuotas del catalogo.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app import db
from app.services.documentos.importes import Alicuota
from tests.conftest import crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

TABLAS_NUEVAS = ("material", "movimiento_material", "item_vendible")
COMPRA_INICIAL = Decimal("270")


class _Deshacer(Exception):
    """Corta la sesion para que haga rollback."""


@dataclass(frozen=True)
class Deposito:
    """Un tenant con un material comprado y una tanda que lo puede consumir."""

    tenant: uuid.UUID
    material: uuid.UUID
    tanda: uuid.UUID


def _cargar_deposito(c: Connection, tenant: uuid.UUID) -> Deposito:
    """Carga los datos como duenio: con FORCE RLS necesita el tenant fijado."""
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)

    def insertar(sql: str, **params: object) -> uuid.UUID:
        valor = c.execute(text(sql), params).scalar_one()
        assert isinstance(valor, uuid.UUID)
        return valor

    raza = insertar("INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id")
    madre = insertar("INSERT INTO madre (identificacion) VALUES ('M-01') RETURNING id")
    tanda = insertar(
        "INSERT INTO tanda (raza_id, madre_id, fecha_traslarve) "
        "VALUES (:raza, :madre, '2026-10-05') RETURNING id",
        raza=raza,
        madre=madre,
    )
    material = insertar(
        "INSERT INTO material (nombre, unidad) VALUES ('Cupula', 'unidad') RETURNING id"
    )
    c.execute(
        text(
            "INSERT INTO movimiento_material (material_id, tipo, cantidad) "
            "VALUES (:material, 'compra', :cantidad)"
        ),
        {"material": material, "cantidad": COMPRA_INICIAL},
    )
    c.execute(text("INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada')"))
    return Deposito(tenant=tenant, material=material, tanda=tanda)


@pytest.fixture(scope="module")
def depositos(motor_owner: Engine) -> Iterator[tuple[Deposito, Deposito]]:
    """Dos tenants con los mismos datos, para probar que no se mezclan."""
    with motor_owner.begin() as c:
        a = _cargar_deposito(c, uuid.uuid4())
    with motor_owner.begin() as c:
        b = _cargar_deposito(c, uuid.uuid4())
    yield a, b
    # El duenio puede borrar (la API no). Se borra de hijas a padres.
    for deposito in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, deposito.tenant)
            for tabla in (
                "item_vendible",
                "movimiento_material",
                "material",
                "tanda",
                "madre",
                "raza",
            ):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(
                text("DELETE FROM tenant WHERE id = :id"), {"id": deposito.tenant}
            )


def _movimiento(s: Session, material: uuid.UUID, tipo: str, cantidad: str) -> None:
    s.execute(
        text(
            "INSERT INTO movimiento_material (material_id, tipo, cantidad) "
            "VALUES (:material, :tipo, :cantidad)"
        ),
        {"material": material, "tipo": tipo, "cantidad": Decimal(cantidad)},
    )


# --- Aislamiento por tenant ---


@pytest.mark.usefixtures("depositos")
@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_sin_set_local_cero_filas(motor_app: Engine, tabla: str) -> None:
    with motor_app.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar()  # noqa: S608
    assert total == 0


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_cada_tenant_ve_solo_lo_suyo(
    depositos: tuple[Deposito, Deposito], tabla: str
) -> None:
    """Fuga entre tenants (AGENTS.md, regla 5): B no ve nada de A."""
    for deposito in depositos:
        with db.sesion_de_tenant(deposito.tenant) as s:
            tenants = s.execute(
                text(f"SELECT DISTINCT tenant_id FROM {tabla}")  # noqa: S608
            ).scalars()
            assert set(tenants) == {deposito.tenant}


@pytest.mark.usefixtures("con_motor_app")
def test_no_puede_crear_material_en_otro_tenant(
    depositos: tuple[Deposito, Deposito],
) -> None:
    a, b = depositos
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO material (tenant_id, nombre, unidad) "
                    "VALUES (:otro, 'intruso', 'kg')"
                ),
                {"otro": b.tenant},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_movimiento_no_puede_apuntar_a_un_material_de_otro_tenant(
    depositos: tuple[Deposito, Deposito],
) -> None:
    """Segunda capa: la clave foranea compuesta (tenant_id, id)."""
    a, b = depositos
    with pytest.raises(IntegrityError, match="fk_movimiento_material_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            _movimiento(s, b.material, "compra", "10")


@pytest.mark.usefixtures("con_motor_app")
def test_consumo_no_puede_apuntar_a_una_tanda_de_otro_tenant(
    depositos: tuple[Deposito, Deposito],
) -> None:
    a, b = depositos
    with pytest.raises(IntegrityError, match="fk_movimiento_tanda_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO movimiento_material "
                    "(material_id, tipo, cantidad, tanda_id) "
                    "VALUES (:material, 'consumo', -135, :tanda)"
                ),
                {"material": a.material, "tanda": b.tanda},
            )


@pytest.mark.parametrize(
    ("tabla", "permiso"),
    [
        ("material", "DELETE"),
        ("movimiento_material", "UPDATE"),
        ("movimiento_material", "DELETE"),
        ("item_vendible", "DELETE"),
    ],
)
def test_rol_app_sin_permiso(motor_owner: Engine, tabla: str, permiso: str) -> None:
    """Material e item con borrado logico; el movimiento no se edita ni se borra."""
    with motor_owner.connect() as c:
        puede = c.execute(
            text("SELECT has_table_privilege('malka_app', :tabla, :permiso)"),
            {"tabla": tabla, "permiso": permiso},
        ).scalar()
    assert puede is False


# --- Reglas de docs/dominio.md ---


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    ("tipo", "cantidad"),
    [
        ("compra", "-10"),
        ("compra", "0"),
        ("consumo", "135"),
        ("consumo", "0"),
        ("ajuste", "0"),
    ],
)
def test_check_de_signo_por_tipo_rechaza(
    depositos: tuple[Deposito, Deposito], tipo: str, cantidad: str
) -> None:
    """DoD de la #30: compra positiva, consumo negativa, ajuste distinto de cero."""
    a, _ = depositos
    with pytest.raises(IntegrityError, match="ck_movimiento_signo"):
        with db.sesion_de_tenant(a.tenant) as s:
            _movimiento(s, a.material, tipo, cantidad)


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    ("tipo", "cantidad"),
    [("compra", "10"), ("consumo", "-135"), ("ajuste", "5"), ("ajuste", "-5")],
)
def test_check_de_signo_por_tipo_acepta(
    depositos: tuple[Deposito, Deposito], tipo: str, cantidad: str
) -> None:
    a, _ = depositos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        _movimiento(s, a.material, tipo, cantidad)
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
def test_tipo_invalido(depositos: tuple[Deposito, Deposito]) -> None:
    a, _ = depositos
    # Un tipo desconocido tampoco cumple el CHECK de signo, y Postgres informa
    # el primero que falla: cualquiera de los dos lo rechaza.
    with pytest.raises(IntegrityError, match="ck_movimiento_(tipo|signo)"):
        with db.sesion_de_tenant(a.tenant) as s:
            _movimiento(s, a.material, "devolucion", "10")


@pytest.mark.usefixtures("con_motor_app")
def test_tanda_solo_en_consumo(depositos: tuple[Deposito, Deposito]) -> None:
    a, _ = depositos
    with pytest.raises(IntegrityError, match="ck_movimiento_tanda_solo_en_consumo"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO movimiento_material "
                    "(material_id, tipo, cantidad, tanda_id) "
                    "VALUES (:material, 'compra', 10, :tanda)"
                ),
                {"material": a.material, "tanda": a.tanda},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_stock_es_la_suma_y_puede_quedar_negativo(
    depositos: tuple[Deposito, Deposito],
) -> None:
    """Sin columna de stock y sin constraint de negativo (docs/dominio.md)."""
    a, _ = depositos
    consumo = -(COMPRA_INICIAL + 135)
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        s.execute(
            text(
                "INSERT INTO movimiento_material "
                "(material_id, tipo, cantidad, tanda_id) "
                "VALUES (:material, 'consumo', :cantidad, :tanda)"
            ),
            {"material": a.material, "cantidad": consumo, "tanda": a.tanda},
        )
        stock = s.execute(
            text(
                "SELECT SUM(cantidad) FROM movimiento_material "
                "WHERE material_id = :material"
            ),
            {"material": a.material},
        ).scalar()
        assert stock == Decimal("-135")
        raise _Deshacer


def test_material_sin_columna_de_stock(motor_owner: Engine) -> None:
    with motor_owner.connect() as c:
        columnas = set(
            c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'material'"
                )
            ).scalars()
        )
    assert not {"stock", "cantidad", "stock_actual"} & columnas


@pytest.mark.usefixtures("con_motor_app")
def test_nombre_de_material_repetido(depositos: tuple[Deposito, Deposito]) -> None:
    a, _ = depositos
    with pytest.raises(IntegrityError, match="uq_material_tenant_nombre"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text("INSERT INTO material (nombre, unidad) VALUES ('Cupula', 'u')")
            )


@pytest.mark.usefixtures("con_motor_app")
def test_mismo_nombre_en_otro_tenant(depositos: tuple[Deposito, Deposito]) -> None:
    """El UNIQUE es por tenant: dos criaderos pueden tener 'Cupula'."""
    _, b = depositos
    with db.sesion_de_tenant(b.tenant) as s:
        nombres = s.execute(text("SELECT nombre FROM material")).scalars().all()
    assert nombres == ["Cupula"]


# --- Catalogo de items vendibles ---


@pytest.mark.usefixtures("con_motor_app")
def test_item_vendible_arranca_en_21_por_ciento(
    depositos: tuple[Deposito, Deposito],
) -> None:
    """Default para reinas: 21%, NO VERIFICADO (docs/dominio.md)."""
    a, _ = depositos
    with db.sesion_de_tenant(a.tenant) as s:
        alicuota = s.execute(text("SELECT alicuota FROM item_vendible")).scalar_one()
    assert Alicuota(alicuota) is Alicuota.IVA_21


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize("alicuota", list(Alicuota), ids=lambda a: a.name)
def test_item_vendible_acepta_cada_alicuota_del_enum(
    depositos: tuple[Deposito, Deposito], alicuota: Alicuota
) -> None:
    """El CHECK de la base y el enum de importes.py tienen los mismos valores."""
    a, _ = depositos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        s.execute(
            text("INSERT INTO item_vendible (nombre, alicuota) VALUES (:n, :a)"),
            {"n": f"Item {alicuota.name}", "a": alicuota.value},
        )
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
def test_item_vendible_rechaza_una_alicuota_fuera_del_enum(
    depositos: tuple[Deposito, Deposito],
) -> None:
    a, _ = depositos
    with pytest.raises(IntegrityError, match="ck_item_vendible_alicuota"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO item_vendible (nombre, alicuota) "
                    "VALUES ('Cupulas', 0.19)"
                )
            )


@pytest.mark.usefixtures("con_motor_app")
def test_no_puede_crear_item_en_otro_tenant(
    depositos: tuple[Deposito, Deposito],
) -> None:
    a, b = depositos
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO item_vendible (tenant_id, nombre) "
                    "VALUES (:otro, 'intruso')"
                ),
                {"otro": b.tenant},
            )
