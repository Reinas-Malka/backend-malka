"""Alta de tanda de punta a punta (#27).

Requests HTTP reales contra la app: token firmado (#21), sesion con el
tenant del token (#19), idempotencia (#25) y PostgreSQL con RLS. Cubre el DoD
de la issue: N cuadros -> N x 135 celdas en trasladada, sin raza o con una
raza de otro criadero, continuadora incompleta, idempotencia, consumo con
advertencia de stock negativo y fuga entre criaderos. Ademas: la madre es
opcional (dominio del 16/10) y el traslarve en domingo es 422
fecha_no_laborable, salvo un registro retroactivo.
"""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app
from app.services.produccion.tandas import CUPULAS_POR_CUADRO
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

TANDAS = "/api/v1/tandas"

# De hijas a padres, para poder borrar al limpiar.
TABLAS = (
    "idempotencia",
    "movimiento_material",
    "celda",
    "tanda",
    "material",
    "madre",
    "raza",
)

Criaderos = tuple[uuid.UUID, uuid.UUID]


def ultimo_domingo() -> date:
    """El domingo pasado mas cercano (nunca hoy: un retroactivo es pasado)."""
    hoy = date.today()
    return hoy - timedelta(days=(hoy.weekday() + 1) % 7 or 7)


def fecha_pasada() -> date:
    """Un viernes pasado: ni domingo ni futuro, sea cual sea el dia de hoy."""
    return ultimo_domingo() - timedelta(days=2)


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


