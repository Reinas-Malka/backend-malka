"""Tabla cliente de la migracion 0005 contra un PostgreSQL real (#32).

Cubre el aislamiento por tenant (incluido el caso de fuga entre tenants que
pide AGENTS.md) y las restricciones que la base repite de ClienteCrear. Esos
CHECK no deberian saltar nunca en el flujo normal: Pydantic rechaza antes.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db
from app.schemas.clientes import ClienteCrear
from tests.conftest import crear_tenant, fijar_tenant

pytestmark = pytest.mark.integration

CUIT = "20123456786"
# Otro CUIT valido, para los inserts que no tienen que chocar con CUIT.
OTRO_CUIT = "30123456781"

INSERTAR_CLIENTE = text(
    "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
    "VALUES (:nombre, :pais, :tipo, :condicion_iva, :cuit_o_tax_id)"
)

NACIONAL = {
    "nombre": "Apicola del Sur",
    "pais": "AR",
    "tipo": "nacional",
    "condicion_iva": "monotributo",
    "cuit_o_tax_id": CUIT,
}

EXPORTACION = {
    "nombre": "Bienenzucht GmbH",
    "pais": "DE",
    "tipo": "exportacion",
    "condicion_iva": "cliente_exterior",
    "cuit_o_tax_id": "DE123456789",
}


class _Deshacer(Exception):
    """Corta la sesion para que haga rollback."""


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Dos criaderos con el mismo cliente nacional, cargados como duenio."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
            # FORCE RLS: ni el duenio escribe sin tenant fijado.
            fijar_tenant(c, tenant)
            c.execute(INSERTAR_CLIENTE, NACIONAL)
    yield a, b
    # El duenio puede borrar (la API no).
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM cliente WHERE true"))
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


# --- aislamiento por tenant ---


@pytest.mark.usefixtures("criaderos")
def test_sin_set_local_cero_filas(motor_app: Engine) -> None:
    with motor_app.connect() as c:
        assert c.execute(text("SELECT count(*) FROM cliente")).scalar() == 0


@pytest.mark.usefixtures("con_motor_app")
def test_cada_tenant_ve_solo_sus_clientes(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Fuga entre tenants (AGENTS.md, regla 5): B no ve los clientes de A."""
    for tenant in criaderos:
        with db.sesion_de_tenant(tenant) as s:
            tenants = s.execute(text("SELECT tenant_id FROM cliente")).scalars()
            assert list(tenants) == [tenant]


