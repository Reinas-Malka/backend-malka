"""Endpoints de embarques de punta a punta (#109, DoD).

Requests HTTP reales contra la app: token firmado, sesion con el tenant del
token y PostgreSQL con RLS (migracion 0010). Cubre tambien el repositorio,
que no tiene tests propios.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.services.exportacion.embarques import TipoDocumentoEmbarque
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/embarques"
AWB = "081-12345675"
OTRA_AWB = "057-11111111"

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = ("documento_embarque", "embarque", "idempotencia")


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A es quien usa la API; B tiene sus propios embarques."""
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
def sin_embarques(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> Iterator[None]:
    """Cada test arranca sin embarques en ninguno de los dos criaderos."""
    yield
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


def embarque(**cambios: Any) -> dict[str, Any]:
    return {"destino_pais": "NZ", "fecha_vuelo": "2026-11-20", **cambios}


def documento(
    tenant: uuid.UUID, tipo: TipoDocumentoEmbarque, **cambios: Any
) -> dict[str, Any]:
    return {
        "tipo": tipo.value,
        "numero": f"{tipo.value.upper()}-0001",
        "archivo_s3_key": f"documentos/{tenant}/{tipo.value}.pdf",
        "fecha": "2026-11-18",
        **cambios,
    }


def crear(api: TestClient, tenant: uuid.UUID, **cambios: Any) -> str:
    respuesta = api.post(URL, json=embarque(**cambios), headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


def cargar(
    api: TestClient,
    tenant: uuid.UUID,
    embarque_id: str,
    tipos: list[TipoDocumentoEmbarque],
) -> None:
    for tipo in tipos:
        respuesta = api.post(
            f"{URL}/{embarque_id}/documentos",
            json=documento(tenant, tipo),
            headers=como(tenant),
        )
        assert respuesta.status_code == 201, respuesta.json()


# --- DoD ---


def test_con_los_6_documentos_la_completitud_es_total(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant, awb=AWB)
    cargar(api, tenant, embarque_id, list(TipoDocumentoEmbarque))

    respuesta = api.get(f"{URL}/{embarque_id}/completitud", headers=como(tenant))

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "embarque_id": embarque_id,
        "completo": True,
        "presentes": [tipo.value for tipo in TipoDocumentoEmbarque],
        "faltantes": [],
    }


def test_con_5_documentos_lista_el_faltante(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)
    cargar(
        api,
        tenant,
        embarque_id,
        [
            tipo
            for tipo in TipoDocumentoEmbarque
            if tipo is not TipoDocumentoEmbarque.DTE
        ],
    )

    completitud = api.get(f"{URL}/{embarque_id}/completitud", headers=como(tenant))
    leido = api.get(f"{URL}/{embarque_id}", headers=como(tenant))

    assert completitud.json()["completo"] is False
    assert completitud.json()["faltantes"] == ["dte"]
    assert leido.json()["documentos_cargados"] == 5


def test_embarque_de_otro_criadero_responde_404(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Igual que uno que no existe: no se puede averiguar que hay en B."""
    a, b = criaderos
    de_b = crear(api, b)

    lecturas = [
        api.get(f"{URL}/{de_b}", headers=como(a)),
        api.get(f"{URL}/{de_b}/documentos", headers=como(a)),
        api.get(f"{URL}/{de_b}/completitud", headers=como(a)),
        api.patch(f"{URL}/{de_b}", json={"estado": "cancelado"}, headers=como(a)),
        api.post(
            f"{URL}/{de_b}/documentos",
            json=documento(a, TipoDocumentoEmbarque.DTE),
            headers=como(a),
        ),
    ]
    inexistente = api.get(f"{URL}/{uuid.uuid4()}", headers=como(a))

    assert [r.status_code for r in lecturas] == [404] * 5
    assert {codigo(r) for r in lecturas} == {"embarque_no_encontrado"}
    assert inexistente.json()["error"]["code"] == "embarque_no_encontrado"
    assert api.get(URL, headers=como(a)).json()["items"] == []
    # Y el de B sigue igual.
    assert api.get(f"{URL}/{de_b}", headers=como(b)).json()["estado"] == (
        "en_preparacion"
    )


def test_no_se_registra_un_archivo_de_otro_criadero(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    embarque_id = crear(api, a)

    respuesta = api.post(
        f"{URL}/{embarque_id}/documentos",
        json=documento(b, TipoDocumentoEmbarque.FACTURA_E),
        headers=como(a),
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "archivo_de_otro_criadero"


# --- Alta, lectura y edicion ---


def test_crea_en_preparacion_sin_documentos(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos

    respuesta = api.post(
        URL, json=embarque(destino_pais="de", awb="08112345675"), headers=como(tenant)
    )

    assert respuesta.status_code == 201, respuesta.json()
    creado = respuesta.json()
    assert creado["estado"] == "en_preparacion"
    assert creado["destino_pais"] == "DE"
    assert creado["awb"] == AWB
    assert creado["documentos_cargados"] == 0


def test_awb_repetida_es_409(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    crear(api, a, awb=AWB)
    # La unicidad es por criadero: B puede tener la misma.
    crear(api, b, awb=AWB)

    respuesta = api.post(URL, json=embarque(awb=AWB), headers=como(a))

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "awb_duplicado"


def test_documento_repetido_es_409(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)
    cargar(api, tenant, embarque_id, [TipoDocumentoEmbarque.GUIA_AEREA])

    respuesta = api.post(
        f"{URL}/{embarque_id}/documentos",
        json=documento(tenant, TipoDocumentoEmbarque.GUIA_AEREA, numero="otra"),
        headers=como(tenant),
    )

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "documento_duplicado"


def test_lista_los_documentos_en_el_orden_de_los_6(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)
    cargar(
        api,
        tenant,
        embarque_id,
        [TipoDocumentoEmbarque.DTE, TipoDocumentoEmbarque.FACTURA_E],
    )

    respuesta = api.get(f"{URL}/{embarque_id}/documentos", headers=como(tenant))

    assert respuesta.status_code == 200
    assert [d["tipo"] for d in respuesta.json()["items"]] == ["factura_e", "dte"]
    assert respuesta.json()["siguiente"] is None


def test_lista_por_fecha_de_vuelo_y_pagina(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    for dia in ("2026-11-10", "2026-11-30", "2026-11-20"):
        crear(api, tenant, fecha_vuelo=dia)

    primera = api.get(URL, params={"limite": 2}, headers=como(tenant)).json()
    segunda = api.get(
        URL, params={"limite": 2, "cursor": primera["siguiente"]}, headers=como(tenant)
    ).json()

    assert [e["fecha_vuelo"] for e in primera["items"]] == ["2026-11-30", "2026-11-20"]
    assert [e["fecha_vuelo"] for e in segunda["items"]] == ["2026-11-10"]
    assert segunda["siguiente"] is None


def test_filtra_por_estado(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    crear(api, tenant)
    cancelado = crear(api, tenant, fecha_vuelo="2026-12-01")
    api.patch(f"{URL}/{cancelado}", json={"estado": "cancelado"}, headers=como(tenant))

    respuesta = api.get(URL, params={"estado": "cancelado"}, headers=como(tenant))

    assert [e["id"] for e in respuesta.json()["items"]] == [cancelado]


def test_en_transito_necesita_awb(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)

    sin_awb = api.patch(
        f"{URL}/{embarque_id}", json={"estado": "en_transito"}, headers=como(tenant)
    )
    con_awb = api.patch(
        f"{URL}/{embarque_id}",
        json={"estado": "en_transito", "awb": OTRA_AWB},
        headers=como(tenant),
    )

    assert sin_awb.status_code == 422
    assert con_awb.status_code == 200, con_awb.json()
    assert (con_awb.json()["estado"], con_awb.json()["awb"]) == (
        "en_transito",
        OTRA_AWB,
    )


def test_patch_valida_como_el_alta(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)

    respuesta = api.patch(
        f"{URL}/{embarque_id}", json={"destino_pais": "AR"}, headers=como(tenant)
    )

    assert respuesta.status_code == 422


def test_patch_awb_repetida_es_409(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    crear(api, tenant, awb=AWB)
    otro = crear(api, tenant)

    respuesta = api.patch(f"{URL}/{otro}", json={"awb": AWB}, headers=como(tenant))

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "awb_duplicado"


def test_cancelado_no_se_edita_ni_recibe_documentos(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)
    api.patch(
        f"{URL}/{embarque_id}", json={"estado": "cancelado"}, headers=como(tenant)
    )

    editar = api.patch(
        f"{URL}/{embarque_id}", json={"fecha_vuelo": "2026-12-01"}, headers=como(tenant)
    )
    agregar = api.post(
        f"{URL}/{embarque_id}/documentos",
        json=documento(tenant, TipoDocumentoEmbarque.DTE),
        headers=como(tenant),
    )

    assert editar.status_code == agregar.status_code == 409
    assert codigo(editar) == codigo(agregar) == "embarque_cancelado"


# --- Roles e idempotencia ---


@pytest.mark.parametrize("rol", ["lectura", "produccion"])
def test_solo_admin_y_ventas_escriben(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID], rol: str
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)

    alta = api.post(URL, json=embarque(), headers=como(tenant, rol))
    edicion = api.patch(
        f"{URL}/{embarque_id}", json={"estado": "cancelado"}, headers=como(tenant, rol)
    )
    carga = api.post(
        f"{URL}/{embarque_id}/documentos",
        json=documento(tenant, TipoDocumentoEmbarque.DTE),
        headers=como(tenant, rol),
    )
    lectura = api.get(f"{URL}/{embarque_id}/completitud", headers=como(tenant, rol))

    assert [alta.status_code, edicion.status_code, carga.status_code] == [403] * 3
    assert lectura.status_code == 200


def test_admin_escribe(api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant, _ = criaderos

    respuesta = api.post(URL, json=embarque(), headers=como(tenant, "admin"))

    assert respuesta.status_code == 201


def test_la_misma_clave_carga_un_solo_documento(
    api: TestClient, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, _ = criaderos
    embarque_id = crear(api, tenant)
    headers = {**como(tenant), "Idempotency-Key": str(uuid.uuid4())}
    cuerpo = documento(tenant, TipoDocumentoEmbarque.LISTA_EMPAQUE)

    primera = api.post(f"{URL}/{embarque_id}/documentos", json=cuerpo, headers=headers)
    segunda = api.post(f"{URL}/{embarque_id}/documentos", json=cuerpo, headers=headers)

    assert primera.status_code == segunda.status_code == 201
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert primera.json() == segunda.json()


# --- Restricciones de la base (por si algo esquiva la API) ---


def _insertar(motor_owner: Engine, tenant: uuid.UUID, sql: str, **params: Any) -> None:
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(text(sql), params)


@pytest.mark.parametrize(
    ("sql", "restriccion"),
    [
        pytest.param(
            "INSERT INTO embarque (destino_pais, fecha_vuelo) "
            "VALUES ('AR', '2026-11-20')",
            "ck_embarque_destino_pais",
            id="destino_ar",
        ),
        pytest.param(
            "INSERT INTO embarque (destino_pais, fecha_vuelo, estado) "
            "VALUES ('NZ', '2026-11-20', 'en_transito')",
            "ck_embarque_awb_al_despachar",
            id="en_transito_sin_awb",
        ),
        pytest.param(
            "INSERT INTO embarque (destino_pais, fecha_vuelo, awb) "
            "VALUES ('NZ', '2026-11-20', '08112345675')",
            "ck_embarque_awb",
            id="awb_sin_normalizar",
        ),
    ],
)
def test_checks_del_embarque(
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    sql: str,
    restriccion: str,
) -> None:
    with pytest.raises(IntegrityError, match=restriccion):
        _insertar(motor_owner, criaderos[0], sql)


@pytest.mark.parametrize(
    ("archivo", "tipo", "restriccion"),
    [
        pytest.param(
            "documentos/{otro}/x.pdf",
            "dte",
            "ck_documento_embarque_archivo_del_tenant",
            id="archivo_de_otro_tenant",
        ),
        pytest.param(
            "documentos/{propio}/../{otro}/x.pdf",
            "dte",
            "ck_documento_embarque_archivo_del_tenant",
            id="sube_de_carpeta",
        ),
        pytest.param(
            "documentos/{propio}/x.pdf",
            "remito",
            "ck_documento_embarque_tipo",
            id="tipo_fuera_de_los_6",
        ),
    ],
)
def test_checks_del_documento(
    motor_owner: Engine,
    criaderos: tuple[uuid.UUID, uuid.UUID],
    archivo: str,
    tipo: str,
    restriccion: str,
) -> None:
    a, b = criaderos
    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        embarque_id = c.execute(
            text(
                "INSERT INTO embarque (destino_pais, fecha_vuelo) "
                "VALUES ('NZ', '2026-11-20') RETURNING id"
            )
        ).scalar_one()

    with pytest.raises(IntegrityError, match=restriccion):
        _insertar(
            motor_owner,
            a,
            "INSERT INTO documento_embarque (embarque_id, tipo, numero, "
            "archivo_s3_key, fecha) VALUES (:e, :tipo, 'N-1', :key, '2026-11-18')",
            e=embarque_id,
            tipo=tipo,
            key=archivo.format(propio=a, otro=b),
        )


def test_documento_no_puede_apuntar_a_un_embarque_de_otro_tenant(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = criaderos
    with motor_owner.begin() as c:
        fijar_tenant(c, b)
        de_b = c.execute(
            text(
                "INSERT INTO embarque (destino_pais, fecha_vuelo) "
                "VALUES ('NZ', '2026-11-20') RETURNING id"
            )
        ).scalar_one()

    with pytest.raises(IntegrityError, match="fk_documento_embarque_embarque"):
        _insertar(
            motor_owner,
            a,
            "INSERT INTO documento_embarque (embarque_id, tipo, numero, "
            "archivo_s3_key, fecha) VALUES (:e, 'dte', 'N-1', :key, '2026-11-18')",
            e=de_b,
            key=f"documentos/{a}/x.pdf",
        )