def crear(api: TestClient, url: str, tenant: uuid.UUID, datos: dict[str, Any]) -> str:
    respuesta = api.post(url, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


def referencias(api: TestClient, tenant: uuid.UUID, stock: int = 0) -> dict[str, str]:
    """Raza y material de cupulas del criadero, con ese stock."""
    material = crear(
        api, "/api/v1/materiales", tenant, {"nombre": "Cupula", "unidad": "unidad"}
    )
    if stock:
        crear(
            api,
            f"/api/v1/materiales/{material}/movimientos",
            tenant,
            {"tipo": "compra", "cantidad": stock},
        )
    return {
        "raza_id": crear(api, "/api/v1/razas", tenant, {"nombre": "Carniola"}),
        "material_id": material,
    }


def crear_madre(api: TestClient, tenant: uuid.UUID, identificacion: str = "M-1") -> str:
    return crear(api, "/api/v1/madres", tenant, {"identificacion": identificacion})


def alta(refs: dict[str, str], **cambios: Any) -> dict[str, Any]:
    """Cuerpo de un alta valida minima: 1 cuadro, dia laborable, sin madre."""
    return {
        **refs,
        "fecha_traslarve": fecha_pasada().isoformat(),
        "cuadros": 1,
        **cambios,
    }


def contar(motor_owner: Engine, tenant: uuid.UUID, tabla: str) -> int:
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        return int(c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar_one())  # noqa: S608


def stock(api: TestClient, tenant: uuid.UUID, material_id: str) -> str:
    url = f"/api/v1/materiales/{material_id}"
    return str(api.get(url, headers=como(tenant)).json()["stock"])


# --- Alta valida ---


@pytest.mark.parametrize("cuadros", [1, 2, 3])
def test_n_cuadros_crean_n_por_135_celdas_trasladadas(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos, cuadros: int
) -> None:
    """DoD: N cuadros -> N x 135 celdas, en trasladada y sin nucleo."""
    a, _ = criaderos
    refs = referencias(api, a, stock=1000)
    respuesta = api.post(TANDAS, json=alta(refs, cuadros=cuadros), headers=como(a))
    assert respuesta.status_code == 201, respuesta.json()
    tanda = respuesta.json()
    esperadas = cuadros * CUPULAS_POR_CUADRO
    assert tanda["celdas"] == esperadas
    assert tanda["retroactivo"] is False
    assert "tenant_id" not in tanda

    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        filas = c.execute(
            text(
                "SELECT estado, nucleo_id IS NULL AS sin_nucleo, count(*) AS n "
                "FROM celda WHERE tanda_id = :id GROUP BY 1, 2"
            ),
            {"id": tanda["id"]},
        ).all()
    assert [(f.estado, f.sin_nucleo, f.n) for f in filas] == [
        ("trasladada", True, esperadas)
    ]


def test_el_alta_descuenta_las_cupulas(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """Movimiento de consumo directo en el alta, con la tanda que lo genero."""
    a, _ = criaderos
    refs = referencias(api, a, stock=500)
    respuesta = api.post(TANDAS, json=alta(refs, cuadros=2), headers=como(a))
    tanda = respuesta.json()

    assert tanda["consumo"] == {
        "id": tanda["consumo"]["id"],
        "material_id": refs["material_id"],
        "tipo": "consumo",
        "cantidad": "-270.000",
        "tanda_id": tanda["id"],
        "motivo": None,
        # El dia en que se usaron las cupulas, no el de la carga.
        "fecha": fecha_pasada().isoformat(),
    }
    assert tanda["stock"] == "230.000"
    assert tanda["advertencias"] == []
    assert stock(api, a, refs["material_id"]) == "230.000"
    assert contar(motor_owner, a, "movimiento_material") == 2


def test_stock_negativo_avisa_y_no_bloquea(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: sin cupulas cargadas la tanda se crea igual, con la advertencia."""
    a, _ = criaderos
    refs = referencias(api, a, stock=100)
    respuesta = api.post(TANDAS, json=alta(refs), headers=como(a))
    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["stock"] == "-35.000"
    assert respuesta.json()["advertencias"] == [
        {
            "code": "stock_negativo",
            "message": "El stock del material quedo negativo.",
            "details": {"stock": "-35.000"},
        }
    ]
    assert contar(motor_owner, a, "tanda") == 1


def test_get_devuelve_la_tanda_con_sus_celdas(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    creada = api.post(TANDAS, json=alta(refs, cuadros=3), headers=como(a)).json()
    respuesta = api.get(f"{TANDAS}/{creada['id']}", headers=como(a, "lectura"))
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "id": creada["id"],
        "raza_id": refs["raza_id"],
        "madre_id": None,
        "fecha_traslarve": fecha_pasada().isoformat(),
        "retroactivo": False,
        "fila_continuadora": None,
        "posicion_continuadora": None,
        "celdas": 3 * CUPULAS_POR_CUADRO,
        "celdas_por_estado": {"trasladada": 3 * CUPULAS_POR_CUADRO},
    }


def test_celdas_por_estado_se_cuentan_al_leer(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """Issue: GET con la cantidad de celdas por estado, calculada al leer.

    Las transiciones son de la #28: aca se descartan 20 celdas directo en la
    base, como hace test_api_disponibilidad con sus celdas.
    """
    a, _ = criaderos
    refs = referencias(api, a)
    creada = api.post(TANDAS, json=alta(refs), headers=como(a)).json()
    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        c.execute(
            text(
                "UPDATE celda SET estado = 'descartada', fecha_descarte = :hoy "
                "WHERE id IN (SELECT id FROM celda WHERE tanda_id = :id LIMIT 20)"
            ),
            {"id": creada["id"], "hoy": date.today()},
        )

    esperado = {"trasladada": CUPULAS_POR_CUADRO - 20, "descartada": 20}
    detalle = api.get(f"{TANDAS}/{creada['id']}", headers=como(a)).json()
    listado = api.get(TANDAS, headers=como(a)).json()["items"]
    assert detalle["celdas_por_estado"] == esperado
    assert detalle["celdas"] == CUPULAS_POR_CUADRO
    assert [t["celdas_por_estado"] for t in listado] == [esperado]


def test_alta_con_madre_y_continuadora(api: TestClient, criaderos: Criaderos) -> None:
    """Dominio 16/10: madre opcional y ubicacion de los primeros 10 dias."""
    a, _ = criaderos
    refs = referencias(api, a)
    madre = crear_madre(api, a)
    datos = alta(refs, madre_id=madre, fila_continuadora=2, posicion_continuadora=7)
    respuesta = api.post(TANDAS, json=datos, headers=como(a))
    assert respuesta.status_code == 201, respuesta.json()
    tanda = api.get(f"{TANDAS}/{respuesta.json()['id']}", headers=como(a)).json()
    assert tanda["madre_id"] == madre
    assert (tanda["fila_continuadora"], tanda["posicion_continuadora"]) == (2, 7)


@pytest.mark.parametrize(
    "cambios",
    [
        {"fila_continuadora": 2},
        {"posicion_continuadora": 7},
        {"fila_continuadora": 0, "posicion_continuadora": 7},
    ],
    ids=["fila_sin_posicion", "posicion_sin_fila", "fila_cero"],
)
def test_continuadora_incompleta_o_invalida_es_422(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos, cambios: dict[str, Any]
) -> None:
    """Las dos o ninguna, como el CHECK ck_tanda_continuadora (0009)."""
    a, _ = criaderos
    refs = referencias(api, a)
    respuesta = api.post(TANDAS, json=alta(refs, **cambios), headers=como(a))
    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"
    assert contar(motor_owner, a, "tanda") == 0


# --- Fechas y domingo ---


def test_domingo_es_422_y_no_crea_nada(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """El traslarve no se hace los domingos: 422 y no se descuenta nada."""
    a, _ = criaderos
    refs = referencias(api, a, stock=500)
    domingo = ultimo_domingo().isoformat()
    respuesta = api.post(
        TANDAS, json=alta(refs, fecha_traslarve=domingo), headers=como(a)
    )
    assert respuesta.status_code == 422
    assert codigo(respuesta) == "fecha_no_laborable"
    assert respuesta.json()["error"]["details"] == {"fecha_traslarve": domingo}
    assert contar(motor_owner, a, "tanda") == 0
    assert contar(motor_owner, a, "celda") == 0
    assert stock(api, a, refs["material_id"]) == "500.000"


def test_domingo_retroactivo_se_acepta(api: TestClient, criaderos: Criaderos) -> None:
    """La carga tardia de una tanda ya hecha admite domingo y queda marcada."""
    a, _ = criaderos
    refs = referencias(api, a)
    datos = alta(refs, fecha_traslarve=ultimo_domingo().isoformat(), retroactivo=True)
    respuesta = api.post(TANDAS, json=datos, headers=como(a))
    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["retroactivo"] is True
    assert respuesta.json()["fecha_traslarve"] == ultimo_domingo().isoformat()


def test_retroactivo_de_hoy_o_fecha_futura_es_422(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    hoy = date.today()
    respuestas = [
        api.post(
            TANDAS,
            json=alta(refs, fecha_traslarve=hoy.isoformat(), retroactivo=True),
            headers=como(a),
        ),
        api.post(
            TANDAS,
            json=alta(refs, fecha_traslarve=(hoy + timedelta(days=1)).isoformat()),
            headers=como(a),
        ),
    ]
    assert [r.status_code for r in respuestas] == [422, 422]
    assert {codigo(r) for r in respuestas} == {"validacion"}
    assert contar(motor_owner, a, "tanda") == 0


# --- Validaciones ---


def test_sin_madre_se_acepta(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """Dominio 16/10: madres fuera de alcance, tanda.madre_id es NULL."""
    a, _ = criaderos
    refs = referencias(api, a)
    respuesta = api.post(TANDAS, json=alta(refs), headers=como(a))
    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["madre_id"] is None
    assert contar(motor_owner, a, "tanda") == 1


@pytest.mark.parametrize("campo", ["raza_id", "material_id", "cuadros"])
def test_sin_campo_obligatorio_es_422(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos, campo: str
) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    datos = alta(refs)
    del datos[campo]
    respuesta = api.post(TANDAS, json=datos, headers=como(a))
    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"
    assert contar(motor_owner, a, "tanda") == 0


@pytest.mark.parametrize("campo", ["madre_id", "raza_id", "material_id"])
def test_referencia_inexistente_o_de_otro_criadero_es_422(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos, campo: str
) -> None:
    """Un id del cuerpo que no existe es 422, igual que en pedidos (#34)."""
    a, b = criaderos
    refs = {**referencias(api, a), "madre_id": crear_madre(api, a)}
    ajenas = {**referencias(api, b), "madre_id": crear_madre(api, b)}
    esperado = f"{campo.removesuffix('_id')}_inexistente"
    for otro in (str(uuid.uuid4()), ajenas[campo]):
        respuesta = api.post(TANDAS, json=alta(refs, **{campo: otro}), headers=como(a))
        assert respuesta.status_code == 422, respuesta.json()
        assert codigo(respuesta) == esperado
    assert contar(motor_owner, a, "tanda") == 0


@pytest.mark.parametrize(
    ("campo", "url", "esperado"),
    [
        ("madre_id", "/api/v1/madres", "madre_inactiva"),
        ("raza_id", "/api/v1/razas", "raza_inactiva"),
        ("material_id", "/api/v1/materiales", "material_inactivo"),
    ],
)
def test_referencia_dada_de_baja_es_422(
    api: TestClient,
    motor_owner: Engine,
    criaderos: Criaderos,
    campo: str,
    url: str,
    esperado: str,
) -> None:
    a, _ = criaderos
    refs = {**referencias(api, a), "madre_id": crear_madre(api, a)}
    baja = api.delete(f"{url}/{refs[campo]}", headers=como(a))
    assert baja.status_code == 204
    respuesta = api.post(TANDAS, json=alta(refs), headers=como(a))
    assert respuesta.status_code == 422
    assert codigo(respuesta) == esperado
    assert contar(motor_owner, a, "tanda") == 0
    assert contar(motor_owner, a, "celda") == 0


@pytest.mark.parametrize(
    "cambios",
    [{"cuadros": 0}, {"cuadros": 4}, {"cuadros": "2"}, {"cupulas": 135}],
    ids=["cero", "cuatro", "texto", "cupulas_libres"],
)
def test_cuadros_invalidos_son_422(
    api: TestClient, criaderos: Criaderos, cambios: dict[str, Any]
) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    respuesta = api.post(TANDAS, json=alta(refs, **cambios), headers=como(a))
    assert respuesta.status_code == 422
    assert codigo(respuesta) == "validacion"


# --- Idempotencia ---


def test_reintento_con_la_misma_clave_crea_una_sola_tanda(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: un reintento no duplica la tanda, ni las celdas, ni el consumo."""
    a, _ = criaderos
    refs = referencias(api, a, stock=500)
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}
    primera = api.post(TANDAS, json=alta(refs), headers=headers)
    repetida = api.post(TANDAS, json=alta(refs), headers=headers)

    assert primera.status_code == repetida.status_code == 201
    assert repetida.headers["Idempotent-Replayed"] == "true"
    assert repetida.json() == primera.json()
    assert contar(motor_owner, a, "tanda") == 1
    assert contar(motor_owner, a, "celda") == CUPULAS_POR_CUADRO
    assert stock(api, a, refs["material_id"]) == "365.000"


def test_la_misma_clave_con_otro_cuerpo_es_409(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}
    api.post(TANDAS, json=alta(refs), headers=headers)
    otra = api.post(TANDAS, json=alta(refs, cuadros=2), headers=headers)
    assert otra.status_code == 409


# --- Roles y criaderos ---


@pytest.mark.parametrize("rol", ["ventas", "lectura"])
def test_alta_sin_rol_es_403(api: TestClient, criaderos: Criaderos, rol: str) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    respuesta = api.post(TANDAS, json=alta(refs), headers=como(a, rol))
    assert respuesta.status_code == 403
    assert codigo(respuesta) == "sin_permiso"


def test_alta_como_admin(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    respuesta = api.post(TANDAS, json=alta(refs), headers=como(a, "admin"))
    assert respuesta.status_code == 201


def test_sin_token_es_401(api: TestClient) -> None:
    respuesta = api.get(TANDAS)
    assert respuesta.status_code == 401


def test_tanda_de_otro_criadero_es_404(api: TestClient, criaderos: Criaderos) -> None:
    a, b = criaderos
    refs = referencias(api, a)
    creada = api.post(TANDAS, json=alta(refs), headers=como(a)).json()
    ajena = api.get(f"{TANDAS}/{creada['id']}", headers=como(b))
    inexistente = api.get(f"{TANDAS}/{uuid.uuid4()}", headers=como(b))
    assert ajena.status_code == inexistente.status_code == 404
    assert codigo(ajena) == codigo(inexistente) == "tanda_no_encontrada"
    assert api.get(TANDAS, headers=como(b)).json()["items"] == []


# --- Listado ---


def test_listado_paginado_de_la_mas_nueva_a_la_mas_vieja(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    base = fecha_pasada()
    fechas = [base - timedelta(days=d) for d in (3, 0, 1, 2)]
    for fecha in fechas:
        api.post(
            TANDAS, json=alta(refs, fecha_traslarve=fecha.isoformat()), headers=como(a)
        )

    vistas: list[str] = []
    params: dict[str, Any] = {"limite": 3}
    while True:
        pagina = api.get(TANDAS, params=params, headers=como(a)).json()
        vistas += [t["fecha_traslarve"] for t in pagina["items"]]
        if pagina["siguiente"] is None:
            break
        params["cursor"] = pagina["siguiente"]

    assert vistas == [f.isoformat() for f in sorted(fechas, reverse=True)]


def test_listado_filtrado_por_raza(api: TestClient, criaderos: Criaderos) -> None:
    a, _ = criaderos
    refs = referencias(api, a)
    otra_raza = crear(api, "/api/v1/razas", a, {"nombre": "Italiana"})
    api.post(TANDAS, json=alta(refs), headers=como(a))
    otra = api.post(TANDAS, json=alta(refs, raza_id=otra_raza), headers=como(a))

    pagina = api.get(TANDAS, params={"raza_id": otra_raza}, headers=como(a)).json()
    assert [t["id"] for t in pagina["items"]] == [otra.json()["id"]]


def test_listado_filtrado_por_fecha(api: TestClient, criaderos: Criaderos) -> None:
    """desde y hasta filtran por fecha_traslarve e incluyen los dos dias."""
    a, _ = criaderos
    refs = referencias(api, a)
    base = fecha_pasada()
    fechas = [base - timedelta(days=d) for d in (0, 1, 2, 3)]
    for fecha in fechas:
        api.post(
            TANDAS, json=alta(refs, fecha_traslarve=fecha.isoformat()), headers=como(a)
        )

    params = {"desde": fechas[2].isoformat(), "hasta": fechas[1].isoformat()}
    pagina = api.get(TANDAS, params=params, headers=como(a)).json()
    assert [t["fecha_traslarve"] for t in pagina["items"]] == [
        fechas[1].isoformat(),
        fechas[2].isoformat(),
    ]


def test_openapi_documenta_los_endpoints(api: TestClient) -> None:
    rutas = api.get("/openapi.json").json()["paths"]
    assert set(rutas[TANDAS]) == {"get", "post"}
    assert set(rutas[f"{TANDAS}/{{tanda_id}}"]) == {"get"}
    parametros = rutas[TANDAS]["post"]["parameters"]
    assert "Idempotency-Key" in [p["name"] for p in parametros]
