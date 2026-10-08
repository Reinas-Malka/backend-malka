"""Endpoints de clientes de punta a punta (#32).

Requests HTTP reales contra la app de produccion: token firmado (#21), sesion
con el tenant del token (#19), validacion (schemas) y PostgreSQL con RLS
(migracion 0005). Cubre tambien el repositorio, que no tiene tests propios.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/clientes"

NACIONAL: dict[str, Any] = {
    "nombre": "Apicola del Sur",
    "pais": "ar",
    "tipo": "nacional",
    "condicion_iva": "monotributo",
    "cuit_o_tax_id": "20-12345678-6",
}

EXPORTACION: dict[str, Any] = {
    "nombre": "Bienenzucht GmbH",
    "pais": "DE",
    "tipo": "exportacion",
    "condicion_iva": "cliente_exterior",
    "cuit_o_tax_id": "DE123456789",
}


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Dos tenants vacios; cada test carga los clientes que necesita."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
    yield a, b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM cliente WHERE true"))
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_clientes(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> Iterator[None]:
    """Cada test arranca sin clientes: los datos de uno no afectan a otro."""
    yield
    for tenant in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM cliente WHERE true"))


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    """Headers de un usuario del criadero con ese rol."""
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def crear(api: TestClient, tenant: uuid.UUID, datos: dict[str, Any]) -> dict[str, Any]:
    respuesta = api.post(URL, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    cuerpo: dict[str, Any] = respuesta.json()
    return cuerpo


# --- alta ---


def test_crea_un_cliente_nacional(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    cuerpo = crear(api, a, NACIONAL)

    assert uuid.UUID(cuerpo["id"])
    assert cuerpo["pais"] == "AR"
    assert cuerpo["cuit_o_tax_id"] == "20123456786"
    assert cuerpo["activo"] is True
    assert cuerpo["tipo_documento_por_defecto"] == "factura_b"


def test_crea_un_cliente_de_exportacion(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    cuerpo = crear(api, a, EXPORTACION)

    assert cuerpo["tipo_documento_por_defecto"] == "factura_e"


def test_alta_invalida_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    datos = {**NACIONAL, "cuit_o_tax_id": "20-12345678-5"}
    respuesta = api.post(URL, json=datos, headers=como(a))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"


def test_alta_con_tenant_id_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """El tenant sale del token, nunca del body."""
    a, b = criaderos
    datos = {**NACIONAL, "tenant_id": str(b)}
    respuesta = api.post(URL, json=datos, headers=como(a))

    assert respuesta.status_code == 422


def test_cuit_repetido_es_409(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    crear(api, a, NACIONAL)
    respuesta = api.post(URL, json=NACIONAL, headers=como(a))

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "cliente_duplicado"


def test_mismo_cuit_en_otro_criadero(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    crear(api, a, NACIONAL)
    crear(api, b, NACIONAL)


# --- permisos ---


def test_sin_token_es_401(api: TestClient) -> None:
    respuesta = api.get(URL)

    assert respuesta.status_code == 401


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_solo_admin_y_ventas_escriben(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], rol: str
) -> None:
    a, _ = criaderos
    creado = crear(api, a, NACIONAL)
    ruta = f"{URL}/{creado['id']}"

    alta = api.post(URL, json=EXPORTACION, headers=como(a, rol))
    edicion = api.patch(ruta, json={"nombre": "Otro"}, headers=como(a, rol))
    baja = api.delete(ruta, headers=como(a, rol))

    assert [alta.status_code, edicion.status_code, baja.status_code] == [403] * 3


@pytest.mark.parametrize("rol", ["admin", "ventas", "produccion", "lectura"])
def test_todos_los_roles_leen(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], rol: str
) -> None:
    a, _ = criaderos
    creado = crear(api, a, NACIONAL)

    lista = api.get(URL, headers=como(a, rol))
    uno = api.get(f"{URL}/{creado['id']}", headers=como(a, rol))

    assert [lista.status_code, uno.status_code] == [200, 200]


def test_admin_escribe(api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = criaderos
    respuesta = api.post(URL, json=NACIONAL, headers=como(a, "admin"))

    assert respuesta.status_code == 201


# --- lectura y aislamiento entre criaderos ---


def test_lista_solo_los_clientes_del_criadero(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Fuga entre tenants (AGENTS.md, regla 5), a traves de la API."""
    a, b = criaderos
    crear(api, a, NACIONAL)
    crear(api, b, EXPORTACION)

    de_a = api.get(URL, headers=como(a)).json()
    de_b = api.get(URL, headers=como(b)).json()

    assert [c["nombre"] for c in de_a] == [NACIONAL["nombre"]]
    assert [c["nombre"] for c in de_b] == [EXPORTACION["nombre"]]


