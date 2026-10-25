"""Operaciones por lote sobre una tanda, de punta a punta (#28).

Requests HTTP reales contra la app: token firmado (#21), sesion con el
tenant del token (#19), idempotencia (#25) y PostgreSQL con RLS. Cubre el DoD
de la issue: reparto parejo, parque sin nucleos, cosecha (cosechadas y
descartadas con nucleo, una sola vez), transicion invalida y cantidad
excedida, la disponibilidad que sube con la cosecha y la fuga entre
criaderos. El banco lleno con dos lotes simultaneos esta en
tests/test_concurrencia_banco.py.
"""

import uuid
from collections.abc import Iterator
from datetime import date
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

TANDAS = "/api/v1/tandas"

# Fechas fijas y pasadas. El 2/10/2026 fue viernes; el 11 y el 18, domingos.
TRASLARVE = date(2026, 10, 2)
INTRODUCCION = date(2026, 10, 12)
COSECHA = date(2026, 10, 20)

# De hijas a padres, para poder borrar al limpiar.
TABLAS = (
    "idempotencia",
    "movimiento_material",
    "celda",
    "tanda",
    "nucleo",
    "parque",
    "banco",
    "material",
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


def crear(api: TestClient, url: str, tenant: uuid.UUID, datos: dict[str, Any]) -> str:
    respuesta = api.post(url, json=datos, headers=como(tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return str(respuesta.json()["id"])


def crear_tanda(
    api: TestClient, tenant: uuid.UUID, nombre_raza: str = "Carniola"
) -> str:
    """Una tanda de 1 cuadro: 135 celdas en trasladada."""
    raza = crear(api, "/api/v1/razas", tenant, {"nombre": nombre_raza})
    material = crear(
        api,
        "/api/v1/materiales",
        tenant,
        {"nombre": f"Cupula {nombre_raza}", "unidad": "unidad"},
    )
    datos = {
        "raza_id": raza,
        "material_id": material,
        "fecha_traslarve": TRASLARVE.isoformat(),
        "cuadros": 1,
    }
    return crear(api, TANDAS, tenant, datos)


def crear_parque(
    motor_owner: Engine,
    api: TestClient,
    tenant: uuid.UUID,
    nucleos: int,
    nombre: str = "Parque norte",
) -> str:
    """Un parque con esa cantidad de nucleos activos, en la fila 1."""
    datos = {"nombre": nombre, "ubicacion": "Predio", "capacidad_max": 100}
    parque = crear(api, "/api/v1/parques", tenant, datos)
    with motor_owner.begin() as c:
        fijar_tenant(c, tenant)
        c.execute(
            text(
                "INSERT INTO nucleo (parque_id, fila, posicion) "
                "SELECT :parque, 1, g FROM generate_series(1, :n) AS g"
            ),
            {"parque": parque, "n": nucleos},
        )
    return parque


def crear_banco(api: TestClient, tenant: uuid.UUID, capacidad: int) -> str:
    datos = {"identificador": f"B-{capacidad}", "capacidad_max": capacidad}
    return crear(api, "/api/v1/bancos", tenant, datos)


def operar(
    api: TestClient,
    tenant: uuid.UUID,
    tanda: str,
    accion: str,
    rol: str = "produccion",
    **datos: Any,
) -> Any:
    cuerpo = {"accion": accion, **datos}
    return api.post(
        f"{TANDAS}/{tanda}/operaciones", json=cuerpo, headers=como(tenant, rol)
    )


def introducir(
    api: TestClient, tenant: uuid.UUID, tanda: str, parque: str, cantidad: int
) -> Any:
    respuesta = operar(
        api,
        tenant,
        tanda,
        "introducir",
        cantidad=cantidad,
        parque_id=parque,
        fecha=INTRODUCCION.isoformat(),
    )
    assert respuesta.status_code == 201, respuesta.json()
    return respuesta.json()


def cosechar(api: TestClient, tenant: uuid.UUID, tanda: str, cantidad: int) -> Any:
    return operar(
        api, tenant, tanda, "cosechar", cantidad=cantidad, fecha=COSECHA.isoformat()
    )


def por_estado(api: TestClient, tenant: uuid.UUID, tanda: str) -> dict[str, int]:
    tanda_leida = api.get(f"{TANDAS}/{tanda}", headers=como(tenant)).json()
    return dict(tanda_leida["celdas_por_estado"])


# --- Descartar ---


def test_descartar_antes_de_introducir(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    respuesta = operar(
        api, a, tanda, "descartar", cantidad=10, fecha=INTRODUCCION.isoformat()
    )
    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["tanda"]["celdas_por_estado"] == {
        "trasladada": 125,
        "descartada": 10,
    }
    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        filas = c.execute(
            text(
                "SELECT nucleo_id IS NULL AS sin_nucleo, fecha_descarte, count(*) AS n "
                "FROM celda WHERE estado = 'descartada' GROUP BY 1, 2"
            )
        ).all()
    assert [(f.sin_nucleo, f.fecha_descarte, f.n) for f in filas] == [
        (True, INTRODUCCION, 10)
    ]


# --- Introducir ---


def test_120_celdas_en_60_nucleos_vacios_son_2_por_nucleo(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: reparto parejo."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=60)

    resultado = introducir(api, a, tanda, parque, 120)

    reparto = resultado["reparto"]
    assert len(reparto) == 60
    assert {n["celdas"] for n in reparto} == {2}
    assert [n["posicion"] for n in reparto] == list(range(1, 61))
    assert resultado["tanda"]["celdas_por_estado"] == {
        "trasladada": 15,
        "introducida": 120,
    }
    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        por_nucleo = c.execute(
            text(
                "SELECT count(*) FROM celda WHERE estado = 'introducida' "
                "AND fecha_introduccion = :fecha GROUP BY nucleo_id"
            ),
            {"fecha": INTRODUCCION},
        ).scalars()
        assert sorted(set(por_nucleo)) == [2]


def test_con_nucleos_ocupados_van_primero_a_los_mas_libres(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: los mas libres se llenan antes, aunque sean de otra tanda."""
    a, _ = criaderos
    primera = crear_tanda(api, a, "Carniola")
    segunda = crear_tanda(api, a, "Italiana")
    parque = crear_parque(motor_owner, api, a, nucleos=3)

    # 4 celdas en 3 nucleos vacios: 2, 1 y 1.
    antes = introducir(api, a, primera, parque, 4)["reparto"]
    assert [n["celdas"] for n in antes] == [2, 1, 1]

    # Las 2 nuevas van a los dos que tienen 1, no al que tiene 2.
    despues = introducir(api, a, segunda, parque, 2)["reparto"]
    assert [(n["posicion"], n["celdas"]) for n in despues] == [(2, 1), (3, 1)]


def test_parque_sin_nucleos_activos_es_409(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=0)
    respuesta = operar(
        api,
        a,
        tanda,
        "introducir",
        cantidad=10,
        parque_id=parque,
        fecha=INTRODUCCION.isoformat(),
    )
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_sin_nucleos"
    assert por_estado(api, a, tanda) == {"trasladada": 135}


def test_parque_dado_de_baja_es_409(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=0)
    api.delete(f"/api/v1/parques/{parque}", headers=como(a))
    respuesta = operar(
        api,
        a,
        tanda,
        "introducir",
        cantidad=10,
        parque_id=parque,
        fecha=INTRODUCCION.isoformat(),
    )
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "parque_inactivo"


def test_introducir_un_domingo_se_acepta(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """docs/dominio.md: las fechas biologicas son dias calendario."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=2)
    domingo = date(2026, 10, 11)
    respuesta = operar(
        api,
        a,
        tanda,
        "introducir",
        cantidad=10,
        parque_id=parque,
        fecha=domingo.isoformat(),
    )
    assert respuesta.status_code == 201, respuesta.json()


# --- Cosechar ---


def test_cosecha_enjaula_las_cosechadas_y_descarta_el_resto(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: 120 introducidas y 95 cosechadas son 95 enjauladas + 25 descartadas."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=60)
    introducir(api, a, tanda, parque, 120)

    respuesta = cosechar(api, a, tanda, 95)

    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["descartadas_en_cosecha"] == 25
    assert respuesta.json()["tanda"]["celdas_por_estado"] == {
        "trasladada": 15,
        "enjaulada": 95,
        "descartada": 25,
    }
    with motor_owner.begin() as c:
        fijar_tenant(c, a)
        filas = c.execute(
            text(
                "SELECT estado, nucleo_id IS NOT NULL AS con_nucleo, "
                "fecha_enjaulado, fecha_descarte, count(*) AS n FROM celda "
                "WHERE estado IN ('enjaulada', 'descartada') "
                "GROUP BY 1, 2, 3, 4 ORDER BY 1"
            )
        ).all()
    assert [tuple(f) for f in filas] == [
        ("descartada", True, None, COSECHA, 25),
        ("enjaulada", True, COSECHA, None, 95),
    ]


def test_una_segunda_cosecha_es_409(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: una tanda se cosecha una sola vez."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=10)
    introducir(api, a, tanda, parque, 20)
    assert cosechar(api, a, tanda, 15).status_code == 201

    segunda = cosechar(api, a, tanda, 1)
    assert segunda.status_code == 409
    assert codigo(segunda) == "tanda_cosechada"


def test_cosecha_de_cero_tambien_cuenta(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """Se perdio todo: todas a descartada, y la tanda queda cosechada."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 10)

    respuesta = cosechar(api, a, tanda, 0)

    assert respuesta.status_code == 201, respuesta.json()
    assert respuesta.json()["descartadas_en_cosecha"] == 10
    assert codigo(cosechar(api, a, tanda, 0)) == "tanda_cosechada"


def test_despues_de_la_cosecha_no_se_introduce_mas(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 10)
    cosechar(api, a, tanda, 8)

    respuesta = operar(
        api,
        a,
        tanda,
        "introducir",
        cantidad=5,
        parque_id=parque,
        fecha=COSECHA.isoformat(),
    )
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "transicion_invalida"


def test_cosechar_sin_celdas_en_el_parque_es_transicion_invalida(
    api: TestClient, criaderos: Criaderos
) -> None:
    """DoD: transicion invalida."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    respuesta = cosechar(api, a, tanda, 5)
    assert respuesta.status_code == 409
    assert codigo(respuesta) == "transicion_invalida"


def test_la_disponibilidad_sube_con_la_cosecha(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: cosechar es lo que suma stock para vender (ADR 0014)."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=10)
    introducir(api, a, tanda, parque, 50)

    def disponible() -> int:
        filas = api.get("/api/v1/disponibilidad", headers=como(a)).json()
        return int(filas[0]["disponible"])

    assert disponible() == 0
    cosechar(api, a, tanda, 40)
    assert disponible() == 40


# --- Cantidad excedida y fechas ---


@pytest.mark.parametrize(
    ("accion", "datos", "disponibles"),
    [
        ("descartar", {"cantidad": 136}, 135),
        ("cosechar", {"cantidad": 11}, 10),
        ("pasar_a_banco", {"cantidad": 9}, 8),
    ],
    ids=["descartar", "cosechar", "pasar_a_banco"],
)
def test_pedir_mas_celdas_de_las_que_hay_es_409(
    api: TestClient,
    motor_owner: Engine,
    criaderos: Criaderos,
    accion: str,
    datos: dict[str, Any],
    disponibles: int,
) -> None:
    """DoD: cantidad excedida, con cuantas hay."""
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    if accion != "descartar":
        parque = crear_parque(motor_owner, api, a, nucleos=5)
        introducir(api, a, tanda, parque, 10)
    if accion == "pasar_a_banco":
        cosechar(api, a, tanda, 8)
        datos = {**datos, "banco_id": crear_banco(api, a, 100)}
    else:
        datos = {**datos, "fecha": COSECHA.isoformat()}

    respuesta = operar(api, a, tanda, accion, **datos)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "cantidad_excedida"
    assert respuesta.json()["error"]["details"]["disponibles"] == disponibles


def test_fecha_anterior_al_traslarve_o_a_la_introduccion_es_422(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    antes_del_traslarve = operar(
        api,
        a,
        tanda,
        "introducir",
        cantidad=5,
        parque_id=parque,
        fecha=date(2026, 10, 1).isoformat(),
    )
    introducir(api, a, tanda, parque, 10)
    antes_de_introducir = operar(
        api, a, tanda, "cosechar", cantidad=5, fecha=date(2026, 10, 5).isoformat()
    )

    for respuesta in (antes_del_traslarve, antes_de_introducir):
        assert respuesta.status_code == 422
        assert codigo(respuesta) == "fecha_anterior"
    assert antes_de_introducir.json()["error"]["details"]["minima"] == (
        INTRODUCCION.isoformat()
    )


def test_accion_desconocida_o_fecha_futura_es_422(
    api: TestClient, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    respuestas = [
        operar(api, a, tanda, "fecundar", cantidad=5, fecha=COSECHA.isoformat()),
        operar(api, a, tanda, "descartar", cantidad=5, fecha="2999-01-01"),
    ]
    assert [r.status_code for r in respuestas] == [422, 422]
    assert {codigo(r) for r in respuestas} == {"validacion"}


# --- Pasar a banco ---


def test_pasar_a_banco(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 10)
    cosechar(api, a, tanda, 8)
    banco = crear_banco(api, a, 100)

    respuesta = operar(api, a, tanda, "pasar_a_banco", cantidad=5, banco_id=banco)

    assert respuesta.status_code == 201, respuesta.json()
    leido = api.get(f"/api/v1/bancos/{banco}", headers=como(a)).json()
    assert leido["reinas_en_banco"] == 5
    # Las otras 3 siguen enjauladas, sin banco: tambien se pueden vender.
    assert respuesta.json()["tanda"]["celdas_por_estado"]["enjaulada"] == 8


def test_banco_sin_lugar_es_409_con_cuantas_caben(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 20)
    cosechar(api, a, tanda, 20)
    banco = crear_banco(api, a, 10)
    operar(api, a, tanda, "pasar_a_banco", cantidad=7, banco_id=banco)

    respuesta = operar(api, a, tanda, "pasar_a_banco", cantidad=5, banco_id=banco)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "banco_lleno"
    assert respuesta.json()["error"]["details"] == {"caben": 3, "capacidad_max": 10}


def test_banco_dado_de_baja_es_409(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    introducir(api, a, tanda, parque, 10)
    cosechar(api, a, tanda, 8)
    banco = crear_banco(api, a, 100)
    api.delete(f"/api/v1/bancos/{banco}", headers=como(a))

    respuesta = operar(api, a, tanda, "pasar_a_banco", cantidad=5, banco_id=banco)

    assert respuesta.status_code == 409
    assert codigo(respuesta) == "banco_inactivo"


# --- Idempotencia, roles y criaderos ---


def test_reintento_con_la_misma_clave_opera_una_sola_vez(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    parque = crear_parque(motor_owner, api, a, nucleos=5)
    headers = {**como(a), "Idempotency-Key": str(uuid.uuid4())}
    cuerpo = {
        "accion": "introducir",
        "cantidad": 10,
        "parque_id": parque,
        "fecha": INTRODUCCION.isoformat(),
    }
    url = f"{TANDAS}/{tanda}/operaciones"

    primera = api.post(url, json=cuerpo, headers=headers)
    repetida = api.post(url, json=cuerpo, headers=headers)

    assert primera.status_code == repetida.status_code == 201
    assert repetida.headers["Idempotent-Replayed"] == "true"
    assert por_estado(api, a, tanda) == {"trasladada": 125, "introducida": 10}


@pytest.mark.parametrize("rol", ["ventas", "lectura"])
def test_operar_sin_rol_es_403(api: TestClient, criaderos: Criaderos, rol: str) -> None:
    a, _ = criaderos
    tanda = crear_tanda(api, a)
    respuesta = operar(
        api, a, tanda, "descartar", rol=rol, cantidad=1, fecha=COSECHA.isoformat()
    )
    assert respuesta.status_code == 403
    assert codigo(respuesta) == "sin_permiso"


def test_lo_de_otro_criadero_es_404(
    api: TestClient, motor_owner: Engine, criaderos: Criaderos
) -> None:
    """DoD: fuga entre criaderos. Ajeno responde igual que inexistente."""
    a, b = criaderos
    tanda_a = crear_tanda(api, a)
    tanda_b = crear_tanda(api, b)
    parque_b = crear_parque(motor_owner, api, b, nucleos=5)
    banco_b = crear_banco(api, b, 100)
    fecha = INTRODUCCION.isoformat()

    tanda_ajena = operar(api, a, tanda_b, "descartar", cantidad=1, fecha=fecha)
    tanda_inexistente = operar(
        api, a, str(uuid.uuid4()), "descartar", cantidad=1, fecha=fecha
    )
    parque_ajeno = operar(
        api, a, tanda_a, "introducir", cantidad=1, parque_id=parque_b, fecha=fecha
    )

    assert tanda_ajena.status_code == tanda_inexistente.status_code == 404
    assert codigo(tanda_ajena) == codigo(tanda_inexistente) == "tanda_no_encontrada"
    assert parque_ajeno.status_code == 404
    assert codigo(parque_ajeno) == "parque_no_encontrado"
    # Nada se movio en ninguno de los dos criaderos.
    assert por_estado(api, a, tanda_a) == {"trasladada": 135}
    assert por_estado(api, b, tanda_b) == {"trasladada": 135}

    # Un banco de otro criadero tampoco existe para A.
    parque_a = crear_parque(motor_owner, api, a, nucleos=5, nombre="Parque A")
    introducir(api, a, tanda_a, parque_a, 10)
    cosechar(api, a, tanda_a, 8)
    banco_ajeno = operar(api, a, tanda_a, "pasar_a_banco", cantidad=1, banco_id=banco_b)
    assert banco_ajeno.status_code == 404
    assert codigo(banco_ajeno) == "banco_no_encontrado"


def test_openapi_documenta_las_operaciones(api: TestClient) -> None:
    rutas = api.get("/openapi.json").json()["paths"]
    post = rutas[f"{TANDAS}/{{tanda_id}}/operaciones"]["post"]
    parametros = [p["name"] for p in post["parameters"]]
    assert "Idempotency-Key" in parametros
    esquema = post["requestBody"]["content"]["application/json"]["schema"]
    assert set(esquema["discriminator"]["mapping"]) == {
        "descartar",
        "introducir",
        "cosechar",
        "pasar_a_banco",
    }
