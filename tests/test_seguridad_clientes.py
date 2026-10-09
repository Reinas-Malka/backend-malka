"""Ataques contra la API de clientes (#32): que fallen sin romper nada.

Cada test manda lo que mandaria un atacante con un token valido de su propio
criadero y verifica dos cosas: la respuesta es controlada (nunca un 500) y no
se filtra ni se modifica nada de otro criadero.
"""

import io
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import db
from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = pytest.mark.integration

URL = "/api/v1/clientes"

# Un tax id de exportacion es texto libre: el lugar mas obvio para inyectar.
EXPORTACION: dict[str, Any] = {
    "nombre": "Bienenzucht GmbH",
    "pais": "DE",
    "tipo": "exportacion",
    "condicion_iva": "cliente_exterior",
    "cuit_o_tax_id": "DE123456789",
}

INYECCIONES_SQL = [
    "'; DROP TABLE cliente; --",
    "' OR '1'='1",
    "x'); DELETE FROM cliente; --",
    "'; RESET app.tenant_id; --",
    "\\'; UPDATE cliente SET activo = false; --",
    "1; SELECT pg_sleep(5)",
]


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A es el atacante; B tiene un cliente que A no tiene que poder tocar."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
        fijar_tenant(c, b)
        c.execute(
            text(
                "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, "
                "cuit_o_tax_id) VALUES ('Victima', 'AR', 'nacional', "
                "'monotributo', '20123456786')"
            )
        )
    yield a, b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM cliente WHERE true"))
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_clientes_del_atacante(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> Iterator[None]:
    yield
    with motor_owner.begin() as c:
        fijar_tenant(c, criaderos[0])
        c.execute(text("DELETE FROM cliente WHERE true"))


@pytest.fixture
def api(cognito: None, con_motor_app: None) -> TestClient:
    # raise_server_exceptions=False: un 500 se ve como respuesta, no como
    # excepcion, igual que lo veria el atacante.
    return TestClient(app, raise_server_exceptions=False)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def victima_intacta(motor_owner: Engine, b: uuid.UUID) -> bool:
    with motor_owner.begin() as c:
        fijar_tenant(c, b)
        filas = c.execute(text("SELECT nombre, activo FROM cliente")).all()
    return [tuple(f) for f in filas] == [("Victima", True)]


# --- inyeccion SQL ---


@pytest.mark.parametrize("ataque", INYECCIONES_SQL)
@pytest.mark.parametrize("campo", ["nombre", "cuit_o_tax_id"])
def test_inyeccion_sql_en_el_alta_se_guarda_como_texto(
    api: TestClient,
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    campo: str,
    ataque: str,
) -> None:
    """Los valores viajan como parametros: el SQL nunca se ejecuta."""
    a, b = criaderos
    respuesta = api.post(URL, json={**EXPORTACION, campo: ataque}, headers=como(a))

    assert respuesta.status_code == 201
    assert respuesta.json()[campo] == ataque
    assert victima_intacta(motor_owner, b)


@pytest.mark.parametrize("ataque", INYECCIONES_SQL)
def test_inyeccion_sql_en_la_edicion(
    api: TestClient,
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    ataque: str,
) -> None:
    a, b = criaderos
    creado = api.post(URL, json=EXPORTACION, headers=como(a)).json()

    respuesta = api.patch(
        f"{URL}/{creado['id']}", json={"nombre": ataque}, headers=como(a)
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["nombre"] == ataque
    assert victima_intacta(motor_owner, b)


@pytest.mark.parametrize(
    "ruta",
    [
        f"{URL}/' OR '1'='1",
        f"{URL}/1; DROP TABLE cliente",
        f"{URL}/00000000-0000-0000-0000-000000000000' OR 'a'='a",
    ],
)
def test_inyeccion_sql_en_la_ruta(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], ruta: str
) -> None:
    """El id tiene que ser un UUID: cualquier otra cosa es 422 antes de la base."""
    a, _ = criaderos
    assert api.get(ruta, headers=como(a)).status_code == 422


@pytest.mark.parametrize("valor", ["true OR 1=1", "1; DROP TABLE cliente"])
def test_inyeccion_sql_en_el_query_string(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], valor: str
) -> None:
    a, _ = criaderos
    respuesta = api.get(URL, params={"incluir_inactivos": valor}, headers=como(a))

    assert respuesta.status_code == 422


# --- cambiar de criadero ---


