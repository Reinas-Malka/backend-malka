"""Esquema de produccion (migraciones 0003 y 0009) contra un PostgreSQL real.

Cubre el DoD de la #26 y el dominio del 16/10: aislamiento por tenant en cada
tabla (madre, parque, nucleo, celda y banco), incluido el caso de fuga entre
tenants, y las reglas de docs/dominio.md que hace cumplir la propia base.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db
from tests.conftest import crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

TABLAS_NUEVAS = ("madre", "parque", "nucleo", "celda", "banco")
CELDAS_POR_TANDA = 3


class _Deshacer(Exception):
    """Corta la sesion para que haga rollback."""


@dataclass(frozen=True)
class Criadero:
    """Un tenant con una tanda de celdas, un parque con un nucleo y un banco."""

    tenant: uuid.UUID
    tanda: uuid.UUID
    parque: uuid.UUID
    nucleo: uuid.UUID
    banco: uuid.UUID


def _cargar_criadero(c: Connection, tenant: uuid.UUID) -> Criadero:
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
    parque = insertar(
        "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
        "VALUES ('P1', 'Predio norte', 9) RETURNING id"
    )
    nucleo = insertar(
        "INSERT INTO nucleo (parque_id, fila, posicion) "
        "VALUES (:parque, 1, 1) RETURNING id",
        parque=parque,
    )
    banco = insertar("INSERT INTO banco (identificador) VALUES ('B1') RETURNING id")
    c.execute(
        text(
            "INSERT INTO celda (tanda_id) "
            "SELECT :tanda FROM generate_series(1, :cantidad)"
        ),
        {"tanda": tanda, "cantidad": CELDAS_POR_TANDA},
    )
    return Criadero(
        tenant=tenant, tanda=tanda, parque=parque, nucleo=nucleo, banco=banco
    )


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[Criadero, Criadero]]:
    """Dos criaderos con los mismos datos, para probar que no se mezclan."""
    with motor_owner.begin() as c:
        a = _cargar_criadero(c, uuid.uuid4())
    with motor_owner.begin() as c:
        b = _cargar_criadero(c, uuid.uuid4())
    yield a, b
    # El duenio puede borrar (la API no). Se borra de hijas a padres.
    for criadero in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, criadero.tenant)
            for tabla in (
                "celda",
                "nucleo",
                "parque",
                "banco",
                "tanda",
                "madre",
                "raza",
            ):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(
                text("DELETE FROM tenant WHERE id = :id"), {"id": criadero.tenant}
            )


@pytest.mark.usefixtures("criaderos")
@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_sin_set_local_cero_filas(motor_app: Engine, tabla: str) -> None:
    with motor_app.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar()  # noqa: S608
    assert total == 0


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_cada_tenant_ve_solo_lo_suyo(
    criaderos: tuple[Criadero, Criadero], tabla: str
) -> None:
    """Fuga entre tenants (AGENTS.md, regla 5): B no ve nada de A."""
    for criadero in criaderos:
        with db.sesion_de_tenant(criadero.tenant) as s:
            tenants = s.execute(
                text(f"SELECT DISTINCT tenant_id FROM {tabla}")  # noqa: S608
            ).scalars()
            assert set(tenants) == {criadero.tenant}


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    ("tabla", "columnas"),
    [
        ("madre", "(tenant_id, identificacion) VALUES (:otro, 'intrusa')"),
        (
            "parque",
            "(tenant_id, nombre, ubicacion, capacidad_max) "
            "VALUES (:otro, 'intruso', 'Predio sur', 1)",
        ),
        ("banco", "(tenant_id, identificador) VALUES (:otro, 'intruso')"),
    ],
)
def test_no_puede_escribir_en_otro_tenant(
    criaderos: tuple[Criadero, Criadero], tabla: str, columnas: str
) -> None:
    a, b = criaderos
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(f"INSERT INTO {tabla} {columnas}"),  # noqa: S608
                {"otro": b.tenant},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_celda_no_puede_apuntar_a_una_tanda_de_otro_tenant(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    """Segunda capa: la clave foranea compuesta (tenant_id, id)."""
    a, b = criaderos
    with pytest.raises(IntegrityError, match="fk_celda_tanda_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text("INSERT INTO celda (tanda_id) VALUES (:tanda)"), {"tanda": b.tanda}
            )


@pytest.mark.usefixtures("con_motor_app")
def test_nucleo_no_puede_apuntar_a_un_parque_de_otro_tenant(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    """Segunda capa: la clave foranea compuesta (tenant_id, id)."""
    a, b = criaderos
    with pytest.raises(IntegrityError, match="fk_nucleo_parque_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO nucleo (parque_id, fila, posicion) "
                    "VALUES (:parque, 2, 2)"
                ),
                {"parque": b.parque},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_tenant_id_lo_completa_postgres(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        insertado = s.execute(
            text(
                "INSERT INTO madre (identificacion) VALUES ('M-02') RETURNING tenant_id"
            )
        ).scalar()
        assert insertado == a.tenant
        raise _Deshacer


@pytest.mark.parametrize("tabla", TABLAS_NUEVAS)
def test_rol_app_sin_delete(motor_owner: Engine, tabla: str) -> None:
    """Borrado logico en madre, parque y nucleo; la celda se cierra con un estado."""
    with motor_owner.connect() as c:
        puede = c.execute(
            text("SELECT has_table_privilege('malka_app', :tabla, :permiso)"),
            {"tabla": tabla, "permiso": "DELETE"},
        ).scalar()
    assert puede is False


@pytest.mark.usefixtures("con_motor_app")
def test_cantidad_de_celdas_es_derivada(criaderos: tuple[Criadero, Criadero]) -> None:
    """Una fila por cupula: la cantidad es COUNT(*), nunca una columna."""
    a, _ = criaderos
    with db.sesion_de_tenant(a.tenant) as s:
        filas = s.execute(
            text("SELECT estado, nucleo_id FROM celda WHERE tanda_id = :tanda"),
            {"tanda": a.tanda},
        ).all()
    assert len(filas) == CELDAS_POR_TANDA
    assert all(f.estado == "trasladada" and f.nucleo_id is None for f in filas)


@pytest.mark.usefixtures("con_motor_app")
def test_celda_introducida_necesita_nucleo(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_introducida_con_nucleo"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET estado = 'introducida'"))


@pytest.mark.usefixtures("con_motor_app")
def test_celda_introducida_con_nucleo_y_fecha(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        actualizadas = s.execute(
            text(
                "UPDATE celda SET estado = 'introducida', nucleo_id = :nucleo, "
                "fecha_introduccion = '2026-10-15'"
            ),
            {"nucleo": a.nucleo},
        ).rowcount
        assert actualizadas == CELDAS_POR_TANDA
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
def test_estado_invalido(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_estado"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET estado = 'perdida'"))


@pytest.mark.usefixtures("con_motor_app")
def test_posicion_de_nucleo_ocupada(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="uq_nucleo_ubicacion"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO nucleo (parque_id, fila, posicion) "
                    "VALUES (:parque, 1, 1)"
                ),
                {"parque": a.parque},
            )


def test_sin_modelo_de_etapas_de_tanda(motor_owner: Engine) -> None:
    """tanda_evento y tanda.etapa_actual ya no existen (docs/dominio.md)."""
    with motor_owner.connect() as c:
        tabla = c.execute(text("SELECT to_regclass('public.tanda_evento')")).scalar()
        banco = c.execute(text("SELECT to_regclass('public.banco')")).scalar()
        columna = c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'tanda' AND column_name = 'etapa_actual'"
            )
        ).scalar()
    assert tabla is None
    assert columna == 0
    # El banco volvio el 16/10, pero como tabla propia (0009), no como etapa.
    assert banco is not None


# --- dominio del 16/10 (migracion 0009) ---


def _con_celdas(tenant: uuid.UUID, sql: str, **params: object) -> int:
    """Corre un UPDATE sobre las celdas y deshace: devuelve las filas tocadas."""
    with pytest.raises(_Deshacer), db.sesion_de_tenant(tenant) as s:
        tocadas = s.execute(text(sql), params).rowcount
        raise _Deshacer
    return tocadas


@pytest.mark.usefixtures("con_motor_app")
def test_tanda_sin_madre_se_acepta(criaderos: tuple[Criadero, Criadero]) -> None:
    """Madres fuera de alcance: la tanda se identifica por su raza."""
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        raza = s.execute(text("SELECT raza_id FROM tanda LIMIT 1")).scalar_one()
        s.execute(
            text(
                "INSERT INTO tanda (raza_id, fecha_traslarve) "
                "VALUES (:raza, '2026-10-16')"
            ),
            {"raza": raza},
        )
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    ("fila", "posicion"),
    [(1, None), (None, 2), (0, 1), (1, -1)],
    ids=["sin_posicion", "sin_fila", "fila_cero", "posicion_negativa"],
)
def test_continuadora_las_dos_y_positivas(
    criaderos: tuple[Criadero, Criadero], fila: int | None, posicion: int | None
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_tanda_continuadora"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "UPDATE tanda SET fila_continuadora = :fila, "
                    "posicion_continuadora = :posicion"
                ),
                {"fila": fila, "posicion": posicion},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_continuadora_completa_se_acepta(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    tocadas = _con_celdas(
        a.tenant, "UPDATE tanda SET fila_continuadora = 3, posicion_continuadora = 7"
    )
    assert tocadas == 1


@pytest.mark.usefixtures("con_motor_app")
def test_celda_descartada_necesita_fecha_de_descarte(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_fecha_descarte"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET estado = 'descartada'"))


@pytest.mark.usefixtures("con_motor_app")
def test_fecha_de_descarte_solo_en_descartada(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_fecha_descarte"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET fecha_descarte = '2026-10-10'"))


@pytest.mark.usefixtures("con_motor_app")
def test_celda_descartada_con_fecha(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    tocadas = _con_celdas(
        a.tenant,
        "UPDATE celda SET estado = 'descartada', fecha_descarte = '2026-10-10'",
    )
    assert tocadas == CELDAS_POR_TANDA


@pytest.mark.usefixtures("con_motor_app")
def test_celda_extraviada_necesita_fecha_de_extravio(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_fecha_extravio"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "UPDATE celda SET estado = 'extraviada', nucleo_id = :nucleo, "
                    "fecha_introduccion = '2026-10-15'"
                ),
                {"nucleo": a.nucleo},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_celda_en_banco_solo_si_esta_enjaulada(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="ck_celda_banco_al_enjaular"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE celda SET banco_id = :banco"), {"banco": a.banco})


@pytest.mark.usefixtures("con_motor_app")
def test_celda_enjaulada_en_su_banco(criaderos: tuple[Criadero, Criadero]) -> None:
    a, _ = criaderos
    tocadas = _con_celdas(
        a.tenant,
        "UPDATE celda SET estado = 'enjaulada', nucleo_id = :nucleo, "
        "fecha_introduccion = '2026-10-15', banco_id = :banco",
        nucleo=a.nucleo,
        banco=a.banco,
    )
    assert tocadas == CELDAS_POR_TANDA


@pytest.mark.usefixtures("con_motor_app")
def test_celda_no_puede_apuntar_a_un_banco_de_otro_tenant(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    """Segunda capa: la clave foranea compuesta (tenant_id, id)."""
    a, b = criaderos
    with pytest.raises(IntegrityError, match="fk_celda_banco_mismo_tenant"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(
                text(
                    "UPDATE celda SET estado = 'enjaulada', nucleo_id = :nucleo, "
                    "fecha_introduccion = '2026-10-15', banco_id = :banco"
                ),
                {"nucleo": a.nucleo, "banco": b.banco},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_banco_capacidad_por_defecto_y_positiva(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a.tenant) as s:
        capacidad = s.execute(
            text("SELECT capacidad_max FROM banco WHERE id = :id"), {"id": a.banco}
        ).scalar_one()
        assert capacidad == 276  # confirmado por la clienta el 16/10
        raise _Deshacer
    with pytest.raises(IntegrityError, match="ck_banco_capacidad_max"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("UPDATE banco SET capacidad_max = 0"))


@pytest.mark.usefixtures("con_motor_app")
def test_identificador_de_banco_unico_por_criadero(
    criaderos: tuple[Criadero, Criadero],
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="uq_banco_tenant_identificador"):
        with db.sesion_de_tenant(a.tenant) as s:
            s.execute(text("INSERT INTO banco (identificador) VALUES ('B1')"))
