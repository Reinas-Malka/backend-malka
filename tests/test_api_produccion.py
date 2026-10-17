"""Endpoints de datos maestros de produccion de punta a punta (#30, #29).

Requests HTTP reales contra la app de produccion: token firmado (#21), sesion
con el tenant del token (#19), validacion (schemas), idempotencia (#25) y
PostgreSQL con RLS. Los tests comunes (alta, roles, 404 entre criaderos, baja
logica, paginacion, idempotencia) corren para razas, madres, parques,
nucleos, materiales e items vendibles; despues vienen las reglas de cada uno:
capacidad del parque (#29), stock y movimientos de material, y alicuotas.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app
from app.services.documentos.importes import Alicuota
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

RAZAS = "/api/v1/razas"
MADRES = "/api/v1/madres"
PARQUES = "/api/v1/parques"
NUCLEOS = "/api/v1/nucleos"
MATERIALES = "/api/v1/materiales"
ITEMS = "/api/v1/items-vendibles"
BANCOS = "/api/v1/bancos"

# De hijas a padres, para poder borrar al limpiar.
TABLAS = (
    "idempotencia",
    "movimiento_material",
    "material",
    "item_vendible",
    "celda",
    "nucleo",
    "parque",
    "banco",
    "tanda",
    "madre",
    "raza",
)

Criaderos = tuple[uuid.UUID, uuid.UUID]


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[Criaderos]:
    """Dos tenants vacios; cada test carga lo que necesita."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        for tenant in (a, b):
            crear_tenant(c, tenant)
    yield a, b
    with motor_owner.begin() as c:
        for tenant in (a, b):
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_datos(motor_owner: Engine, criaderos: Criaderos) -> Iterator[None]:
    """Cada test arranca vacio: los datos de uno no afectan a otro."""
    yield
    for tenant in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in TABLAS:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "produccion") -> dict[str, str]:
    """Headers de un usuario del criadero con ese rol."""
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def crear(
    api: TestClient, url: str, tenant: uuid.UUID, datos: dict[str, Any]
) -> dict[str, Any]:
    respuesta = api.post(url, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return sin_avisos(respuesta.json())


def sin_avisos(cuerpo: dict[str, Any]) -> dict[str, Any]:
    """El alta y la edicion de nucleos suman `advertencias` (#29); el GET no."""
    return {campo: valor for campo, valor in cuerpo.items() if campo != "advertencias"}


def crear_parque(
    api: TestClient,
    tenant: uuid.UUID,
    nombre: str = "Parque norte",
    capacidad: int = 10,
) -> dict[str, Any]:
    datos = {"nombre": nombre, "ubicacion": "Predio norte", "capacidad_max": capacidad}
    return crear(api, PARQUES, tenant, datos)


# --- Tests comunes a los cuatro recursos ---


def _parque_de_nucleos(api: TestClient, tenant: uuid.UUID) -> str:
    """Un parque grande donde colgar los nucleos de los tests comunes."""
    nombre = "Parque de prueba"
    datos = {"nombre": nombre, "ubicacion": "Predio sur", "capacidad_max": 100}
    respuesta = api.post(PARQUES, json=datos, headers=como(tenant))
    if respuesta.status_code == 201:
        return str(respuesta.json()["id"])
    pagina = api.get(PARQUES, headers=como(tenant)).json()
    return next(str(p["id"]) for p in pagina["items"] if p["nombre"] == nombre)


@dataclass(frozen=True)
class Recurso:
    url: str
    # Cuerpo valido del alta numero n; el mismo n da el mismo cuerpo.
    cuerpo: Callable[[TestClient, uuid.UUID, int], dict[str, Any]]
    # Un PATCH valido y un alta invalida (cambios sobre el cuerpo 1).
    cambio: dict[str, Any]
    invalido: dict[str, Any]
    duplicado: str
    no_encontrado: str


RECURSOS = [
    Recurso(
        url=RAZAS,
        cuerpo=lambda api, t, n: {"nombre": f"Raza {n:02d}", "descripcion": "Mansa"},
        cambio={"nombre": "Carniola"},
        invalido={"nombre": ""},
        duplicado="raza_duplicada",
        no_encontrado="raza_no_encontrada",
    ),
    Recurso(
        url=MADRES,
        cuerpo=lambda api, t, n: {"identificacion": f"M-{n:02d}"},
        cambio={"identificacion": "M-99"},
        invalido={"identificacion": "   "},
        duplicado="madre_duplicada",
        no_encontrado="madre_no_encontrada",
    ),
    Recurso(
        url=PARQUES,
        cuerpo=lambda api, t, n: {
            "nombre": f"Parque {n:02d}",
            "ubicacion": "Predio norte",
            "capacidad_max": 5,
        },
        cambio={"ubicacion": "Predio sur"},
        invalido={"capacidad_max": 0},
        duplicado="parque_duplicado",
        no_encontrado="parque_no_encontrado",
    ),
    Recurso(
        url=NUCLEOS,
        cuerpo=lambda api, t, n: {
            "parque_id": _parque_de_nucleos(api, t),
            "fila": 1,
            "posicion": n,
        },
        cambio={"fila": 2},
        invalido={"posicion": 0},
        duplicado="posicion_ocupada",
        no_encontrado="nucleo_no_encontrado",
    ),
    Recurso(
        url=MATERIALES,
        cuerpo=lambda api, t, n: {"nombre": f"Cupula {n:02d}", "unidad": "unidad"},
        cambio={"unidad": "caja"},
        invalido={"unidad": "x" * 21},
        duplicado="material_duplicado",
        no_encontrado="material_no_encontrado",
    ),
    Recurso(
        url=ITEMS,
        cuerpo=lambda api, t, n: {"nombre": f"Reina fecundada {n:02d}"},
        cambio={"alicuota": "0.105"},
        invalido={"alicuota": "0.19"},
        duplicado="item_vendible_duplicado",
        no_encontrado="item_vendible_no_encontrado",
    ),
    Recurso(
        url=BANCOS,
        cuerpo=lambda api, t, n: {"identificador": f"B-{n:02d}"},
        cambio={"capacidad_max": 300},
        invalido={"capacidad_max": 0},
        duplicado="banco_duplicado",
        no_encontrado="banco_no_encontrado",
    ),
]

por_recurso = pytest.mark.parametrize(
    "recurso", RECURSOS, ids=[r.url.rsplit("/", 1)[-1] for r in RECURSOS]
)


@por_recurso
@pytest.mark.parametrize("rol", ["admin", "produccion"])
def test_alta(
    api: TestClient, criaderos: Criaderos, recurso: Recurso, rol: str
) -> None:
    a, _ = criaderos
    respuesta = api.post(
        recurso.url, json=recurso.cuerpo(api, a, 1), headers=como(a, rol)
    )
    assert respuesta.status_code == 201, respuesta.json()
    cuerpo = respuesta.json()
    assert uuid.UUID(cuerpo["id"])
    assert cuerpo["activo"] is True
    assert "tenant_id" not in cuerpo


@por_recurso
@pytest.mark.parametrize("rol", ["ventas", "lectura"])
def test_escribir_sin_rol_es_403(
    api: TestClient, criaderos: Criaderos, recurso: Recurso, rol: str
) -> None:
    """DoD de la #30: rol insuficiente. Escriben solo admin y produccion."""
    a, _ = criaderos
    existente = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    url = f"{recurso.url}/{existente['id']}"
    headers = como(a, rol)

    respuestas = [
        api.post(recurso.url, json=recurso.cuerpo(api, a, 2), headers=headers),
        api.patch(url, json=recurso.cambio, headers=headers),
        api.delete(url, headers=headers),
    ]
    assert [r.status_code for r in respuestas] == [403, 403, 403]
    assert {codigo(r) for r in respuestas} == {"sin_permiso"}


@por_recurso
@pytest.mark.parametrize("rol", ["admin", "produccion", "ventas", "lectura"])
def test_leer_puede_cualquier_rol(
    api: TestClient, criaderos: Criaderos, recurso: Recurso, rol: str
) -> None:
    a, _ = criaderos
    existente = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    lista = api.get(recurso.url, headers=como(a, rol))
    uno = api.get(f"{recurso.url}/{existente['id']}", headers=como(a, rol))
    assert lista.status_code == 200
    assert existente["id"] in [item["id"] for item in lista.json()["items"]]
    assert uno.json() == existente


@por_recurso
def test_sin_token_es_401(api: TestClient, recurso: Recurso) -> None:
    respuesta = api.get(recurso.url)
    assert respuesta.status_code == 401
    assert codigo(respuesta) == "token_ausente"


@por_recurso
def test_duplicado_es_409(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    respuesta = api.post(recurso.url, json=recurso.cuerpo(api, a, 1), headers=como(a))
    assert respuesta.status_code == 409
    assert codigo(respuesta) == recurso.duplicado


@por_recurso
def test_mismo_dato_en_otro_criadero(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    """Los UNIQUE son por criadero: B puede tener lo mismo que A."""
    a, b = criaderos
    crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    crear(api, recurso.url, b, recurso.cuerpo(api, b, 1))


@por_recurso
def test_alta_invalida_es_422(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    datos = {**recurso.cuerpo(api, a, 1), **recurso.invalido}
    respuesta = api.post(recurso.url, json=datos, headers=como(a))
    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"


@por_recurso
def test_tenant_en_el_body_es_422(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    """Asignacion masiva: el tenant sale del token, nunca del body."""
    a, b = criaderos
    datos = {**recurso.cuerpo(api, a, 1), "tenant_id": str(b)}
    respuesta = api.post(recurso.url, json=datos, headers=como(a))
    assert respuesta.status_code == 422


@por_recurso
def test_lo_de_otro_criadero_es_404(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    """Fuga entre tenants (AGENTS.md, #22): con datos en los dos criaderos, un
    token de B no lee, no modifica y no borra lo de A, y cada uno lista solo
    lo suyo."""
    a, b = criaderos
    de_a = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    de_b = crear(api, recurso.url, b, recurso.cuerpo(api, b, 2))
    url = f"{recurso.url}/{de_a['id']}"

    respuestas = [
        api.get(url, headers=como(b)),
        api.patch(url, json=recurso.cambio, headers=como(b)),
        api.delete(url, headers=como(b)),
    ]
    assert [r.status_code for r in respuestas] == [404, 404, 404]
    assert {codigo(r) for r in respuestas} == {recurso.no_encontrado}

    for tenant, propio, ajeno in ((a, de_a, de_b), (b, de_b, de_a)):
        pagina = api.get(
            recurso.url, params={"incluir_inactivos": True}, headers=como(tenant)
        ).json()
        ids = [item["id"] for item in pagina["items"]]
        assert propio["id"] in ids
        assert ajeno["id"] not in ids
    # Y lo de A sigue intacto: ni el PATCH ni el DELETE de B lo tocaron.
    assert api.get(url, headers=como(a)).json() == de_a


@por_recurso
def test_404_no_distingue_inexistente_de_otro_criadero(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    """#22: el 404 de algo de otro criadero es igual al de algo que no existe,
    asi no se puede averiguar que ids existen en otro criadero."""
    a, b = criaderos
    de_a = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))

    def error(id_: str, metodo: str) -> dict[str, Any]:
        url = f"{recurso.url}/{id_}"
        if metodo == "patch":
            respuesta = api.patch(url, json=recurso.cambio, headers=como(b))
        else:
            respuesta = getattr(api, metodo)(url, headers=como(b))
        assert respuesta.status_code == 404
        cuerpo: dict[str, Any] = respuesta.json()["error"]
        cuerpo.pop("request_id")
        return cuerpo

    for metodo in ("get", "patch", "delete"):
        assert error(de_a["id"], metodo) == error(str(uuid.uuid4()), metodo)


@por_recurso
def test_id_inexistente_o_malformado(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    inexistente = api.get(f"{recurso.url}/{uuid.uuid4()}", headers=como(a))
    malformado = api.get(f"{recurso.url}/no-es-un-uuid", headers=como(a))
    assert inexistente.status_code == 404
    assert codigo(inexistente) == recurso.no_encontrado
    assert malformado.status_code == 422


@por_recurso
def test_patch(api: TestClient, criaderos: Criaderos, recurso: Recurso) -> None:
    a, _ = criaderos
    existente = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    respuesta = api.patch(
        f"{recurso.url}/{existente['id']}", json=recurso.cambio, headers=como(a)
    )
    assert respuesta.status_code == 200, respuesta.json()
    assert sin_avisos(respuesta.json()) == {**existente, **recurso.cambio}


@por_recurso
def test_patch_invalido_es_422(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    existente = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    url = f"{recurso.url}/{existente['id']}"
    respuesta = api.patch(url, json=recurso.invalido, headers=como(a))
    assert respuesta.status_code == 422
    assert api.get(url, headers=como(a)).json() == existente


@por_recurso
def test_patch_no_puede_pisar_a_otro(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    """Editar el 2 para que quede igual al 1 choca con el UNIQUE: 409."""
    a, _ = criaderos
    primero = recurso.cuerpo(api, a, 1)
    crear(api, recurso.url, a, primero)
    segundo = crear(api, recurso.url, a, recurso.cuerpo(api, a, 2))
    cambios = {
        campo: valor
        for campo, valor in primero.items()
        if campo not in {"parque_id", "descripcion", "ubicacion", "capacidad_max"}
    }
    respuesta = api.patch(
        f"{recurso.url}/{segundo['id']}", json=cambios, headers=como(a)
    )
    assert respuesta.status_code == 409
    assert codigo(respuesta) == recurso.duplicado


@por_recurso
def test_baja_logica_y_reactivacion(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    existente = crear(api, recurso.url, a, recurso.cuerpo(api, a, 1))
    url = f"{recurso.url}/{existente['id']}"

    assert api.delete(url, headers=como(a)).status_code == 204

    def ids(**params: Any) -> list[str]:
        pagina = api.get(recurso.url, params=params, headers=como(a)).json()
        return [item["id"] for item in pagina["items"]]

    # Sale de la lista, pero sigue existiendo.
    assert existente["id"] not in ids()
    assert existente["id"] in ids(incluir_inactivos=True)
    assert api.get(url, headers=como(a)).json()["activo"] is False

    reactivado = api.patch(url, json={"activo": True}, headers=como(a))
    assert reactivado.status_code == 200
    assert sin_avisos(reactivado.json()) == existente
    assert existente["id"] in ids()


@por_recurso
def test_paginacion_recorre_todo_sin_repetir(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    creados = {
        crear(api, recurso.url, a, recurso.cuerpo(api, a, n))["id"]
        for n in (1, 2, 3, 4, 5)
    }

    vistos: list[str] = []
    params: dict[str, Any] = {"limite": 2}
    paginas = 0
    while True:
        respuesta = api.get(recurso.url, params=params, headers=como(a))
        assert respuesta.status_code == 200, respuesta.json()
        pagina = respuesta.json()
        assert len(pagina["items"]) <= 2
        vistos += [item["id"] for item in pagina["items"]]
        paginas += 1
        if pagina["siguiente"] is None:
            break
        params["cursor"] = pagina["siguiente"]

    assert paginas == 3
    assert set(vistos) == creados
    assert len(vistos) == len(creados)

    todo = api.get(recurso.url, params={"limite": 100}, headers=como(a)).json()
    assert [item["id"] for item in todo["items"]] == vistos
    assert todo["siguiente"] is None


@por_recurso
@pytest.mark.parametrize(
    "params",
    [{"limite": 101}, {"limite": 0}, {"limite": "muchos"}],
    ids=["mas_de_100", "cero", "texto"],
)
def test_limite_fuera_de_rango_es_422(
    api: TestClient, criaderos: Criaderos, recurso: Recurso, params: dict[str, Any]
) -> None:
    a, _ = criaderos
    respuesta = api.get(recurso.url, params=params, headers=como(a))
    assert respuesta.status_code == 422


@por_recurso
def test_cursor_invalido_es_422(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    respuesta = api.get(recurso.url, params={"cursor": "basura"}, headers=como(a))
    assert respuesta.status_code == 422
    assert codigo(respuesta) == "cursor_invalido"


@pytest.mark.parametrize(
    ("url", "campo"),
    [
        (RAZAS, "nombre"),
        (MADRES, "identificacion"),
        (PARQUES, "ubicacion"),
        (MATERIALES, "nombre"),
        (ITEMS, "nombre"),
    ],
    ids=["razas", "madres", "parques", "materiales", "items"],
)
def test_inyeccion_sql_se_guarda_como_texto(
    api: TestClient, criaderos: Criaderos, url: str, campo: str
) -> None:
    """Los valores viajan como parametros: el texto se guarda tal cual."""
    a, _ = criaderos
    recurso = next(r for r in RECURSOS if r.url == url)
    malicioso = "x'); DROP TABLE raza; --"
    creado = crear(api, url, a, {**recurso.cuerpo(api, a, 1), campo: malicioso})
    assert creado[campo] == malicioso
    assert api.get(RAZAS, headers=como(a)).status_code == 200


# --- Idempotencia (#25), en todos los POST ---


@por_recurso
def test_post_con_la_misma_clave_crea_uno_solo(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    datos = recurso.cuerpo(api, a, 1)
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}

    primero = api.post(recurso.url, json=datos, headers=headers)
    repetido = api.post(recurso.url, json=datos, headers=headers)

    assert primero.status_code == repetido.status_code == 201
    assert repetido.json() == primero.json()
    assert repetido.headers["Idempotent-Replayed"] == "true"
    pagina = api.get(recurso.url, params={"limite": 100}, headers=como(a)).json()
    assert [i["id"] for i in pagina["items"]].count(primero.json()["id"]) == 1


@por_recurso
def test_la_misma_clave_con_otro_cuerpo_es_409(
    api: TestClient, criaderos: Criaderos, recurso: Recurso
) -> None:
    a, _ = criaderos
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}
    api.post(recurso.url, json=recurso.cuerpo(api, a, 1), headers=headers)
    otro = api.post(recurso.url, json=recurso.cuerpo(api, a, 2), headers=headers)
    assert otro.status_code == 409
    assert codigo(otro) == "idempotency_key_reutilizada"


# --- Razas ---


def test_raza_descripcion_opcional_y_borrable(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    sin = crear(api, RAZAS, a, {"nombre": "Italiana"})
    assert sin["descripcion"] is None

    con = crear(api, RAZAS, a, {"nombre": "Buckfast", "descripcion": "Mansa"})
    url = f"{RAZAS}/{con['id']}"
    borrada = api.patch(url, json={"descripcion": None}, headers=como(a))
    assert borrada.json()["descripcion"] is None


def test_raza_nombre_null_en_patch_es_422(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    raza = crear(api, RAZAS, a, {"nombre": "Italiana"})
    respuesta = api.patch(
        f"{RAZAS}/{raza['id']}", json={"nombre": None}, headers=como(a)
    )
    assert respuesta.status_code == 422


@pytest.mark.parametrize(
    "nombre", ["Ita\x00liana", "Ita\nliana"], ids=["nul", "salto_de_linea"]
)
def test_caracteres_de_control_son_422(
    api: TestClient, criaderos: Criaderos, nombre: str
) -> None:
    a, _ = criaderos
    respuesta = api.post(RAZAS, json={"nombre": nombre}, headers=como(a))
    assert respuesta.status_code == 422


def test_listado_ordenado_por_nombre(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    for nombre in ("Carniola", "Buckfast", "Italiana"):
        crear(api, RAZAS, a, {"nombre": nombre})
    pagina = api.get(RAZAS, headers=como(a)).json()
    assert [r["nombre"] for r in pagina["items"]] == [
        "Buckfast",
        "Carniola",
        "Italiana",
    ]


# --- Parques y nucleos: reglas fisicas ---


def nucleo(parque: dict[str, Any], fila: int, posicion: int) -> dict[str, Any]:
    return {"parque_id": parque["id"], "fila": fila, "posicion": posicion}


def test_parque_cuenta_sus_nucleos_activos(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)
    assert parque["nucleos_activos"] == 0
    crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    baja = crear(api, NUCLEOS, a, nucleo(parque, 1, 2))
    api.delete(f"{NUCLEOS}/{baja['id']}", headers=como(a))

    actual = api.get(f"{PARQUES}/{parque['id']}", headers=como(a)).json()
    assert actual["nucleos_activos"] == 1


def test_parque_lleno_es_409(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=2)
    crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    crear(api, NUCLEOS, a, nucleo(parque, 1, 2))

    respuesta = api.post(NUCLEOS, json=nucleo(parque, 1, 3), headers=como(a))
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_lleno"
    assert respuesta.json()["error"]["details"] == {"capacidad_max": 2}


def test_dar_de_baja_un_nucleo_libera_lugar(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=1)
    primero = crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    api.delete(f"{NUCLEOS}/{primero['id']}", headers=como(a))

    crear(api, NUCLEOS, a, nucleo(parque, 1, 2))

    # Y ahora el primero no se puede reactivar: el parque esta lleno otra vez.
    respuesta = api.patch(
        f"{NUCLEOS}/{primero['id']}", json={"activo": True}, headers=como(a)
    )
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_lleno"


def test_posicion_ocupada_por_un_nucleo_dado_de_baja(
    api: TestClient, criaderos: Criaderos
) -> None:
    """La posicion se recupera reactivando el nucleo, no creando otro."""
    a, _ = criaderos
    parque = crear_parque(api, a)
    viejo = crear(api, NUCLEOS, a, nucleo(parque, 2, 3))
    api.delete(f"{NUCLEOS}/{viejo['id']}", headers=como(a))

    respuesta = api.post(NUCLEOS, json=nucleo(parque, 2, 3), headers=como(a))
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "posicion_ocupada"


def test_nucleo_en_parque_de_otro_criadero_es_404(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, b = criaderos
    parque_de_a = crear_parque(api, a)
    respuesta = api.post(NUCLEOS, json=nucleo(parque_de_a, 1, 1), headers=como(b))
    assert respuesta.status_code == 404
    assert codigo(respuesta) == "parque_no_encontrado"


def test_nucleo_en_parque_inexistente_es_404(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    datos = {"parque_id": str(uuid.uuid4()), "fila": 1, "posicion": 1}
    respuesta = api.post(NUCLEOS, json=datos, headers=como(a))
    assert respuesta.status_code == 404
    assert codigo(respuesta) == "parque_no_encontrado"


def test_nucleo_en_parque_dado_de_baja_es_409(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)
    api.delete(f"{PARQUES}/{parque['id']}", headers=como(a))

    respuesta = api.post(NUCLEOS, json=nucleo(parque, 1, 1), headers=como(a))
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_inactivo"


@pytest.mark.parametrize(
    ("metodo", "cuerpo"),
    [("delete", None), ("patch", {"activo": False})],
    ids=["delete", "patch"],
)
def test_parque_con_nucleos_no_se_da_de_baja(
    api: TestClient, criaderos: Criaderos, metodo: str, cuerpo: dict[str, Any] | None
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)
    crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    url = f"{PARQUES}/{parque['id']}"

    if cuerpo is None:
        respuesta = api.delete(url, headers=como(a))
    else:
        respuesta = api.patch(url, json=cuerpo, headers=como(a))
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_con_nucleos"
    assert api.get(url, headers=como(a)).json()["activo"] is True


def test_capacidad_menor_que_los_nucleos_es_409(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=3)
    crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    crear(api, NUCLEOS, a, nucleo(parque, 1, 2))
    url = f"{PARQUES}/{parque['id']}"

    menor = api.patch(url, json={"capacidad_max": 1}, headers=como(a))
    assert menor.status_code == 409
    assert codigo(menor) == "capacidad_insuficiente"

    justa = api.patch(url, json={"capacidad_max": 2}, headers=como(a))
    assert justa.status_code == 200
    assert justa.json()["capacidad_max"] == 2


def test_el_parque_de_un_nucleo_no_se_cambia(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)
    otro = crear_parque(api, a, nombre="Parque sur")
    creado = crear(api, NUCLEOS, a, nucleo(parque, 1, 1))

    respuesta = api.patch(
        f"{NUCLEOS}/{creado['id']}", json={"parque_id": otro["id"]}, headers=como(a)
    )
    assert respuesta.status_code == 422


def test_fila_como_texto_es_422(api: TestClient, criaderos: Criaderos) -> None:
    """strict: "3" no se convierte en 3 en silencio."""
    a, _ = criaderos
    parque = crear_parque(api, a)
    datos = {"parque_id": parque["id"], "fila": "3", "posicion": 1}
    respuesta = api.post(NUCLEOS, json=datos, headers=como(a))
    assert respuesta.status_code == 422


def test_nucleos_filtrados_por_parque(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    norte = crear_parque(api, a)
    sur = crear_parque(api, a, nombre="Parque sur")
    del_norte = crear(api, NUCLEOS, a, nucleo(norte, 1, 1))
    crear(api, NUCLEOS, a, nucleo(sur, 1, 1))

    pagina = api.get(NUCLEOS, params={"parque_id": norte["id"]}, headers=como(a))
    assert [n["id"] for n in pagina.json()["items"]] == [del_norte["id"]]


def test_nucleos_ordenados_por_fila_y_posicion(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a)
    for fila, posicion in ((2, 1), (1, 2), (1, 1)):
        crear(api, NUCLEOS, a, nucleo(parque, fila, posicion))
    pagina = api.get(NUCLEOS, params={"parque_id": parque["id"]}, headers=como(a))
    ubicaciones = [(n["fila"], n["posicion"]) for n in pagina.json()["items"]]
    assert ubicaciones == [(1, 1), (1, 2), (2, 1)]


# --- OpenAPI ---


def test_openapi_documenta_los_endpoints(api: TestClient) -> None:
    """DoD de la #30: el OpenAPI incluye las rutas nuevas."""
    rutas = api.get("/openapi.json").json()["paths"]
    for url in (RAZAS, MADRES, PARQUES, NUCLEOS, MATERIALES, ITEMS):
        assert set(rutas[url]) == {"get", "post"}
        id_ = next(
            ruta
            for ruta in rutas
            if ruta.startswith(f"{url}/{{") and ruta.count("/") == url.count("/") + 1
        )
        assert set(rutas[id_]) == {"get", "patch", "delete"}
    assert set(rutas[f"{MATERIALES}/{{material_id}}/movimientos"]) == {"get", "post"}
    # Los POST documentan el header de idempotencia (#25).
    parametros = rutas[RAZAS]["post"]["parameters"]
    assert "Idempotency-Key" in [p["name"] for p in parametros]


# --- Capacidad del parque: advertencia sin bloqueo (#29) ---


def test_advertencia_cuando_el_parque_queda_al_limite(
    api: TestClient, criaderos: Criaderos
) -> None:
    """#29: si el parque queda al limite la operacion sigue, con un aviso."""
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=2)

    primero = api.post(NUCLEOS, json=nucleo(parque, 1, 1), headers=como(a))
    segundo = api.post(NUCLEOS, json=nucleo(parque, 1, 2), headers=como(a))

    assert primero.status_code == segundo.status_code == 201
    assert primero.json()["advertencias"] == []
    assert segundo.json()["advertencias"] == [
        {
            "code": "parque_al_limite",
            "message": "El parque quedo lleno: no admite mas nucleos activos.",
            "details": {"capacidad_max": 2},
        }
    ]
    # El GET no repite el aviso: es de la operacion, no del nucleo.
    guardado = api.get(f"{NUCLEOS}/{segundo.json()['id']}", headers=como(a)).json()
    assert "advertencias" not in guardado


def test_advertencia_al_reactivar_y_llenar_el_parque(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=1)
    creado = crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    url = f"{NUCLEOS}/{creado['id']}"
    api.delete(url, headers=como(a))

    reactivado = api.patch(url, json={"activo": True}, headers=como(a))
    assert reactivado.status_code == 200
    assert [w["code"] for w in reactivado.json()["advertencias"]] == [
        "parque_al_limite"
    ]


def test_editar_sin_reactivar_no_avisa(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    parque = crear_parque(api, a, capacidad=1)
    creado = crear(api, NUCLEOS, a, nucleo(parque, 1, 1))
    movido = api.patch(f"{NUCLEOS}/{creado['id']}", json={"fila": 2}, headers=como(a))
    assert movido.json()["advertencias"] == []


# --- Materiales: stock y movimientos ---


def movimientos(material: dict[str, Any]) -> str:
    return f"{MATERIALES}/{material['id']}/movimientos"


def crear_material(api: TestClient, tenant: uuid.UUID) -> dict[str, Any]:
    return crear(api, MATERIALES, tenant, {"nombre": "Cupula", "unidad": "unidad"})


def test_material_nuevo_tiene_stock_cero(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    assert crear_material(api, a)["stock"] == "0"


def test_el_stock_es_la_suma_de_los_movimientos(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    material = crear_material(api, a)

    compra = api.post(
        movimientos(material), json={"tipo": "compra", "cantidad": 270}, headers=como(a)
    )
    assert compra.status_code == 201, compra.json()
    assert compra.json()["stock"] == "270.000"
    assert compra.json()["advertencias"] == []

    ajuste = api.post(
        movimientos(material),
        json={"tipo": "ajuste", "cantidad": "-20", "motivo": "Rotas"},
        headers=como(a),
    )
    assert ajuste.json()["stock"] == "250.000"

    actual = api.get(f"{MATERIALES}/{material['id']}", headers=como(a)).json()
    assert actual["stock"] == "250.000"


def test_stock_negativo_avisa_y_no_bloquea(
    api: TestClient, criaderos: Criaderos
) -> None:
    """docs/dominio.md: el stock negativo se permite, con una advertencia."""
    a, _ = criaderos
    material = crear_material(api, a)
    respuesta = api.post(
        movimientos(material),
        json={"tipo": "ajuste", "cantidad": "-5", "motivo": "Conteo"},
        headers=como(a),
    )
    assert respuesta.status_code == 201
    assert respuesta.json()["stock"] == "-5.000"
    assert respuesta.json()["advertencias"] == [
        {
            "code": "stock_negativo",
            "message": "El stock del material quedo negativo.",
            "details": {"stock": "-5.000"},
        }
    ]


@pytest.mark.parametrize(
    "datos",
    [
        {"tipo": "consumo", "cantidad": "-135"},
        {"tipo": "compra", "cantidad": "-10"},
        {"tipo": "compra", "cantidad": "0"},
        {"tipo": "ajuste", "cantidad": "5"},
        {"tipo": "ajuste", "cantidad": "0", "motivo": "x"},
        {"tipo": "compra", "cantidad": "1.2345"},
        {"tipo": "compra", "cantidad": "NaN"},
        {"tipo": "compra", "cantidad": 10, "tanda_id": str(uuid.uuid4())},
        {"tipo": "compra", "cantidad": 10, "fecha": str(date.today() + timedelta(1))},
    ],
    ids=[
        "consumo_a_mano",
        "compra_negativa",
        "compra_cero",
        "ajuste_sin_motivo",
        "ajuste_cero",
        "cuatro_decimales",
        "nan",
        "tanda_a_mano",
        "fecha_futura",
    ],
)
def test_movimiento_invalido_es_422(
    api: TestClient, criaderos: Criaderos, datos: dict[str, Any]
) -> None:
    """El consumo lo registra el alta de tanda (#27): por aca no entra."""
    a, _ = criaderos
    material = crear_material(api, a)
    respuesta = api.post(movimientos(material), json=datos, headers=como(a))
    assert respuesta.status_code == 422
    stock = api.get(f"{MATERIALES}/{material['id']}", headers=como(a)).json()["stock"]
    assert stock == "0"


def test_movimiento_con_fecha_pasada(api: TestClient, criaderos: Criaderos) -> None:
    """Los movimientos se cargan tarde: la fecha puede ser anterior a hoy."""
    a, _ = criaderos
    material = crear_material(api, a)
    ayer = str(date.today() - timedelta(1))
    respuesta = api.post(
        movimientos(material),
        json={"tipo": "compra", "cantidad": 10, "fecha": ayer},
        headers=como(a),
    )
    assert respuesta.json()["fecha"] == ayer


def test_material_dado_de_baja_no_admite_movimientos(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    material = crear_material(api, a)
    api.delete(f"{MATERIALES}/{material['id']}", headers=como(a))
    respuesta = api.post(
        movimientos(material), json={"tipo": "compra", "cantidad": 1}, headers=como(a)
    )
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "material_inactivo"


def test_movimientos_de_otro_criadero_son_404(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, b = criaderos
    material_de_a = crear_material(api, a)
    url = movimientos(material_de_a)
    alta = api.post(url, json={"tipo": "compra", "cantidad": 1}, headers=como(b))
    lista = api.get(url, headers=como(b))
    assert [alta.status_code, lista.status_code] == [404, 404]
    assert {codigo(alta), codigo(lista)} == {"material_no_encontrado"}


@pytest.mark.parametrize("rol", ["ventas", "lectura"])
def test_movimiento_sin_rol_es_403(
    api: TestClient, criaderos: Criaderos, rol: str
) -> None:
    a, _ = criaderos
    material = crear_material(api, a)
    respuesta = api.post(
        movimientos(material),
        json={"tipo": "compra", "cantidad": 1},
        headers=como(a, rol),
    )
    assert respuesta.status_code == 403
    assert api.get(movimientos(material), headers=como(a, rol)).status_code == 200


def test_movimientos_del_mas_nuevo_al_mas_viejo_paginados(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    material = crear_material(api, a)
    hoy = date.today()
    for dias, cantidad in ((3, 1), (1, 2), (2, 3), (1, 4)):
        api.post(
            movimientos(material),
            json={
                "tipo": "compra",
                "cantidad": cantidad,
                "fecha": str(hoy - timedelta(dias)),
            },
            headers=como(a),
        )

    vistos: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limite": 3}
    while True:
        pagina = api.get(movimientos(material), params=params, headers=como(a)).json()
        vistos += pagina["items"]
        if pagina["siguiente"] is None:
            break
        params["cursor"] = pagina["siguiente"]

    # Por fecha descendente; en el mismo dia, el ultimo cargado primero.
    assert [m["cantidad"] for m in vistos] == ["4.000", "2.000", "3.000", "1.000"]
    assert all(m["material_id"] == material["id"] for m in vistos)


def test_movimiento_idempotente(api: TestClient, criaderos: Criaderos) -> None:
    """Un reintento de la misma compra no la suma dos veces (#25)."""
    a, _ = criaderos
    material = crear_material(api, a)
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}
    datos = {"tipo": "compra", "cantidad": 270}
    api.post(movimientos(material), json=datos, headers=headers)
    repetido = api.post(movimientos(material), json=datos, headers=headers)
    assert repetido.headers["Idempotent-Replayed"] == "true"
    stock = api.get(f"{MATERIALES}/{material['id']}", headers=como(a)).json()["stock"]
    assert stock == "270.000"


# --- Items vendibles: alicuota ---


def test_item_vendible_arranca_en_21_por_ciento(
    api: TestClient, criaderos: Criaderos
) -> None:
    """docs/dominio.md: 21% por defecto (confirmado el 11/10), configurable."""
    a, _ = criaderos
    assert crear(api, ITEMS, a, {"nombre": "Reina"})["alicuota"] == "0.21"


@pytest.mark.parametrize("alicuota", list(Alicuota), ids=lambda a: a.name)
def test_item_vendible_acepta_cada_alicuota_del_enum(
    api: TestClient, criaderos: Criaderos, alicuota: Alicuota
) -> None:
    a, _ = criaderos
    item = crear(
        api,
        ITEMS,
        a,
        {"nombre": f"Item {alicuota.name}", "alicuota": str(alicuota.value)},
    )
    assert item["alicuota"] == str(alicuota.value)


def test_item_vendible_acepta_la_alicuota_como_numero(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    assert crear(api, ITEMS, a, {"nombre": "Reina", "alicuota": 0.105})["alicuota"] == (
        "0.105"
    )


@pytest.mark.parametrize("alicuota", ["0.19", "-0.21", "21", "veintiuno"])
def test_item_vendible_rechaza_otra_alicuota(
    api: TestClient, criaderos: Criaderos, alicuota: str
) -> None:
    a, _ = criaderos
    respuesta = api.post(
        ITEMS, json={"nombre": "Reina", "alicuota": alicuota}, headers=como(a)
    )
    assert respuesta.status_code == 422


# --- Banco (dominio del 16/10, migracion 0009) ---


def _enjaular_en_banco(
    motor_owner: Engine, tenant: uuid.UUID, banco_id: str, cantidad: int
) -> None:
    """Carga como duenio celdas enjauladas en el banco (todavia no hay #28)."""
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        raza = c.execute(
            text("INSERT INTO raza (nombre) VALUES ('Italiana') RETURNING id")
        ).scalar_one()
        tanda = c.execute(
            text(
                "INSERT INTO tanda (raza_id, fecha_traslarve) "
                "VALUES (:raza, '2026-09-20') RETURNING id"
            ),
            {"raza": raza},
        ).scalar_one()
        parque = c.execute(
            text(
                "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
                "VALUES ('P banco', 'Predio', 10) RETURNING id"
            )
        ).scalar_one()
        nucleo = c.execute(
            text(
                "INSERT INTO nucleo (parque_id, fila, posicion) "
                "VALUES (:parque, 1, 1) RETURNING id"
            ),
            {"parque": parque},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO celda (tanda_id, estado, nucleo_id, fecha_introduccion, "
                "banco_id) SELECT :tanda, 'enjaulada', :nucleo, '2026-09-30', :banco "
                "FROM generate_series(1, :cantidad)"
            ),
            {"tanda": tanda, "nucleo": nucleo, "banco": banco_id, "cantidad": cantidad},
        )


def test_banco_nuevo_con_capacidad_por_defecto(
    api: TestClient, criaderos: Criaderos
) -> None:
    banco = crear(api, BANCOS, criaderos[0], {"identificador": "B-norte"})

    assert banco["capacidad_max"] == 276  # confirmado por la clienta el 16/10
    assert banco["reinas_en_banco"] == 0
    assert banco["activo"] is True


def test_banco_cuenta_sus_reinas_desde_las_celdas(
    api: TestClient, criaderos: Criaderos, motor_owner: Engine
) -> None:
    tenant = criaderos[0]
    banco = crear(api, BANCOS, tenant, {"identificador": "B-norte"})
    _enjaular_en_banco(motor_owner, tenant, banco["id"], 4)

    leido = api.get(f"{BANCOS}/{banco['id']}", headers=como(tenant)).json()

    assert leido["reinas_en_banco"] == 4


def test_capacidad_menor_que_las_reinas_del_banco_es_409(
    api: TestClient, criaderos: Criaderos, motor_owner: Engine
) -> None:
    tenant = criaderos[0]
    banco = crear(api, BANCOS, tenant, {"identificador": "B-norte"})
    _enjaular_en_banco(motor_owner, tenant, banco["id"], 4)

    respuesta = api.patch(
        f"{BANCOS}/{banco['id']}", json={"capacidad_max": 3}, headers=como(tenant)
    )

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "capacidad_insuficiente"
    assert respuesta.json()["error"]["details"]["reinas_en_banco"] == 4


def test_banco_con_reinas_no_se_da_de_baja(
    api: TestClient, criaderos: Criaderos, motor_owner: Engine
) -> None:
    tenant = criaderos[0]
    banco = crear(api, BANCOS, tenant, {"identificador": "B-norte"})
    _enjaular_en_banco(motor_owner, tenant, banco["id"], 2)

    respuesta = api.delete(f"{BANCOS}/{banco['id']}", headers=como(tenant))

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "banco_con_reinas"


def test_capacidad_del_banco_como_texto_es_422(
    api: TestClient, criaderos: Criaderos
) -> None:
    respuesta = api.post(
        BANCOS,
        json={"identificador": "B-1", "capacidad_max": "276"},
        headers=como(criaderos[0]),
    )

    assert respuesta.status_code == 422