def test_tenant_id_en_el_body_de_la_edicion(
    api: TestClient,
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Mover un cliente propio al criadero B: el body no admite tenant_id."""
    a, b = criaderos
    creado = api.post(URL, json=EXPORTACION, headers=como(a)).json()

    respuesta = api.patch(
        f"{URL}/{creado['id']}", json={"tenant_id": str(b)}, headers=como(a)
    )

    assert respuesta.status_code == 422
    assert victima_intacta(motor_owner, b)


@pytest.mark.parametrize(
    "header", ["X-Tenant-Id", "X-Amz-Tenant", "Tenant-Id", "X-Forwarded-Tenant"]
)
def test_un_header_no_cambia_el_criadero(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], header: str
) -> None:
    """El criadero sale solo del token firmado, nunca de otro header."""
    a, b = criaderos
    headers = {**como(a), header: str(b)}

    assert api.get(URL, headers=headers).json() == []


@pytest.mark.parametrize(
    "campo", [{"id": str(uuid.uuid4())}, {"activo": False}, {"creado_en": "2020"}]
)
def test_no_se_pueden_fijar_campos_internos_en_el_alta(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], campo: dict[str, Any]
) -> None:
    """Asignacion masiva: solo entran los campos de ClienteCrear."""
    a, _ = criaderos
    respuesta = api.post(URL, json={**EXPORTACION, **campo}, headers=como(a))

    assert respuesta.status_code == 422


def test_otro_criadero_y_un_id_inexistente_responden_igual(
    api: TestClient, motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Si las respuestas difirieran, A podria saber que ids existen en B."""
    a, b = criaderos
    with motor_owner.begin() as c:
        fijar_tenant(c, b)
        de_b = c.execute(text("SELECT id FROM cliente")).scalar_one()

    ajeno = api.get(f"{URL}/{de_b}", headers=como(a)).json()["error"]
    inexistente = api.get(f"{URL}/{uuid.uuid4()}", headers=como(a)).json()["error"]

    assert {**ajeno, "request_id": None} == {**inexistente, "request_id": None}


# --- datos malformados: siempre 422, nunca 500 ---


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"nombre": "a\x00b"}, id="byte_nulo_en_nombre"),
        pytest.param({"cuit_o_tax_id": "DE\x00123"}, id="byte_nulo_en_tax_id"),
        pytest.param({"nombre": "linea\nnueva"}, id="salto_de_linea"),
        pytest.param({"nombre": "x" * 201}, id="nombre_largo"),
        pytest.param({"cuit_o_tax_id": "x" * 51}, id="tax_id_largo"),
        pytest.param({"nombre": "x" * 1_000_000}, id="nombre_de_un_mega"),
        pytest.param({"nombre": ["lista"]}, id="nombre_lista"),
        pytest.param({"nombre": {"$ne": ""}}, id="operador_nosql"),
        pytest.param({"pais": 54}, id="pais_numero"),
        pytest.param({"pais": "ＤＥ"}, id="pais_en_ancho_completo"),
        pytest.param({"tipo": None}, id="tipo_nulo"),
    ],
)
def test_datos_malformados_en_el_alta(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], cambios: dict[str, Any]
) -> None:
    a, _ = criaderos
    respuesta = api.post(URL, json={**EXPORTACION, **cambios}, headers=como(a))

    assert respuesta.status_code == 422


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"nombre": "a\x00b"}, id="byte_nulo"),
        pytest.param({"nombre": None}, id="nombre_nulo"),
        pytest.param({"activo": "quizas"}, id="activo_texto"),
        pytest.param({"tipo": "nacional"}, id="tipo_sin_cuit"),
    ],
)
def test_datos_malformados_en_la_edicion(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], cambios: dict[str, Any]
) -> None:
    a, _ = criaderos
    creado = api.post(URL, json=EXPORTACION, headers=como(a)).json()

    respuesta = api.patch(f"{URL}/{creado['id']}", json=cambios, headers=como(a))

    assert respuesta.status_code == 422


def test_activo_nulo_en_la_edicion_no_cambia_nada(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = criaderos
    creado = api.post(URL, json=EXPORTACION, headers=como(a)).json()

    respuesta = api.patch(
        f"{URL}/{creado['id']}", json={"activo": None}, headers=como(a)
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["activo"] is True


def test_json_roto(api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = criaderos
    respuesta = api.post(
        URL,
        content=b'{"nombre": "x",',
        headers={**como(a), "Content-Type": "application/json"},
    )

    assert respuesta.status_code == 422


# --- lo que se devuelve y lo que se loguea ---


def test_un_script_se_devuelve_como_json_y_no_como_html(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """La API guarda el texto tal cual; escaparlo al mostrarlo es del frontend."""
    a, _ = criaderos
    ataque = "<script>alert(1)</script>"
    respuesta = api.post(URL, json={**EXPORTACION, "nombre": ataque}, headers=como(a))

    assert respuesta.headers["content-type"] == "application/json"
    assert respuesta.json()["nombre"] == ataque


def test_el_cuit_no_aparece_en_los_logs(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], logs: io.StringIO
) -> None:
    a, _ = criaderos
    nacional = {
        **EXPORTACION,
        "pais": "AR",
        "tipo": "nacional",
        "condicion_iva": "monotributo",
    }
    api.post(URL, json={**nacional, "cuit_o_tax_id": "27-87654321-5"}, headers=como(a))
    api.post(URL, json={**nacional, "cuit_o_tax_id": "20123456786"}, headers=como(a))

    salida = logs.getvalue()
    assert "87654321" not in salida
    assert "12345678" not in salida


@pytest.mark.usefixtures("config_limpia")
def test_el_motor_de_la_api_no_lleva_los_datos_al_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con hide_parameters, un error de la base trae el SQL pero no los valores."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:1/x")
    db.obtener_motor.cache_clear()
    try:
        assert db.obtener_motor().hide_parameters is True
    finally:
        db.obtener_motor.cache_clear()