def test_cliente_de_otro_criadero_es_404(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """404 y no 403: decir "no tenes permiso" confirmaria que existe."""
    a, b = criaderos
    ruta = f"{URL}/{crear(api, a, NACIONAL)['id']}"

    respuestas = [
        api.get(ruta, headers=como(b)),
        api.patch(ruta, json={"nombre": "Robado"}, headers=como(b)),
        api.delete(ruta, headers=como(b)),
    ]

    assert [r.status_code for r in respuestas] == [404] * 3
    assert api.get(ruta, headers=como(a)).json()["nombre"] == NACIONAL["nombre"]


def test_cliente_inexistente_es_404(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    respuesta = api.get(f"{URL}/{uuid.uuid4()}", headers=como(a))

    assert respuesta.status_code == 404
    assert codigo(respuesta) == "cliente_no_encontrado"


def test_id_que_no_es_uuid_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    respuesta = api.get(f"{URL}/no-es-un-uuid", headers=como(a))

    assert respuesta.status_code == 422


def test_lista_ordenada_por_nombre(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    crear(api, a, EXPORTACION)
    crear(api, a, NACIONAL)

    nombres = [c["nombre"] for c in api.get(URL, headers=como(a)).json()]

    assert nombres == sorted(nombres)


# --- edicion ---


def test_edita_un_campo(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    ruta = f"{URL}/{crear(api, a, NACIONAL)['id']}"

    respuesta = api.patch(ruta, json={"nombre": "Apicola del Norte"}, headers=como(a))

    assert respuesta.status_code == 200
    assert respuesta.json()["nombre"] == "Apicola del Norte"
    assert respuesta.json()["cuit_o_tax_id"] == "20123456786"


def test_editar_valida_contra_lo_guardado(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Solo cambia el tipo: el pais guardado (AR) no sirve para exportacion."""
    a, _ = criaderos
    ruta = f"{URL}/{crear(api, a, NACIONAL)['id']}"

    respuesta = api.patch(ruta, json={"tipo": "exportacion"}, headers=como(a))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"


def test_pasa_de_nacional_a_exportacion(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    ruta = f"{URL}/{crear(api, a, NACIONAL)['id']}"
    cambios = {k: v for k, v in EXPORTACION.items() if k != "nombre"}

    respuesta = api.patch(ruta, json=cambios, headers=como(a))

    assert respuesta.status_code == 200
    assert respuesta.json()["tipo_documento_por_defecto"] == "factura_e"


def test_editar_a_un_cuit_repetido_es_409(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    crear(api, a, NACIONAL)
    otro = crear(api, a, {**NACIONAL, "cuit_o_tax_id": "30-12345678-1"})

    respuesta = api.patch(
        f"{URL}/{otro['id']}",
        json={"cuit_o_tax_id": NACIONAL["cuit_o_tax_id"]},
        headers=como(a),
    )

    assert respuesta.status_code == 409


# --- baja logica ---


def test_baja_logica_y_reactivacion(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    ruta = f"{URL}/{crear(api, a, NACIONAL)['id']}"

    baja = api.delete(ruta, headers=como(a))
    activos = api.get(URL, headers=como(a)).json()
    todos = api.get(URL, params={"incluir_inactivos": True}, headers=como(a)).json()
    desactivado = api.get(ruta, headers=como(a)).json()

    assert baja.status_code == 204
    assert activos == []
    assert [c["activo"] for c in todos] == [False]
    assert desactivado["activo"] is False

    reactivado = api.patch(ruta, json={"activo": True}, headers=como(a))

    assert reactivado.json()["activo"] is True
    assert len(api.get(URL, headers=como(a)).json()) == 1


def test_un_cliente_desactivado_sigue_ocupando_su_cuit(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Para volver a usarlo se reactiva; no se da de alta otra vez."""
    a, _ = criaderos
    api.delete(f"{URL}/{crear(api, a, NACIONAL)['id']}", headers=como(a))

    respuesta = api.post(URL, json=NACIONAL, headers=como(a))

    assert respuesta.status_code == 409