@pytest.mark.usefixtures("con_motor_app")
def test_no_puede_escribir_en_otro_tenant(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = criaderos
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.sesion_de_tenant(a) as s:
            s.execute(
                text(
                    "INSERT INTO cliente "
                    "(tenant_id, nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
                    "VALUES (:otro, :nombre, :pais, :tipo, :condicion_iva, "
                    ":cuit_o_tax_id)"
                ),
                {**NACIONAL, "otro": b, "cuit_o_tax_id": OTRO_CUIT},
            )


@pytest.mark.usefixtures("con_motor_app")
def test_tenant_id_lo_completa_postgres(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a) as s:
        insertado = s.execute(
            text(f"{INSERTAR_CLIENTE.text} RETURNING tenant_id"), EXPORTACION
        ).scalar()
        assert insertado == a
        raise _Deshacer


def test_rol_app_sin_delete(motor_owner: Engine) -> None:
    """Borrado logico: un cliente se desactiva, no se borra."""
    with motor_owner.connect() as c:
        puede = c.execute(
            text("SELECT has_table_privilege('malka_app', 'cliente', 'DELETE')")
        ).scalar()
    assert puede is False


# --- CUIT unico por criadero ---


@pytest.mark.usefixtures("con_motor_app")
def test_cuit_repetido_en_el_mismo_criadero(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """La fixture ya cargo este CUIT en A. La API lo va a traducir a 409."""
    a, _ = criaderos
    with pytest.raises(IntegrityError, match="uq_cliente_tenant_cuit_o_tax_id"):
        with db.sesion_de_tenant(a) as s:
            s.execute(INSERTAR_CLIENTE, {**NACIONAL, "nombre": "Duplicado"})


@pytest.mark.usefixtures("con_motor_app")
def test_mismo_cuit_en_otro_criadero(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """La fixture cargo el mismo CUIT en A y en B sin error."""
    for tenant in criaderos:
        with db.sesion_de_tenant(tenant) as s:
            cuits = s.execute(text("SELECT cuit_o_tax_id FROM cliente")).scalars()
            assert list(cuits) == [CUIT]


# --- restricciones que la base repite de ClienteCrear ---


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    "datos",
    [
        pytest.param(NACIONAL, id="nacional"),
        pytest.param(EXPORTACION, id="exportacion"),
    ],
)
def test_acepta_cliente_valido(
    criaderos: tuple[uuid.UUID, uuid.UUID], datos: dict[str, str]
) -> None:
    a, _ = criaderos
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a) as s:
        s.execute(INSERTAR_CLIENTE, {**datos, "cuit_o_tax_id": OTRO_CUIT})
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
def test_pais_en_minuscula_por_la_api_se_guarda(
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """ClienteCrear normaliza "ar" a "AR", asi que la base lo acepta."""
    a, _ = criaderos
    cliente = ClienteCrear(**{**NACIONAL, "cuit_o_tax_id": OTRO_CUIT, "pais": "ar"})
    with pytest.raises(_Deshacer), db.sesion_de_tenant(a) as s:
        pais = s.execute(
            text(f"{INSERTAR_CLIENTE.text} RETURNING pais"), cliente.model_dump()
        ).scalar()
        assert pais == "AR"
        raise _Deshacer


@pytest.mark.usefixtures("con_motor_app")
@pytest.mark.parametrize(
    ("base", "cambios", "restriccion"),
    [
        pytest.param(
            NACIONAL, {"pais": "UY"}, "ck_cliente_nacional", id="nacional_fuera_de_ar"
        ),
        pytest.param(
            NACIONAL,
            {"condicion_iva": "cliente_exterior"},
            "ck_cliente_nacional",
            id="nacional_con_condicion_exterior",
        ),
        pytest.param(
            NACIONAL,
            {"cuit_o_tax_id": "20-12345678-6"},
            "ck_cliente_nacional",
            id="cuit_con_guiones",
        ),
        pytest.param(
            EXPORTACION,
            {"pais": "AR"},
            "ck_cliente_exportacion",
            id="exportacion_desde_ar",
        ),
        pytest.param(
            EXPORTACION,
            {"condicion_iva": "exento"},
            "ck_cliente_exportacion",
            id="exportacion_con_condicion_local",
        ),
        # Con exportacion, "de" viola solo el formato. Un nacional con "ar"
        # violaria tambien ck_cliente_nacional y Postgres reporta una sola.
        pytest.param(
            EXPORTACION, {"pais": "de"}, "ck_cliente_pais", id="pais_en_minuscula"
        ),
        pytest.param(
            NACIONAL, {"tipo": "otro"}, "ck_cliente_tipo", id="tipo_inexistente"
        ),
        pytest.param(
            NACIONAL,
            {"condicion_iva": "otra"},
            "ck_cliente_condicion_iva",
            id="condicion_inexistente",
        ),
        pytest.param(
            NACIONAL, {"pais": "ar"}, "ck_cliente_nacional", id="nacional_ar_minuscula"
        ),
    ],
)
def test_rechaza_cliente_invalido(
    criaderos: tuple[uuid.UUID, uuid.UUID],
    base: dict[str, str],
    cambios: dict[str, str],
    restriccion: str,
) -> None:
    a, _ = criaderos
    with pytest.raises(IntegrityError, match=restriccion):
        with db.sesion_de_tenant(a) as s:
            s.execute(INSERTAR_CLIENTE, {**base, "cuit_o_tax_id": OTRO_CUIT, **cambios})
