"""Propuestas de catalogos, nucleos, movimientos y cancelacion de pedidos.

Mismo criterio que test_api_asistente_propuestas.py: el cuerpo valida
contra el schema real del endpoint, el resumen y los roles los pone el
servidor, confirmarla con el endpoint real funciona y, hasta entonces, la
base no cambia. Al final, el rol resultado y el flujo encadenado parque ->
resultado -> nucleos con el modelo falso.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy import Engine, text

from app.main import app
from app.schemas.produccion import (
    BancoCrear,
    ItemVendibleCrear,
    MaterialCrear,
    MovimientoCrear,
    NucleosLoteCrear,
    ParqueCrear,
    RazaCrear,
)
from app.schemas.transiciones import CambioDeEstado
from app.services.ia.asistente.conversacion import ENCABEZADO_RESULTADO
from tests.asistente_datos import (
    Criadero,
    ModeloGuionado,
    cargar_criadero,
    como,
    confirmar,
    conversar,
    datos_de,
    error_de,
    limpiar,
    pide,
    pide_varias,
    proponer,
    responde,
    resumen,
)
from tests.conftest import fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

PRODUCCION = ["admin", "produccion"]


@pytest.fixture
def criadero(motor_owner: Engine) -> Iterator[Criadero]:
    tenant = uuid.uuid4()
    with motor_owner.begin() as c:
        datos = cargar_criadero(c, tenant)
    yield datos
    limpiar(motor_owner, tenant)


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def ejecutar_sql(motor: Engine, tenant: uuid.UUID, sql: str, **params: Any) -> Any:
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        resultado = c.execute(text(sql), params)
        return resultado.scalar() if resultado.returns_rows else None


def foto(motor: Engine, tenant: uuid.UUID) -> dict[str, Any]:
    """Todo lo que estas propuestas podrian cambiar."""
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        conteos = {
            tabla: c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar_one()  # noqa: S608
            for tabla in (
                "parque",
                "nucleo",
                "banco",
                "raza",
                "item_vendible",
                "material",
                "movimiento_material",
                "reserva",
            )
        }
        estados = c.execute(text("SELECT estado FROM pedido ORDER BY id")).all()
    return {**conteos, "pedidos": estados}


# --- crear_parque ---


def test_parque_valido_con_resumen_y_roles_de_produccion(
    api: TestClient, criadero: Criadero
) -> None:
    entrada = {"nombre": "Casa Santi", "ubicacion": "Grand Bell", "capacidad_max": 100}

    propuesta, visto = proponer(api, criadero, "proponer_parque", entrada)

    assert propuesta["accion"] == "crear_parque"
    assert (propuesta["metodo"], propuesta["ruta"]) == ("POST", "/api/v1/parques")
    assert propuesta["cuerpo"] == entrada
    ParqueCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta) == {
        "Nombre": "Casa Santi",
        "Ubicación": "Grand Bell",
        "Capacidad": "100 núcleos",
        "Núcleos": "Ninguno todavía: se agregan después, por cantidad.",
    }
    assert propuesta["roles_permitidos"] == PRODUCCION
    assert visto["el_usuario_puede_confirmarla"] is True
    creado = confirmar(api, criadero, propuesta, "produccion")
    assert creado.status_code == 201
    assert creado.json()["nucleos_activos"] == 0


def test_ventas_no_puede_confirmar_un_parque(
    api: TestClient, criadero: Criadero
) -> None:
    entrada = {"nombre": "Casa Santi", "ubicacion": "Grand Bell", "capacidad_max": 100}

    propuesta, visto = proponer(api, criadero, "proponer_parque", entrada, rol="ventas")

    assert visto["el_usuario_puede_confirmarla"] is False
    assert confirmar(api, criadero, propuesta, "ventas").status_code == 403


@pytest.mark.parametrize(
    ("entrada", "mensaje"),
    [
        (
            {"nombre": "Casa Santi", "ubicacion": "Grand Bell"},
            "Faltan datos obligatorios: capacidad máxima (en núcleos)",
        ),
        (
            {"nombre": "Casa Santi"},
            "Faltan datos obligatorios: ubicación, capacidad máxima (en núcleos)",
        ),
        (
            {"nombre": "Casa Santi", "ubicacion": "X", "capacidad_max": 0},
            "capacidad_max",
        ),
        (
            {"nombre": "parque NORTE", "ubicacion": "X", "capacidad_max": 10},
            'Ya existe un parque "Parque Norte"',
        ),
    ],
)
def test_parque_incompleto_invalido_o_repetido_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, entrada: dict[str, Any], mensaje: str
) -> None:
    assert mensaje in error_de(api, criadero, "proponer_parque", entrada)


def test_faltan_datos_pide_preguntar(api: TestClient, criadero: Criadero) -> None:
    mensaje = error_de(api, criadero, "proponer_material", {"nombre": "Jaulas"})

    assert mensaje == (
        "Faltan datos obligatorios: unidad. Preguntale al usuario; no los "
        "completes por tu cuenta."
    )


# --- crear_nucleos ---


@pytest.mark.parametrize("como_lo_nombra", ["parque norte", "norte", "id"])
def test_nucleos_resuelve_el_parque_por_nombre_o_id(
    api: TestClient, criadero: Criadero, como_lo_nombra: str
) -> None:
    parque = str(criadero.parque) if como_lo_nombra == "id" else como_lo_nombra

    propuesta, _ = proponer(
        api, criadero, "proponer_nucleos", {"parque": parque, "cantidad": 5}
    )

    assert propuesta["accion"] == "crear_nucleos"
    assert propuesta["ruta"] == f"/api/v1/parques/{criadero.parque}/nucleos"
    assert propuesta["cuerpo"] == {"cantidad": 5}
    NucleosLoteCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta) == {
        "Parque": "Parque Norte",
        "Núcleos a crear": "5",
        "Ubicación": "Una fila nueva, posiciones 1 a 5",
        "Después del alta": "7 de 50 núcleos",
    }
    assert propuesta["roles_permitidos"] == PRODUCCION
    creado = confirmar(api, criadero, propuesta, "admin")
    assert creado.status_code == 201
    assert creado.json()["creados"] == 5


def test_nucleos_de_un_parque_que_no_existe_pide_proponer_el_parque(
    api: TestClient, criadero: Criadero
) -> None:
    mensaje = error_de(
        api, criadero, "proponer_nucleos", {"parque": "Casa Santi", "cantidad": 10}
    )

    assert 'El parque "Casa Santi" todavía no existe' in mensaje
    assert "proponer_parque" in mensaje


def test_nucleos_de_un_parque_dado_de_baja(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    ejecutar_sql(
        motor_owner,
        criadero.tenant,
        "INSERT INTO parque (nombre, ubicacion, capacidad_max, activo) "
        "VALUES ('Parque Viejo', 'Loma', 10, false)",
    )

    mensaje = error_de(
        api, criadero, "proponer_nucleos", {"parque": "Parque Viejo", "cantidad": 1}
    )

    assert "dado de baja" in mensaje


@pytest.mark.parametrize(
    ("entrada", "mensaje"),
    [
        (
            {"parque": "Parque Norte", "cantidad": 49},
            'En el parque "Parque Norte" entran 48 núcleos más',
        ),
        ({"parque": "Parque Norte"}, "Faltan datos obligatorios: cantidad"),
        ({"parque": "Parque Norte", "cantidad": 0}, "cantidad"),
        ({"parque": str(uuid.uuid4()), "cantidad": 1}, "No existe el parque."),
    ],
)
def test_nucleos_sin_lugar_o_sin_cantidad_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, entrada: dict[str, Any], mensaje: str
) -> None:
    assert mensaje in error_de(api, criadero, "proponer_nucleos", entrada)


# --- crear_banco ---


def test_banco_sin_datos_lo_numera_el_sistema(
    api: TestClient, criadero: Criadero
) -> None:
    propuesta, _ = proponer(api, criadero, "proponer_banco", {})

    assert propuesta["accion"] == "crear_banco"
    assert (propuesta["ruta"], propuesta["cuerpo"]) == ("/api/v1/bancos", {})
    BancoCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta) == {
        "Identificador": "B-02 (lo asigna el sistema al crearlo)",
        "Capacidad": "276 reinas (la estándar)",
    }
    assert propuesta["roles_permitidos"] == PRODUCCION
    creado = confirmar(api, criadero, propuesta, "produccion")
    assert creado.status_code == 201
    assert creado.json()["identificador"] == "B-02"
    assert creado.json()["capacidad_max"] == 276


# --- crear_raza ---


def test_raza_valida_y_confirmable(api: TestClient, criadero: Criadero) -> None:
    entrada = {"nombre": "Ligustica", "descripcion": "Italiana"}

    propuesta, _ = proponer(api, criadero, "proponer_raza", entrada)

    assert propuesta["accion"] == "crear_raza"
    assert (propuesta["ruta"], propuesta["cuerpo"]) == ("/api/v1/razas", entrada)
    RazaCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta) == {"Nombre": "Ligustica", "Descripción": "Italiana"}
    assert propuesta["roles_permitidos"] == PRODUCCION
    assert confirmar(api, criadero, propuesta, "produccion").status_code == 201


def test_raza_repetida_o_sin_nombre_vuelve_al_modelo(
    api: TestClient, criadero: Criadero
) -> None:
    assert 'Ya existe una raza "Buckfast"' in error_de(
        api, criadero, "proponer_raza", {"nombre": "BUCKFAST"}
    )
    assert "nombre de la raza" in error_de(api, criadero, "proponer_raza", {})


# --- crear_producto ---


@pytest.mark.parametrize(
    ("alicuota", "esperada", "texto"),
    [
        (None, "0.21", "21% (por defecto)"),
        ("0.105", "0.105", "10,5%"),
    ],
)
def test_producto_con_la_alicuota_por_defecto_o_la_que_dice_el_usuario(
    api: TestClient,
    criadero: Criadero,
    alicuota: str | None,
    esperada: str,
    texto: str,
) -> None:
    entrada = {"nombre": "Reina en celda"}
    if alicuota is not None:
        entrada["alicuota"] = alicuota

    propuesta, _ = proponer(api, criadero, "proponer_producto", entrada)

    assert propuesta["accion"] == "crear_producto"
    assert propuesta["ruta"] == "/api/v1/items-vendibles"
    assert propuesta["cuerpo"] == {"nombre": "Reina en celda", "alicuota": esperada}
    ItemVendibleCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta)["Alícuota de IVA"] == texto
    assert propuesta["roles_permitidos"] == PRODUCCION
    creado = confirmar(api, criadero, propuesta, "admin")
    assert creado.status_code == 201
    assert creado.json()["alicuota"] == esperada


def test_producto_invalido_o_repetido_vuelve_al_modelo(
    api: TestClient, criadero: Criadero
) -> None:
    assert "alicuota" in error_de(
        api, criadero, "proponer_producto", {"nombre": "X", "alicuota": "0.3"}
    )
    assert 'Ya existe un producto "Reina virgen"' in error_de(
        api, criadero, "proponer_producto", {"nombre": "reina virgen"}
    )


# --- crear_material ---


def test_material_valido_y_confirmable(api: TestClient, criadero: Criadero) -> None:
    entrada = {"nombre": "Jaulas de transporte", "unidad": "unidad"}

    propuesta, _ = proponer(api, criadero, "proponer_material", entrada)

    assert propuesta["accion"] == "crear_material"
    assert (propuesta["ruta"], propuesta["cuerpo"]) == ("/api/v1/materiales", entrada)
    MaterialCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta)["Stock inicial"] == "0: se suma con una compra."
    assert propuesta["roles_permitidos"] == PRODUCCION
    assert confirmar(api, criadero, propuesta, "produccion").status_code == 201


def test_material_repetido_vuelve_al_modelo(
    api: TestClient, criadero: Criadero
) -> None:
    mensaje = error_de(
        api, criadero, "proponer_material", {"nombre": "cupulas", "unidad": "u"}
    )

    assert 'Ya existe un material "Cúpulas"' in mensaje


# --- registrar_movimiento_material ---


def test_compra_resuelve_el_material_y_muestra_el_stock(
    api: TestClient, criadero: Criadero
) -> None:
    entrada = {"material": "cupulas", "tipo": "compra", "cantidad": 500}

    propuesta, _ = proponer(api, criadero, "proponer_movimiento_material", entrada)

    assert propuesta["accion"] == "registrar_movimiento_material"
    assert propuesta["ruta"] == f"/api/v1/materiales/{criadero.material}/movimientos"
    assert propuesta["cuerpo"] == {"tipo": "compra", "cantidad": "500"}
    TypeAdapter(MovimientoCrear).validate_python(propuesta["cuerpo"])
    assert resumen(propuesta) == {
        "Material": "Cúpulas",
        "Tipo": "compra",
        "Cantidad": "+500 unidad",
        "Fecha": "hoy",
        "Stock": "0 → 500 unidad",
    }
    assert propuesta["roles_permitidos"] == PRODUCCION
    creado = confirmar(api, criadero, propuesta, "produccion")
    assert creado.status_code == 201
    assert creado.json()["stock"] == "500.000"


def test_ajuste_negativo_avisa_el_stock_negativo(
    api: TestClient, criadero: Criadero
) -> None:
    entrada = {
        "material": "Cúpulas",
        "tipo": "ajuste",
        "cantidad": "-20",
        "motivo": "Rotas",
        "fecha": "2026-11-01",
    }

    propuesta, _ = proponer(api, criadero, "proponer_movimiento_material", entrada)

    datos = resumen(propuesta)
    assert datos["Motivo"] == "Rotas"
    assert datos["Fecha"] == "2026-11-01"
    assert datos["Stock"] == ("0 → -20 unidad (queda negativo: se permite, con aviso)")
    assert confirmar(api, criadero, propuesta, "admin").status_code == 201


@pytest.mark.parametrize(
    ("entrada", "mensaje"),
    [
        ({"material": "Cúpulas", "tipo": "ajuste", "cantidad": 5}, "motivo"),
        ({"material": "Cúpulas", "tipo": "compra", "cantidad": -5}, "positivo"),
        ({"material": "Cúpulas", "cantidad": 5}, "tipo (compra o ajuste)"),
        ({"material": "Cúpulas", "tipo": "consumo", "cantidad": 5}, "tipo"),
        (
            {"material": "Alzas", "tipo": "compra", "cantidad": 5},
            'No existe el material "Alzas"',
        ),
    ],
)
def test_movimiento_invalido_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, entrada: dict[str, Any], mensaje: str
) -> None:
    assert mensaje in error_de(api, criadero, "proponer_movimiento_material", entrada)


# --- cancelar_pedido ---


def test_cancelar_un_pedido_en_borrador_advierte_que_es_final(
    api: TestClient, criadero: Criadero
) -> None:
    propuesta, _ = proponer(
        api,
        criadero,
        "proponer_cancelacion_pedido",
        {"pedido_id": str(criadero.pedido)},
    )

    assert propuesta["accion"] == "cancelar_pedido"
    assert propuesta["ruta"] == f"/api/v1/pedidos/{criadero.pedido}/estado"
    assert propuesta["cuerpo"] == {"estado": "cancelado"}
    TypeAdapter(CambioDeEstado).validate_python(propuesta["cuerpo"])
    assert resumen(propuesta) == {
        "Pedido": "Apícola del Sur, entrega 2026-12-01",
        "Estado actual": "borrador",
        "Reinas del pedido": "5",
        "Atención": "Cancelar es final: el pedido no se puede reactivar ni "
        "editar después.",
    }
    assert propuesta["roles_permitidos"] == ["admin", "ventas"]
    cancelado = confirmar(api, criadero, propuesta, "ventas")
    assert cancelado.status_code == 200
    assert cancelado.json()["estado"] == "cancelado"


def test_cancelar_un_pedido_reservado_avisa_que_libera_las_reservas(
    api: TestClient, criadero: Criadero
) -> None:
    reserva = api.post(
        f"/api/v1/pedidos/{criadero.pedido}/reservas",
        headers=como(criadero.tenant, "ventas"),
    )
    assert reserva.status_code == 201, reserva.json()

    propuesta, _ = proponer(
        api, criadero, "proponer_cancelacion_pedido", {"cliente": "apicola del sur"}
    )

    assert propuesta["ruta"] == f"/api/v1/pedidos/{criadero.pedido}/estado"
    assert resumen(propuesta)["Reservas"].startswith("Se liberan")


def test_cancelar_resuelve_el_pedido_por_cliente_o_pregunta(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    propuesta, _ = proponer(
        api, criadero, "proponer_cancelacion_pedido", {"cliente": "Apícola del Sur"}
    )
    assert propuesta["ruta"] == f"/api/v1/pedidos/{criadero.pedido}/estado"

    ejecutar_sql(
        motor_owner,
        criadero.tenant,
        "WITH nuevo AS (INSERT INTO pedido (cliente_id, moneda, "
        "fecha_entrega_solicitada) VALUES (:cliente, 'ARS', '2026-12-20') "
        "RETURNING id) INSERT INTO pedido_item (pedido_id, numero_linea, "
        "item_vendible_id, raza_id, cantidad, precio_unitario, alicuota) "
        "SELECT id, 1, :item, :raza, 2, 30000.00, 0.21 FROM nuevo",
        cliente=criadero.apicola_del_sur,
        item=criadero.reina_fecundada,
        raza=criadero.buckfast,
    )
    mensaje = error_de(
        api, criadero, "proponer_cancelacion_pedido", {"cliente": "Apícola del Sur"}
    )
    assert "tiene 2 pedidos que se pueden cancelar" in mensaje
    assert "Preguntale al usuario cuál" in mensaje


@pytest.mark.parametrize(
    ("entrada", "mensaje"),
    [
        ({}, "Faltan datos obligatorios: el pedido (o el cliente del pedido)"),
        ({"cliente": "Bienenhof"}, "no tiene pedidos en borrador ni reservados"),
        ({"pedido_id": str(uuid.uuid4())}, "No existe el pedido"),
    ],
)
def test_cancelar_sin_pedido_identificable_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, entrada: dict[str, Any], mensaje: str
) -> None:
    assert mensaje in error_de(api, criadero, "proponer_cancelacion_pedido", entrada)


@pytest.mark.parametrize("estado", ["cancelado", "entregado"])
def test_un_pedido_final_no_se_propone_cancelar(
    api: TestClient, criadero: Criadero, motor_owner: Engine, estado: str
) -> None:
    ejecutar_sql(
        motor_owner,
        criadero.tenant,
        "UPDATE pedido SET estado = :estado WHERE id = :id",
        estado=estado,
        id=criadero.pedido,
    )

    mensaje = error_de(
        api,
        criadero,
        "proponer_cancelacion_pedido",
        {"pedido_id": str(criadero.pedido)},
    )

    assert mensaje == (
        f"El pedido está {estado}: solo se cancela uno en borrador o reservado."
    )


# --- nada se escribe sin confirmar ---


def test_ninguna_propuesta_nueva_toca_la_base(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    antes = foto(motor_owner, criadero.tenant)
    usos: list[tuple[str, dict[str, Any], str]] = [
        (
            "proponer_parque",
            {"nombre": "Casa Santi", "ubicacion": "Grand Bell", "capacidad_max": 100},
            "1",
        ),
        ("proponer_nucleos", {"parque": "Parque Norte", "cantidad": 3}, "2"),
        ("proponer_banco", {}, "3"),
        ("proponer_raza", {"nombre": "Ligustica"}, "4"),
        ("proponer_producto", {"nombre": "Reina en celda"}, "5"),
        ("proponer_material", {"nombre": "Jaulas", "unidad": "unidad"}, "6"),
        (
            "proponer_movimiento_material",
            {"material": "Cúpulas", "tipo": "compra", "cantidad": 100},
            "7",
        ),
        ("proponer_cancelacion_pedido", {"pedido_id": str(criadero.pedido)}, "8"),
    ]
    modelo = ModeloGuionado([pide_varias(usos), responde("Ocho propuestas.")])

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 200
    assert [r["status"] for r in modelo.resultados(1)] == ["success"] * 8
    assert [p["accion"] for p in respuesta.json()["propuestas"]] == [
        "crear_parque",
        "crear_nucleos",
        "crear_banco",
        "crear_raza",
        "crear_producto",
        "crear_material",
        "registrar_movimiento_material",
        "cancelar_pedido",
    ]
    assert foto(motor_owner, criadero.tenant) == antes


# --- el rol resultado y el flujo encadenado ---


def test_un_resultado_como_ultimo_mensaje_llega_delimitado(
    api: TestClient, criadero: Criadero
) -> None:
    modelo = ModeloGuionado([responde("¿Querés cambiar algo del pedido?")])
    mensajes = [
        {"rol": "usuario", "texto": "Armá un pedido"},
        {"rol": "asistente", "texto": "Te dejé la propuesta."},
        {"rol": "resultado", "texto": "El usuario descartó la propuesta crear_pedido"},
    ]

    respuesta = conversar(api, criadero, modelo, mensajes=mensajes)

    assert respuesta.status_code == 200
    ultimo = modelo.pedidos[0]["messages"][-1]
    assert ultimo == {
        "role": "user",
        "content": [
            {
                "text": f"{ENCABEZADO_RESULTADO}\n<datos>\nEl usuario descartó "
                "la propuesta crear_pedido\n</datos>"
            }
        ],
    }


def test_un_resultado_demasiado_largo_es_422(
    api: TestClient, criadero: Criadero
) -> None:
    modelo = ModeloGuionado([])
    mensajes = [
        {"rol": "usuario", "texto": "Hola"},
        {"rol": "resultado", "texto": "x" * 2001},
    ]

    respuesta = conversar(api, criadero, modelo, mensajes=mensajes)

    assert respuesta.status_code == 422
    assert modelo.pedidos == []


def test_parque_resultado_y_nucleos_encadenados(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    pedido = (
        "Creá un parque Casa Santi, ubicación Grand Bell, capacidad 100, con 10 núcleos"
    )
    # 1. El asistente propone solo el parque.
    primero = ModeloGuionado(
        [
            pide(
                "proponer_parque",
                {
                    "nombre": "Casa Santi",
                    "ubicacion": "Grand Bell",
                    "capacidad_max": 100,
                },
            ),
            responde(
                "Te propuse el parque; cuando lo confirmes, sigo con los núcleos."
            ),
        ]
    )
    respuesta = conversar(
        api, criadero, primero, mensajes=[{"rol": "usuario", "texto": pedido}]
    )
    assert respuesta.status_code == 200
    (parque,) = respuesta.json()["propuestas"]
    assert parque["accion"] == "crear_parque"

    # 2. El usuario confirma: el front llama al endpoint con su token.
    creado = confirmar(api, criadero, parque, "produccion")
    assert creado.status_code == 201
    parque_id = creado.json()["id"]

    # 3. El front manda el resultado y "Seguí": el modelo propone los núcleos.
    resultado = (
        f"Confirmado crear_parque: se creó el parque Casa Santi (id {parque_id})"
    )
    segundo = ModeloGuionado(
        [
            pide("proponer_nucleos", {"parque": parque_id, "cantidad": 10}),
            responde("Te propuse los 10 núcleos de Casa Santi."),
        ]
    )
    respuesta = conversar(
        api,
        criadero,
        segundo,
        mensajes=[
            {"rol": "usuario", "texto": pedido},
            {"rol": "asistente", "texto": respuesta.json()["texto"]},
            {"rol": "resultado", "texto": resultado},
            {"rol": "usuario", "texto": "Seguí con el siguiente paso"},
        ],
    )
    assert respuesta.status_code == 200
    turno = segundo.pedidos[0]["messages"][-1]["content"][0]["text"]
    assert f"<datos>\n{resultado}\n</datos>" in turno
    assert turno.endswith("Seguí con el siguiente paso")
    (nucleos,) = respuesta.json()["propuestas"]
    assert nucleos["ruta"] == f"/api/v1/parques/{parque_id}/nucleos"
    assert resumen(nucleos)["Parque"] == "Casa Santi"
    assert datos_de(segundo.resultados(1)[0])["accion"] == "crear_nucleos"

    # 4. Confirmados los núcleos, el parque queda con 10.
    assert confirmar(api, criadero, nucleos, "produccion").status_code == 201
    activos = ejecutar_sql(
        motor_owner,
        criadero.tenant,
        "SELECT count(*) FROM nucleo WHERE parque_id = :id AND activo",
        id=parque_id,
    )
    assert activos == 10
