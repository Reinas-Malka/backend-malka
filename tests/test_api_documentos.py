"""Subida y descarga de documentos de punta a punta (#102, DoD).

Requests reales contra la app con PostgreSQL y RLS (migracion 0011). S3 no
se toca: el firmador se reemplaza por uno falso que devuelve URLs de prueba.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app
from app.routers.documentos import obtener_firmador
from tests.conftest import crear_tenant, emitir, fijar_tenant
from tests.test_almacenamiento import FirmadorFalso

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/documentos"
PDF = {"nombre": "Factura E 41.pdf", "tipo": "application/pdf", "tamano": 2048}
TABLAS = ("documento_embarque", "embarque", "documento", "idempotencia")


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    for tenant in (a, b):
        with motor_owner.begin() as c:
            crear_tenant(c, tenant)
    yield a, b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def entorno(
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FirmadorFalso]:
    """Bucket configurado, firmador falso y tablas limpias en cada test."""
    monkeypatch.setenv("BUCKET_DOCUMENTOS", "bucket-de-prueba")
    firmador = FirmadorFalso()
    app.dependency_overrides[obtener_firmador] = lambda: firmador
    yield firmador
    app.dependency_overrides.pop(obtener_firmador, None)
    for tenant in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in TABLAS:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def documentos_de(motor_owner: Engine, tenant: uuid.UUID) -> list[Any]:
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        return list(c.execute(text("SELECT * FROM documento")).mappings())


def subir(api: TestClient, tenant: uuid.UUID, **cambios: Any) -> Any:
    return api.post(f"{URL}/subidas", json={**PDF, **cambios}, headers=como(tenant))


# --- DoD ---


def test_subida_ok_devuelve_la_url_y_registra_el_documento(
    api: TestClient,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    motor_owner: Engine,
    entorno: FirmadorFalso,
) -> None:
    tenant, _ = criaderos

    respuesta = subir(api, tenant)

    assert respuesta.status_code == 201, respuesta.json()
    cuerpo = respuesta.json()
    assert cuerpo["clave"].startswith(f"documentos/{tenant}/")
    assert cuerpo["clave"].endswith("-Factura-E-41.pdf")
    assert cuerpo["url_subida"] == f"https://s3.test/{cuerpo['clave']}?firma=falsa"
    assert cuerpo["expira_en_segundos"] == 300
    [llamada] = entorno.llamadas
    assert llamada["params"]["ContentType"] == "application/pdf"
    [fila] = documentos_de(motor_owner, tenant)
    assert fila["archivo_s3_key"] == cuerpo["clave"]
    assert (fila["nombre"], fila["tamano_bytes"]) == ("Factura E 41.pdf", 2048)
    # cargado_por del dominio: el sub de Cognito, no un email.
    assert fila["subido_por"] == "usuario-1"


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"tipo": "application/zip"}, id="tipo_invalido"),
        pytest.param({"tipo": "text/html"}, id="html"),
        pytest.param({"tamano": 10 * 1024 * 1024 + 1}, id="mas_de_10_mb"),
        pytest.param({"tamano": 0}, id="vacio"),
        pytest.param({"nombre": ""}, id="sin_nombre"),
        pytest.param({"tenant_id": str(uuid.uuid4())}, id="tenant_id_en_el_cuerpo"),
    ],
)
def test_pedido_invalido_es_422_y_no_registra_nada(
    api: TestClient,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    motor_owner: Engine,
    cambios: dict[str, Any],
) -> None:
    tenant, _ = criaderos

    respuesta = subir(api, tenant, **cambios)

    assert respuesta.status_code == 422
    assert documentos_de(motor_owner, tenant) == []


def test_documento_de_otro_criadero_es_404(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    clave_de_b = subir(api, b).json()["clave"]

    de_otro = api.get(f"{URL}/descarga", params={"clave": clave_de_b}, headers=como(a))
    inexistente = api.get(
        f"{URL}/descarga",
        params={"clave": f"documentos/{a}/no-existe.pdf"},
        headers=como(a),
    )

    assert de_otro.status_code == inexistente.status_code == 404
    assert codigo(de_otro) == codigo(inexistente) == "documento_no_encontrado"


# --- Descarga, roles y configuración ---


@pytest.mark.parametrize("rol", ["admin", "ventas", "produccion", "lectura"])
def test_cualquier_rol_descarga_lo_de_su_criadero(
    api: TestClient,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    entorno: FirmadorFalso,
    rol: str,
) -> None:
    tenant, _ = criaderos
    clave = subir(api, tenant).json()["clave"]

    respuesta = api.get(
        f"{URL}/descarga", params={"clave": clave}, headers=como(tenant, rol)
    )

    assert respuesta.status_code == 200, respuesta.json()
    assert respuesta.json()["url_descarga"].startswith(f"https://s3.test/{clave}")
    assert entorno.llamadas[-1]["metodo"] == "get_object"


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_solo_admin_y_ventas_suben(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], rol: str
) -> None:
    tenant, _ = criaderos

    respuesta = api.post(f"{URL}/subidas", json=PDF, headers=como(tenant, rol))

    assert respuesta.status_code == 403


def test_sin_bucket_es_503_y_no_queda_un_documento_huerfano(
    api: TestClient,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    motor_owner: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, _ = criaderos
    monkeypatch.delenv("BUCKET_DOCUMENTOS")

    respuesta = subir(api, tenant)

    assert respuesta.status_code == 503
    assert codigo(respuesta) == "subida_no_configurada"
    assert documentos_de(motor_owner, tenant) == []


def test_la_misma_clave_de_idempotencia_registra_un_solo_documento(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], motor_owner: Engine
) -> None:
    tenant, _ = criaderos
    headers = {**como(tenant), "Idempotency-Key": str(uuid.uuid4())}

    primera = api.post(f"{URL}/subidas", json=PDF, headers=headers)
    segunda = api.post(f"{URL}/subidas", json=PDF, headers=headers)

    assert primera.status_code == segunda.status_code == 201
    assert primera.json()["clave"] == segunda.json()["clave"]
    assert len(documentos_de(motor_owner, tenant)) == 1


def test_la_clave_subida_se_puede_registrar_en_un_embarque(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """El flujo de la demo: subir el PDF y registrarlo como documento del embarque."""
    tenant, _ = criaderos
    clave = subir(api, tenant).json()["clave"]
    embarque = api.post(
        "/api/v1/embarques",
        json={"destino_pais": "CL", "fecha_vuelo": "2026-11-20"},
        headers=como(tenant),
    ).json()

    respuesta = api.post(
        f"/api/v1/embarques/{embarque['id']}/documentos",
        json={
            "tipo": "factura_e",
            "numero": "E 0003-00000041",
            "archivo_s3_key": clave,
            "fecha": "2026-11-18",
        },
        headers=como(tenant),
    )

    assert respuesta.status_code == 201, respuesta.json()


def test_la_base_no_acepta_una_key_de_otro_criadero(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    with pytest.raises(Exception, match="ck_documento_archivo_del_tenant"):
        with motor_owner.begin() as c:
            fijar_tenant(c, a)
            c.execute(
                text(
                    "INSERT INTO documento (archivo_s3_key, nombre, tipo_contenido, "
                    "tamano_bytes, subido_por) VALUES (:key, 'x.pdf', "
                    "'application/pdf', 10, 'sub')"
                ),
                {"key": f"documentos/{b}/x.pdf"},
            )
