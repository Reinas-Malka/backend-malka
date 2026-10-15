"""Endpoint de pedidos de punta a punta (#34, DoD).

Requests HTTP reales contra la app de produccion: token firmado, sesion con
el tenant del token, schemas y PostgreSQL con RLS (migracion 0008). Cubre
tambien el repositorio, que no tiene tests propios.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/pedidos"

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = ("pedido_item", "pedido", "idempotencia", "cliente", "item_vendible", "raza")


@dataclass(frozen=True)
class Datos:
    """Lo que cada criadero tiene cargado antes de los tests."""

    tenant: uuid.UUID
    nacional: uuid.UUID
    exportacion: uuid.UUID
    inactivo: uuid.UUID
    item: uuid.UUID
    raza: uuid.UUID
    raza_inactiva: uuid.UUID


def _id(c: Connection, sql: str) -> uuid.UUID:
    valor = c.execute(text(sql)).scalar_one()
    assert isinstance(valor, uuid.UUID)
    return valor


def _cargar(c: Connection, tenant: uuid.UUID) -> Datos:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    return Datos(
        tenant=tenant,
        nacional=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
            "VALUES ('Apicola del Sur', 'AR', 'nacional', 'monotributo', "
            "'20123456786') RETURNING id",
        ),
        exportacion=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
            "VALUES ('Bienenzucht GmbH', 'DE', 'exportacion', 'cliente_exterior', "
            "'DE123456789') RETURNING id",
        ),
        inactivo=_id(
            c,
            "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id, "
            "activo) VALUES ('Dado de baja', 'AR', 'nacional', 'monotributo', "
            "'30123456781', false) RETURNING id",
        ),
        item=_id(
            c,
            "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') "
            "RETURNING id",
        ),
        raza=_id(c, "INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id"),
        raza_inactiva=_id(
            c, "INSERT INTO raza (nombre, activo) VALUES ('Vieja', false) RETURNING id"
        ),
    )


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[uuid.UUID, Datos, Datos]]:
    """El criadero A con sus datos, y los datos de B para intentar alcanzarlos."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        datos_a = _cargar(c, a)
    with motor_owner.begin() as c:
        datos_b = _cargar(c, b)
    yield a, datos_a, datos_b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in TABLAS:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_pedidos(
    motor_owner: Engine, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> Iterator[None]:
    """Cada test arranca sin pedidos: los datos de uno no afectan a otro.

    Limpia los dos criaderos: los tests de lectura tambien crean pedidos en B.
    """
    yield
    for tenant in (criaderos[0], criaderos[2].tenant):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in ("pedido_item", "pedido", "idempotencia"):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def nacional(datos: Datos, **cambios: Any) -> dict[str, Any]:
    return {
        "cliente_id": str(datos.nacional),
        "moneda": "ARS",
        "fecha_entrega_solicitada": "2026-11-30",
        "items": [
            {
                "item_vendible_id": str(datos.item),
                "raza_id": str(datos.raza),
                "cantidad": 10,
                "precio_unitario": "35000.50",
            }
        ],
        **cambios,
    }


def exportacion(datos: Datos, **cambios: Any) -> dict[str, Any]:
    completo = {
        "cliente_id": str(datos.exportacion),
        "moneda": "EUR",
        "destino_pais": "DE",
        "incoterm": "CPT",
    }
    return nacional(datos, **{**completo, **cambios})


def cantidad_de_pedidos(motor_owner: Engine, tenant: uuid.UUID) -> int:
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        return int(c.execute(text("SELECT count(*) FROM pedido")).scalar_one())


# --- DoD ---


def test_crea_un_pedido_nacional(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=nacional(datos), headers=como(tenant))

    assert respuesta.status_code == 201, respuesta.json()
    pedido = respuesta.json()
    assert pedido["estado"] == "borrador"
    assert pedido["moneda"] == "ARS"
    assert pedido["destino_pais"] is None
    assert pedido["incoterm"] is None
    [item] = pedido["items"]
    assert item["cantidad"] == 10
    assert item["precio_unitario"] == "35000.50"
    # Copiada del item vendible, que tiene el default de la 0004.
    assert item["alicuota"] == "0.21"


def test_crea_un_pedido_de_exportacion_completo(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=exportacion(datos), headers=como(tenant))

    assert respuesta.status_code == 201, respuesta.json()
    pedido = respuesta.json()
    assert (pedido["moneda"], pedido["destino_pais"], pedido["incoterm"]) == (
        "EUR",
        "DE",
        "CPT",
    )


def test_exportacion_sin_destino_pais_es_422(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    cuerpo = exportacion(datos)
    del cuerpo["destino_pais"]

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "pedido_incoherente"
    assert cantidad_de_pedidos(motor_owner, tenant) == 0


# --- Reglas que necesitan la base ---


def test_exportacion_sin_incoterm_se_acepta(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """Envio consolidado a Europa: el incoterm queda vacio."""
    tenant, datos, _ = criaderos

    respuesta = api.post(
        URL, json=exportacion(datos, incoterm=None), headers=como(tenant)
    )

    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["incoterm"] is None


def test_nacional_en_euros_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=nacional(datos, moneda="EUR"), headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "pedido_incoherente"


def test_cliente_de_otro_criadero_responde_igual_que_uno_inexistente(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """Si las respuestas difirieran, A podria saber que ids existen en B."""
    tenant, datos, datos_b = criaderos

    de_b = api.post(
        URL,
        json=nacional(datos, cliente_id=str(datos_b.nacional)),
        headers=como(tenant),
    )
    inexistente = api.post(
        URL, json=nacional(datos, cliente_id=str(uuid.uuid4())), headers=como(tenant)
    )

    assert de_b.status_code == inexistente.status_code == 422
    assert codigo(de_b) == codigo(inexistente) == "cliente_inexistente"


def test_cliente_dado_de_baja_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(
        URL, json=nacional(datos, cliente_id=str(datos.inactivo)), headers=como(tenant)
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "cliente_inactivo"


@pytest.mark.parametrize(
    ("campo", "valor", "esperado"),
    [
        pytest.param("item_vendible_id", "inexistente", "item_vendible_inexistente"),
        pytest.param("item_vendible_id", "de_b", "item_vendible_inexistente"),
        pytest.param("raza_id", "inexistente", "raza_inexistente"),
        pytest.param("raza_id", "de_b", "raza_inexistente"),
        pytest.param("raza_id", "inactiva", "raza_inexistente"),
    ],
)
def test_item_o_raza_que_no_se_puede_usar_es_422(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
    campo: str,
    valor: str,
    esperado: str,
) -> None:
    tenant, datos, datos_b = criaderos
    ids = {
        "inexistente": uuid.uuid4(),
        "de_b": datos_b.item if campo == "item_vendible_id" else datos_b.raza,
        "inactiva": datos.raza_inactiva,
    }
    cuerpo = nacional(datos)
    # Una linea valida y una mala: no tiene que quedar ninguna de las dos.
    cuerpo["items"].append({**cuerpo["items"][0], campo: str(ids[valor])})

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == esperado
    assert respuesta.json()["error"]["details"]["ids"] == [str(ids[valor])]
    assert cantidad_de_pedidos(motor_owner, tenant) == 0


def test_la_alicuota_se_copia_y_no_cambia_con_el_catalogo(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    creado = api.post(URL, json=nacional(datos), headers=como(tenant)).json()

    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text("UPDATE item_vendible SET alicuota = 0.105 WHERE id = :id"),
            {"id": datos.item},
        )
        guardada = c.execute(
            text("SELECT alicuota FROM pedido_item WHERE id = :id"),
            {"id": creado["items"][0]["id"]},
        ).scalar_one()
        c.execute(
            text("UPDATE item_vendible SET alicuota = 0.21 WHERE id = :id"),
            {"id": datos.item},
        )

    assert str(guardada) == "0.210"


def test_un_pedido_con_varias_lineas(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    cuerpo = nacional(datos)
    cuerpo["items"].append({**cuerpo["items"][0], "cantidad": 5})

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 201, respuesta.json()
    assert [item["cantidad"] for item in respuesta.json()["items"]] == [10, 5]


# --- Permisos, formato e idempotencia ---


def test_estado_en_el_cuerpo_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(
        URL, json=nacional(datos, estado="entregado"), headers=como(tenant)
    )

    assert respuesta.status_code == 422


def test_sin_token_es_401(api: TestClient) -> None:
    assert api.post(URL, json={}).status_code == 401


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_solo_admin_y_ventas_crean(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos], rol: str
) -> None:
    tenant, datos, _ = criaderos

    respuesta = api.post(URL, json=nacional(datos), headers=como(tenant, rol))

    assert respuesta.status_code == 403


def test_la_misma_clave_crea_un_solo_pedido(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    headers = {**como(tenant), "Idempotency-Key": str(uuid.uuid4())}

    primera = api.post(URL, json=nacional(datos), headers=headers)
    segunda = api.post(URL, json=nacional(datos), headers=headers)

    assert primera.status_code == segunda.status_code == 201
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert primera.json() == segunda.json()
    assert cantidad_de_pedidos(motor_owner, tenant) == 1


def test_la_misma_clave_con_otro_pedido_es_409(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    headers = {**como(tenant), "Idempotency-Key": str(uuid.uuid4())}

    primera = api.post(URL, json=nacional(datos), headers=headers)
    otra = api.post(
        URL,
        json=nacional(datos, fecha_entrega_solicitada="2026-12-15"),
        headers=headers,
    )

    assert primera.status_code == 201
    assert otra.status_code == 409
    assert codigo(otra) == "idempotency_key_reutilizada"
    assert cantidad_de_pedidos(motor_owner, tenant) == 1


def test_cantidad_que_no_entra_en_la_columna_es_422_y_no_500(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    cuerpo = nacional(datos)
    cuerpo["items"][0]["cantidad"] = 3_000_000_000

    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"
    assert cantidad_de_pedidos(motor_owner, tenant) == 0


# --- Lectura: GET /api/v1/pedidos y GET /api/v1/pedidos/{id} ---


def crear(api: TestClient, tenant: uuid.UUID, cuerpo: dict[str, Any]) -> dict[str, Any]:
    respuesta = api.post(URL, json=cuerpo, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    creado: dict[str, Any] = respuesta.json()
    return creado


def test_obtener_devuelve_lo_mismo_que_el_alta(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    creado = crear(api, tenant, exportacion(datos))

    respuesta = api.get(f"{URL}/{creado['id']}", headers=como(tenant))

    assert respuesta.status_code == 200
    assert respuesta.json() == creado


def test_las_lineas_vuelven_en_el_orden_en_que_se_cargaron(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """creado_en es igual en todas las lineas: el orden lo da numero_linea."""
    tenant, datos, _ = criaderos
    cuerpo = nacional(datos)
    linea = cuerpo["items"][0]
    cuerpo["items"] = [{**linea, "cantidad": cantidad} for cantidad in (3, 1, 7, 2)]
    creado = crear(api, tenant, cuerpo)

    leido = api.get(f"{URL}/{creado['id']}", headers=como(tenant)).json()

    assert [i["cantidad"] for i in leido["items"]] == [3, 1, 7, 2]
    assert [i["numero_linea"] for i in leido["items"]] == [1, 2, 3, 4]


def test_pedido_de_otro_criadero_responde_igual_que_uno_inexistente(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """404 y no 403: decir "no tenes permiso" confirmaria que existe."""
    tenant, _, datos_b = criaderos
    de_b = crear(api, datos_b.tenant, nacional(datos_b))

    ajeno = api.get(f"{URL}/{de_b['id']}", headers=como(tenant))
    inexistente = api.get(f"{URL}/{uuid.uuid4()}", headers=como(tenant))

    assert ajeno.status_code == inexistente.status_code == 404
    assert codigo(ajeno) == codigo(inexistente) == "pedido_no_encontrado"


def test_id_que_no_es_uuid_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, _, _ = criaderos

    assert api.get(f"{URL}/abc", headers=como(tenant)).status_code == 422


def test_lista_vacia(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, _, _ = criaderos

    respuesta = api.get(URL, headers=como(tenant))

    assert respuesta.status_code == 200
    assert respuesta.json() == {"items": [], "siguiente": None}


def test_lista_solo_los_del_criadero_del_mas_nuevo_al_mas_viejo(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, datos_b = criaderos
    viejo = crear(api, tenant, nacional(datos))
    nuevo = crear(api, tenant, exportacion(datos))
    crear(api, datos_b.tenant, nacional(datos_b))

    lista = api.get(URL, headers=como(tenant)).json()["items"]

    assert [p["id"] for p in lista] == [nuevo["id"], viejo["id"]]


def test_lista_cada_pedido_con_sus_lineas(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    con_dos = nacional(datos)
    con_dos["items"].append({**con_dos["items"][0], "cantidad": 5})
    a = crear(api, tenant, con_dos)
    b = crear(api, tenant, exportacion(datos))

    lista = api.get(URL, headers=como(tenant)).json()["items"]

    por_id = {p["id"]: p for p in lista}
    assert por_id[a["id"]]["items"] == a["items"]
    assert por_id[b["id"]]["items"] == b["items"]


def test_lista_filtra_por_estado(
    api: TestClient,
    criaderos: tuple[uuid.UUID, Datos, Datos],
    motor_owner: Engine,
) -> None:
    tenant, datos, _ = criaderos
    borrador = crear(api, tenant, nacional(datos))
    reservado = crear(api, tenant, nacional(datos))
    # Hasta #36 no hay transiciones: se cambia el estado directo en la base.
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text("UPDATE pedido SET estado = 'reservado' WHERE id = :id"),
            {"id": reservado["id"]},
        )

    def ids(estado: str) -> list[str]:
        lista = api.get(URL, params={"estado": estado}, headers=como(tenant)).json()
        return [p["id"] for p in lista["items"]]

    assert ids("borrador") == [borrador["id"]]
    assert ids("reservado") == [reservado["id"]]
    assert ids("entregado") == []


def test_lista_filtra_por_cliente(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    crear(api, tenant, nacional(datos))
    del_exterior = crear(api, tenant, exportacion(datos))

    lista = api.get(
        URL, params={"cliente_id": str(datos.exportacion)}, headers=como(tenant)
    ).json()["items"]

    assert [p["id"] for p in lista] == [del_exterior["id"]]


def test_lista_paginada_con_cursor(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    creados = [crear(api, tenant, nacional(datos))["id"] for _ in range(3)]

    def pagina(**params: Any) -> dict[str, Any]:
        respuesta = api.get(URL, params={"limite": 2, **params}, headers=como(tenant))
        assert respuesta.status_code == 200
        return dict(respuesta.json())

    # Del mas nuevo al mas viejo: la primera pagina trae los dos ultimos.
    primera = pagina()
    assert [p["id"] for p in primera["items"]] == [creados[2], creados[1]]
    assert primera["siguiente"] is not None

    # Un alta entre dos paginas no corre a los demas: con desplazamiento, la
    # segunda pagina repetiria creados[1].
    crear(api, tenant, nacional(datos))
    segunda = pagina(cursor=primera["siguiente"])
    assert [p["id"] for p in segunda["items"]] == [creados[0]]
    assert segunda["siguiente"] is None


def test_el_cursor_respeta_los_filtros(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    tenant, datos, _ = criaderos
    del_exterior = [crear(api, tenant, exportacion(datos))["id"] for _ in range(2)]
    crear(api, tenant, nacional(datos))
    params = {"cliente_id": str(datos.exportacion), "limite": 1}

    primera = api.get(URL, params=params, headers=como(tenant)).json()
    segunda = api.get(
        URL, params={**params, "cursor": primera["siguiente"]}, headers=como(tenant)
    ).json()

    assert [p["id"] for p in primera["items"] + segunda["items"]] == [
        del_exterior[1],
        del_exterior[0],
    ]
    assert segunda["siguiente"] is None


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"estado": "enviado"}, id="estado_fuera_de_la_lista"),
        pytest.param({"cliente_id": "abc"}, id="cliente_no_es_uuid"),
        pytest.param({"limite": 0}, id="limite_cero"),
        pytest.param({"limite": 101}, id="limite_mayor_al_tope"),
    ],
)
def test_filtro_invalido_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos], params: dict[str, Any]
) -> None:
    tenant, _, _ = criaderos

    respuesta = api.get(URL, params=params, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"


def test_cursor_invalido_es_422(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos]
) -> None:
    """Un cursor roto o armado a mano no llega a la base."""
    tenant, _, _ = criaderos

    respuesta = api.get(URL, params={"cursor": "no-es-un-cursor"}, headers=como(tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "cursor_invalido"


@pytest.mark.parametrize("rol", ["admin", "ventas", "produccion", "lectura"])
def test_todos_los_roles_leen(
    api: TestClient, criaderos: tuple[uuid.UUID, Datos, Datos], rol: str
) -> None:
    tenant, datos, _ = criaderos
    creado = crear(api, tenant, nacional(datos))

    lista = api.get(URL, headers=como(tenant, rol))
    uno = api.get(f"{URL}/{creado['id']}", headers=como(tenant, rol))

    assert lista.status_code == uno.status_code == 200


def test_leer_sin_token_es_401(api: TestClient) -> None:
    assert api.get(URL).status_code == 401
    assert api.get(f"{URL}/{uuid.uuid4()}").status_code == 401
