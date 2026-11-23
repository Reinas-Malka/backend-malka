"""Planes de varios pasos en un solo turno.

El asistente propone todo lo que pidió el usuario, aunque dependa de algo
que todavía no existe: el paso dependiente lleva el marcador
{{ref:<id de la propuesta>.id}} y depende_de. Acá se ejecuta cada plan como
lo hace el front (en orden, reemplazando el marcador con el id que devolvió
el paso anterior y con Idempotency-Key = id de la propuesta) y se comprueba
que hasta entonces la base no cambió.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

from app.main import app
from app.routers.borradores import obtener_cola
from app.schemas.asistente import Propuesta
from app.schemas.borradores import BorradorPedido
from app.services.ia.asistente.plan import MARCADOR, MAX_PROPUESTAS, marcador
from tests.asistente_datos import (
    Criadero,
    ModeloGuionado,
    cargar_criadero,
    como,
    conversar,
    datos_de,
    limpiar,
    pide,
    pide_varias,
    responde,
    resumen,
)
from tests.conftest import fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

Paso = dict[str, Any] | Callable[[list[dict[str, Any]]], dict[str, Any]]


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


class ColaFalsa:
    def __init__(self) -> None:
        self.mensajes: list[dict[str, Any]] = []

    def enviar(self, cuerpo: dict[str, Any]) -> None:
        self.mensajes.append(cuerpo)


@pytest.fixture
def cola() -> Iterator[ColaFalsa]:
    falsa = ColaFalsa()
    app.dependency_overrides[obtener_cola] = lambda: falsa
    yield falsa
    app.dependency_overrides.pop(obtener_cola, None)


class ModeloQueLee(ModeloGuionado):
    """Como ModeloGuionado, pero un paso del guion puede ser una función que
    arma la respuesta con los datos de la ronda anterior (el propuesta_id
    que devolvió proponer_pedido, como haría el modelo)."""

    def __init__(self, guion: list[Paso]) -> None:
        super().__init__([])
        self.pasos = list(guion)

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.pedidos.append(json.loads(json.dumps(kwargs, default=str)))
        paso = self.pasos.pop(0)
        if callable(paso):
            ultimo = kwargs["messages"][-1]["content"]
            return paso([datos_de(b["toolResult"]) for b in ultimo])
        return paso


def plan_de(
    api: TestClient, criadero: Criadero, guion: list[Paso], rol: str = "admin"
) -> list[dict[str, Any]]:
    respuesta = conversar(api, criadero, ModeloQueLee(guion), rol=rol)
    assert respuesta.status_code == 200, respuesta.json()
    propuestas: list[dict[str, Any]] = respuesta.json()["propuestas"]
    for propuesta in propuestas:
        Propuesta.model_validate(propuesta)
    return propuestas


def reemplazar(valor: Any, ids: dict[str, str]) -> Any:
    """Lo que hace el front: cada marcador por el id del paso del que depende."""
    texto = json.dumps(valor)
    for coincidencia in set(MARCADOR.findall(texto)):
        texto = texto.replace(marcador(uuid.UUID(coincidencia)), ids[coincidencia])
    return json.loads(texto)


def ejecutar_plan(
    api: TestClient, criadero: Criadero, propuestas: list[dict[str, Any]], rol: str
) -> list[Response]:
    ids: dict[str, str] = {}
    respuestas = []
    for propuesta in sorted(propuestas, key=lambda p: p["paso"]):
        ruta = reemplazar(propuesta["ruta"], ids)
        cuerpo = reemplazar(propuesta["cuerpo"], ids)
        assert MARCADOR.search(json.dumps([ruta, cuerpo])) is None
        headers = {**como(criadero.tenant, rol), "Idempotency-Key": propuesta["id"]}
        respuesta = (
            api.post(ruta, headers=headers)
            if cuerpo is None
            else api.post(ruta, json=cuerpo, headers=headers)
        )
        assert respuesta.status_code in (200, 201, 202), respuesta.json()
        respuestas.append(respuesta)
        cuerpo_respuesta = respuesta.json()
        if isinstance(cuerpo_respuesta, dict) and "id" in cuerpo_respuesta:
            ids[propuesta["id"]] = str(cuerpo_respuesta["id"])
    return respuestas


def foto(motor: Engine, tenant: uuid.UUID) -> dict[str, int]:
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        return {
            tabla: c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar_one()  # noqa: S608
            for tabla in (
                "parque",
                "nucleo",
                "raza",
                "material",
                "movimiento_material",
                "cliente",
                "pedido",
                "reserva",
                "borrador",
            )
        }


def contar(motor: Engine, tenant: uuid.UUID, sql: str, **params: Any) -> int:
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        return int(c.execute(text(sql), params).scalar_one())


PARQUE = {"nombre": "Casa Santi", "ubicacion": "Grand Bell", "capacidad_max": 100}
CLIENTE_ALEMAN = {
    "nombre": "Imkerei Sonne",
    "pais": "DE",
    "tipo": "exportacion",
    "condicion_iva": "cliente_exterior",
    "cuit_o_tax_id": "DE987654321",
}


def _pedido(cliente: str, **extra: Any) -> dict[str, Any]:
    return {
        "cliente": cliente,
        "fecha_entrega_solicitada": "2026-12-15",
        "items": [
            {
                "producto": "Reina fecundada",
                "raza": "Buckfast",
                "cantidad": 10,
                "precio_unitario": 40,
            }
        ],
        **extra,
    }


def _con_el_pedido(herramienta: str) -> Callable[[list[dict[str, Any]]], Any]:
    """La ronda siguiente: usa el propuesta_id del crear_pedido anterior."""

    def armar(datos: list[dict[str, Any]]) -> dict[str, Any]:
        pedido = next(d for d in datos if d.get("accion") == "crear_pedido")
        return pide(herramienta, {"pedido_id": pedido["propuesta_id"]}, "r2")

    return armar


# --- planes con dependencias ---


def test_parque_y_sus_nucleos_en_un_turno(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    antes = foto(motor_owner, criadero.tenant)
    modelo = ModeloQueLee(
        [
            pide_varias(
                [
                    ("proponer_parque", PARQUE, "1"),
                    ("proponer_nucleos", {"parque": "casa santi", "cantidad": 10}, "2"),
                ]
            ),
            responde("Te armé un plan de dos pasos: el parque y sus 10 núcleos."),
        ]
    )

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 200
    parque, nucleos = respuesta.json()["propuestas"]
    assert (parque["paso"], parque["depende_de"]) == (1, [])
    assert nucleos["paso"] == 2
    assert nucleos["depende_de"] == [parque["id"]]
    assert nucleos["ruta"] == f"/api/v1/parques/{{{{ref:{parque['id']}.id}}}}/nucleos"
    assert nucleos["cuerpo"] == {"cantidad": 10}
    assert resumen(nucleos)["Parque"] == "Casa Santi"
    assert resumen(nucleos)["Después del alta"] == "10 de 100 núcleos"
    (_, visto) = (datos_de(r) for r in modelo.resultados(1))
    assert visto["depende_de_los_pasos"] == [1]
    assert foto(motor_owner, criadero.tenant) == antes

    _, creados = ejecutar_plan(api, criadero, [parque, nucleos], "produccion")

    assert creados.json()["creados"] == 10
    assert (
        contar(
            motor_owner,
            criadero.tenant,
            "SELECT count(*) FROM nucleo n JOIN parque p ON p.id = n.parque_id "
            "WHERE p.nombre = 'Casa Santi'",
        )
        == 10
    )


def test_nucleos_de_mas_para_un_parque_propuesto(
    api: TestClient, criadero: Criadero
) -> None:
    modelo = ModeloQueLee(
        [
            pide_varias(
                [
                    ("proponer_parque", {**PARQUE, "capacidad_max": 5}, "1"),
                    ("proponer_nucleos", {"parque": "Casa Santi", "cantidad": 10}, "2"),
                ]
            ),
            responde("Listo."),
        ]
    )

    respuesta = conversar(api, criadero, modelo)

    _, error = modelo.resultados(1)
    assert error["status"] == "error"
    assert "entran 5 núcleos más" in datos_de(error)["error"]
    assert [p["accion"] for p in respuesta.json()["propuestas"]] == ["crear_parque"]


def test_cliente_pedido_y_reserva_en_un_turno(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    antes = foto(motor_owner, criadero.tenant)

    propuestas = plan_de(
        api,
        criadero,
        [
            pide_varias(
                [
                    ("proponer_cliente", CLIENTE_ALEMAN, "1"),
                    (
                        "proponer_pedido",
                        _pedido("Imkerei Sonne", moneda="EUR", destino_pais="DE"),
                        "2",
                    ),
                ]
            ),
            _con_el_pedido("proponer_reserva"),
            responde("Plan de tres pasos: cliente, pedido y reserva."),
        ],
    )

    cliente, pedido, reserva = propuestas
    assert [p["paso"] for p in propuestas] == [1, 2, 3]
    assert pedido["depende_de"] == [cliente["id"]]
    assert pedido["cuerpo"]["cliente_id"] == marcador(uuid.UUID(cliente["id"]))
    assert pedido["cuerpo"]["moneda"] == "EUR"
    assert resumen(pedido)["Cliente"] == "Imkerei Sonne"
    assert reserva["depende_de"] == [pedido["id"]]
    assert reserva["ruta"] == f"/api/v1/pedidos/{{{{ref:{pedido['id']}.id}}}}/reservas"
    assert reserva["cuerpo"] is None
    assert resumen(reserva)["Pedido"] == (
        "Imkerei Sonne, entrega 2026-12-15 (el del paso 2)"
    )
    assert resumen(reserva)["Reinas Buckfast"] == "10 (disponibles ahora: 50)"
    assert foto(motor_owner, criadero.tenant) == antes

    ejecutar_plan(api, criadero, propuestas, "ventas")

    assert (
        contar(
            motor_owner,
            criadero.tenant,
            "SELECT count(*) FROM pedido p JOIN cliente c ON c.id = p.cliente_id "
            "WHERE c.nombre = 'Imkerei Sonne' AND p.estado = 'reservado'",
        )
        == 1
    )


def test_material_y_su_ingreso_en_un_turno(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    propuestas = plan_de(
        api,
        criadero,
        [
            pide_varias(
                [
                    (
                        "proponer_material",
                        {"nombre": "Jaulas", "unidad": "unidad"},
                        "1",
                    ),
                    (
                        "proponer_movimiento_material",
                        {"material": "jaulas", "tipo": "compra", "cantidad": 500},
                        "2",
                    ),
                ]
            ),
            responde("Listo."),
        ],
    )

    material, ingreso = propuestas
    assert ingreso["depende_de"] == [material["id"]]
    assert ingreso["ruta"] == (
        f"/api/v1/materiales/{{{{ref:{material['id']}.id}}}}/movimientos"
    )
    assert resumen(ingreso)["Stock"] == "0 → 500 unidad"

    _, guardado = ejecutar_plan(api, criadero, propuestas, "produccion")

    assert guardado.json()["stock"] == "500.000"


@pytest.mark.usefixtures("cola")
def test_pedido_y_su_borrador_en_un_turno(api: TestClient, criadero: Criadero) -> None:
    propuestas = plan_de(
        api,
        criadero,
        [
            pide("proponer_pedido", _pedido("Apícola del Sur")),
            _con_el_pedido("proponer_borrador"),
            responde("Listo."),
        ],
    )

    pedido, borrador = propuestas
    referencia = marcador(uuid.UUID(pedido["id"]))
    assert borrador["cuerpo"] == {"pedido_id": referencia}
    assert borrador["depende_de"] == [pedido["id"]]
    BorradorPedido.model_validate({"pedido_id": str(uuid.uuid4())})

    _, pedido_el_borrador = ejecutar_plan(api, criadero, propuestas, "ventas")

    assert pedido_el_borrador.status_code == 202


def test_pedido_con_producto_y_raza_propuestos_en_el_turno(
    api: TestClient, criadero: Criadero
) -> None:
    propuestas = plan_de(
        api,
        criadero,
        [
            pide_varias(
                [
                    ("proponer_raza", {"nombre": "Ligustica"}, "1"),
                    ("proponer_producto", {"nombre": "Reina virgen premium"}, "2"),
                    (
                        "proponer_pedido",
                        {
                            "cliente": "Apícola del Sur",
                            "fecha_entrega_solicitada": "2026-12-15",
                            "items": [
                                {
                                    "producto": "Reina virgen premium",
                                    "raza": "Ligustica",
                                    "cantidad": 3,
                                    "precio_unitario": 10,
                                }
                            ],
                        },
                        "3",
                    ),
                ]
            ),
            responde("Listo."),
        ],
    )

    raza, producto, pedido = propuestas
    (linea,) = pedido["cuerpo"]["items"]
    assert linea["raza_id"] == marcador(uuid.UUID(raza["id"]))
    assert linea["item_vendible_id"] == marcador(uuid.UUID(producto["id"]))
    assert pedido["depende_de"] == [producto["id"], raza["id"]]

    ejecutar_plan(api, criadero, propuestas, "admin")


# --- independientes, repetidos, ambiguos y el tope ---


def test_dos_razas_independientes(api: TestClient, criadero: Criadero) -> None:
    propuestas = plan_de(
        api,
        criadero,
        [
            pide_varias(
                [
                    ("proponer_raza", {"nombre": "Ligústica"}, "1"),
                    ("proponer_raza", {"nombre": "Caucásica"}, "2"),
                ]
            ),
            responde("Dos razas para confirmar."),
        ],
    )

    assert [(p["paso"], p["depende_de"]) for p in propuestas] == [(1, []), (2, [])]
    ejecutar_plan(api, criadero, propuestas, "produccion")


def test_la_misma_raza_dos_veces_en_un_turno(
    api: TestClient, criadero: Criadero
) -> None:
    modelo = ModeloQueLee(
        [
            pide_varias(
                [
                    ("proponer_raza", {"nombre": "Ligustica"}, "1"),
                    ("proponer_raza", {"nombre": "LIGÚSTICA"}, "2"),
                ]
            ),
            responde("Listo."),
        ]
    )

    respuesta = conversar(api, criadero, modelo)

    _, repetida = modelo.resultados(1)
    assert datos_de(repetida)["error"] == (
        'Ya propusiste una raza "Ligustica" en este turno: no lo repitas.'
    )
    assert len(respuesta.json()["propuestas"]) == 1


def test_un_nombre_ambiguo_entre_propuestas_vuelve_al_modelo(
    api: TestClient, criadero: Criadero
) -> None:
    modelo = ModeloQueLee(
        [
            pide_varias(
                [
                    ("proponer_parque", {**PARQUE, "nombre": "Casa Santi Norte"}, "1"),
                    ("proponer_parque", {**PARQUE, "nombre": "Casa Santi Sur"}, "2"),
                    ("proponer_nucleos", {"parque": "Casa Santi", "cantidad": 2}, "3"),
                ]
            ),
            responde("¿Cuál de los dos?"),
        ]
    )

    conversar(api, criadero, modelo)

    error = datos_de(modelo.resultados(1)[2])["error"]
    assert '"Casa Santi" es ambiguo' in error
    assert '"Casa Santi Norte" (propuesto en este turno)' in error


def test_la_base_gana_sobre_lo_propuesto(api: TestClient, criadero: Criadero) -> None:
    propuestas = plan_de(
        api,
        criadero,
        [
            pide_varias(
                [
                    ("proponer_parque", {**PARQUE, "nombre": "Parque Norte II"}, "1"),
                    (
                        "proponer_nucleos",
                        {"parque": "Parque Norte", "cantidad": 2},
                        "2",
                    ),
                ]
            ),
            responde("Listo."),
        ],
    )

    _, nucleos = propuestas
    assert nucleos["ruta"] == f"/api/v1/parques/{criadero.parque}/nucleos"
    assert nucleos["depende_de"] == []


def test_tope_de_propuestas_por_respuesta(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    antes = foto(motor_owner, criadero.tenant)
    usos = [
        ("proponer_raza", {"nombre": f"Raza {n}"}, str(n))
        for n in range(MAX_PROPUESTAS + 1)
    ]
    modelo = ModeloQueLee([pide_varias(usos), responde("Diez por ahora.")])

    respuesta = conversar(api, criadero, modelo)

    resultados = modelo.resultados(1)
    assert [r["status"] for r in resultados] == ["success"] * MAX_PROPUESTAS + ["error"]
    assert "Ya hay 10 propuestas en este turno" in datos_de(resultados[-1])["error"]
    assert len(respuesta.json()["propuestas"]) == MAX_PROPUESTAS
    assert foto(motor_owner, criadero.tenant) == antes


def test_un_plan_completo_no_escribe_nada(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    antes = foto(motor_owner, criadero.tenant)

    propuestas = plan_de(
        api,
        criadero,
        [
            pide_varias(
                [
                    ("proponer_parque", PARQUE, "1"),
                    ("proponer_nucleos", {"parque": "Casa Santi", "cantidad": 3}, "2"),
                    ("proponer_material", {"nombre": "Jaulas", "unidad": "u"}, "3"),
                    (
                        "proponer_movimiento_material",
                        {"material": "Jaulas", "tipo": "compra", "cantidad": 5},
                        "4",
                    ),
                    ("proponer_cliente", CLIENTE_ALEMAN, "5"),
                    (
                        "proponer_pedido",
                        _pedido("Imkerei Sonne", moneda="EUR", destino_pais="DE"),
                        "6",
                    ),
                ]
            ),
            _con_el_pedido("proponer_reserva"),
            responde("Siete pasos."),
        ],
    )

    assert len(propuestas) == 7
    assert foto(motor_owner, criadero.tenant) == antes
