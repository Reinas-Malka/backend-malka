"""Propuestas del asistente: arman la escritura, no la ejecutan.

Cada propuesta trae el cuerpo exacto del endpoint, ya validado contra su
schema, y un resumen armado por el servidor. La prueba de fuego: confirmarla
con el endpoint real (Idempotency-Key = id de la propuesta) responde 201 o
202. Y hasta que alguien confirma, la base no cambia.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import TypeAdapter
from sqlalchemy import Engine, text

from app.fechas import hoy
from app.main import app
from app.routers.borradores import BorradorPedido, obtener_cola
from app.schemas.asistente import Propuesta
from app.schemas.clientes import ClienteCrear
from app.schemas.pedidos import PedidoCrear
from app.schemas.produccion import OperacionCrear, TandaCrear
from app.services.ventas.clientes import es_cuit_valido
from tests.asistente_datos import (
    FECHA_TANDA,
    Criadero,
    ModeloGuionado,
    cargar_criadero,
    como,
    conversar,
    datos_de,
    error_de,
    limpiar,
    pide,
    pide_varias,
    responde,
    usar,
)
from tests.conftest import fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]


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


def proponer(
    api: TestClient,
    criadero: Criadero,
    nombre: str,
    entrada: dict[str, Any],
    rol: str = "admin",
) -> tuple[dict[str, Any], Any]:
    """La propuesta que devolvio la herramienta y lo que vio el modelo."""
    cuerpo, resultado = usar(api, criadero, nombre, entrada, rol=rol)
    assert resultado["status"] == "success", datos_de(resultado)
    (propuesta,) = cuerpo["propuestas"]
    Propuesta.model_validate(propuesta)
    assert cuerpo["consultas"] == []
    return propuesta, datos_de(resultado)


def confirmar(
    api: TestClient, criadero: Criadero, propuesta: dict[str, Any], rol: str
) -> Response:
    """Lo que hace el front: el endpoint de la propuesta, con su token."""
    headers = {**como(criadero.tenant, rol), "Idempotency-Key": propuesta["id"]}
    if propuesta["cuerpo"] is None:
        return api.request(propuesta["metodo"], propuesta["ruta"], headers=headers)
    return api.request(
        propuesta["metodo"],
        propuesta["ruta"],
        json=propuesta["cuerpo"],
        headers=headers,
    )


def resumen(propuesta: dict[str, Any]) -> dict[str, str]:
    return {par["etiqueta"]: par["valor"] for par in propuesta["resumen"]}


def dia_habil_pasado() -> date:
    dia = hoy() - timedelta(days=1)
    while dia.weekday() == 6:
        dia -= timedelta(days=1)
    return dia


def domingo_pasado() -> date:
    dia = hoy() - timedelta(days=1)
    while dia.weekday() != 6:
        dia -= timedelta(days=1)
    return dia


def cuit_nuevo() -> str:
    for base in range(2033333333, 2033333433):
        for digito in range(10):
            cuit = f"{base}{digito}"
            if es_cuit_valido(cuit):
                return cuit
    raise AssertionError("sin CUIT valido")


def foto(motor: Engine, tenant: uuid.UUID) -> dict[str, Any]:
    """Todo lo que una escritura podria cambiar."""
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        conteos = {
            tabla: c.execute(text(f"SELECT count(*) FROM {tabla}")).scalar_one()  # noqa: S608
            for tabla in (
                "cliente",
                "pedido",
                "pedido_item",
                "reserva",
                "tanda",
                "movimiento_material",
                "borrador",
            )
        }
        celdas = c.execute(
            text("SELECT estado, count(*) FROM celda GROUP BY estado ORDER BY estado")
        ).all()
        estados = c.execute(text("SELECT estado FROM pedido ORDER BY id")).all()
    return {**conteos, "celdas": celdas, "pedidos": estados}


# --- crear_cliente ---


def test_propuesta_de_cliente_valida_y_confirmable(
    api: TestClient, criadero: Criadero
) -> None:
    cuit = cuit_nuevo()
    guion = f"{cuit[:2]}-{cuit[2:10]}-{cuit[10]}"
    entrada = {
        "nombre": "Miel Pampa",
        "pais": "ar",
        "tipo": "nacional",
        "condicion_iva": "monotributo",
        "cuit_o_tax_id": guion,
    }

    propuesta, visto = proponer(api, criadero, "proponer_cliente", entrada)

    assert propuesta["accion"] == "crear_cliente"
    assert (propuesta["metodo"], propuesta["ruta"]) == ("POST", "/api/v1/clientes")
    # Normalizado como lo guarda el endpoint: pais en mayusculas, CUIT sin guiones.
    assert propuesta["cuerpo"] == {**entrada, "pais": "AR", "cuit_o_tax_id": cuit}
    ClienteCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta)["Nombre"] == "Miel Pampa"
    assert propuesta["roles_permitidos"] == ["admin", "ventas"]
    assert visto["el_usuario_puede_confirmarla"] is True
    assert "No se ejecutó nada" in visto["nota"]
    assert confirmar(api, criadero, propuesta, "ventas").status_code == 201


def test_cliente_invalido_vuelve_al_modelo(api: TestClient, criadero: Criadero) -> None:
    mensaje = error_de(
        api,
        criadero,
        "proponer_cliente",
        {
            "nombre": "Miel Pampa",
            "pais": "AR",
            "tipo": "nacional",
            "condicion_iva": "monotributo",
            "cuit_o_tax_id": "20123456780",
        },
    )

    assert "El CUIT no es valido" in mensaje
    assert "20123456780" not in mensaje


# --- crear_pedido ---


def test_pedido_nacional_resuelve_nombres_y_va_en_pesos(
    api: TestClient, criadero: Criadero
) -> None:
    propuesta, _ = proponer(
        api,
        criadero,
        "proponer_pedido",
        {
            "cliente": "apicola del sur",
            "fecha_entrega_solicitada": "2026-12-15",
            "items": [
                {
                    "producto": "reina fecundada",
                    "raza": "Buckfast",
                    "cantidad": 20,
                    "precio_unitario": 35000,
                }
            ],
        },
        rol="ventas",
    )

    assert propuesta["ruta"] == "/api/v1/pedidos"
    assert propuesta["cuerpo"] == {
        "cliente_id": str(criadero.apicola_del_sur),
        "moneda": "ARS",
        "fecha_entrega_solicitada": "2026-12-15",
        "items": [
            {
                "item_vendible_id": str(criadero.reina_fecundada),
                "raza_id": str(criadero.buckfast),
                "cantidad": 20,
                "precio_unitario": "35000",
            }
        ],
    }
    PedidoCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta) == {
        "Cliente": "Apícola del Sur",
        "Moneda": "ARS",
        "Entrega solicitada": "2026-12-15",
        "Línea 1": "20 reinas Buckfast (Reina fecundada) a 35000 ARS c/u",
    }
    assert confirmar(api, criadero, propuesta, "ventas").status_code == 201


def test_pedido_de_exportacion_pide_la_moneda(
    api: TestClient, criadero: Criadero
) -> None:
    linea = {
        "producto": "Reina fecundada",
        "raza": "Carniola",
        "cantidad": 30,
        "precio_unitario": "42.50",
    }
    base = {
        "cliente": "Bienenhof",
        "fecha_entrega_solicitada": "2026-12-15",
        "items": [linea],
    }

    sin_moneda = error_de(api, criadero, "proponer_pedido", base)
    propuesta, _ = proponer(
        api,
        criadero,
        "proponer_pedido",
        {**base, "moneda": "EUR", "destino_pais": "DE", "incoterm": "CPT"},
    )

    assert "USD o en EUR" in sin_moneda
    assert propuesta["cuerpo"]["moneda"] == "EUR"
    assert propuesta["cuerpo"]["destino_pais"] == "DE"
    assert resumen(propuesta)["Incoterm"] == "CPT"
    assert confirmar(api, criadero, propuesta, "admin").status_code == 201


@pytest.mark.parametrize(
    ("cambios", "esperado"),
    [
        pytest.param({"destino_pais": "UY"}, "no lleva destino_pais", id="destino"),
        pytest.param({"moneda": "USD"}, "va en ARS", id="moneda"),
        pytest.param({"cliente": "Desconocido SRL"}, "No existe el cliente", id="cli"),
        pytest.param(
            {"items": [{"producto": "Núcleo", "raza": "Buckfast", "cantidad": 1}]},
            "precio_unitario",
            id="sin_precio",
        ),
        pytest.param(
            {
                "items": [
                    {
                        "producto": "Núcleo",
                        "raza": "Buckfast",
                        "cantidad": 1,
                        "precio_unitario": 1,
                    }
                ]
            },
            'No existe el producto "Núcleo"',
            id="producto",
        ),
        pytest.param(
            {
                "items": [
                    {
                        "producto": "Reina fecundada",
                        "raza": "Buckfast",
                        "cantidad": 0,
                        "precio_unitario": 1,
                    }
                ]
            },
            "items.0.cantidad",
            id="cantidad",
        ),
    ],
)
def test_pedido_incoherente_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, cambios: dict[str, Any], esperado: str
) -> None:
    entrada = {
        "cliente": "Apícola del Sur",
        "fecha_entrega_solicitada": "2026-12-15",
        "items": [
            {
                "producto": "Reina fecundada",
                "raza": "Buckfast",
                "cantidad": 5,
                "precio_unitario": 100,
            }
        ],
        **cambios,
    }

    assert esperado in error_de(api, criadero, "proponer_pedido", entrada)


# --- reservar_pedido ---


def test_reserva_sin_cuerpo_con_la_disponibilidad_en_el_resumen(
    api: TestClient, criadero: Criadero
) -> None:
    propuesta, _ = proponer(
        api, criadero, "proponer_reserva", {"pedido_id": str(criadero.pedido)}
    )

    assert propuesta["ruta"] == f"/api/v1/pedidos/{criadero.pedido}/reservas"
    assert propuesta["cuerpo"] is None
    assert resumen(propuesta) == {
        "Pedido": "Apícola del Sur, entrega 2026-12-01",
        "Reinas Buckfast": "5 (disponibles ahora: 50)",
    }
    assert confirmar(api, criadero, propuesta, "ventas").status_code == 201
    # Ya reservado: una segunda propuesta no tiene sentido.
    mensaje = error_de(
        api, criadero, "proponer_reserva", {"pedido_id": str(criadero.pedido)}
    )
    assert "está reservado" in mensaje


def test_reserva_sin_disponibilidad_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        c.execute(
            text("UPDATE pedido_item SET cantidad = 80 WHERE pedido_id = :p"),
            {"p": criadero.pedido},
        )

    mensaje = error_de(
        api, criadero, "proponer_reserva", {"pedido_id": str(criadero.pedido)}
    )

    assert "Buckfast: pide 80 y hay 50 disponibles" in mensaje


# --- crear_tanda ---


def test_tanda_con_el_unico_material_y_celdas_calculadas(
    api: TestClient, criadero: Criadero
) -> None:
    fecha = dia_habil_pasado().isoformat()

    propuesta, visto = proponer(
        api,
        criadero,
        "proponer_tanda",
        {"raza": "Carniola", "fecha_traslarve": fecha, "cuadros": 2},
        rol="ventas",
    )

    assert propuesta["ruta"] == "/api/v1/tandas"
    assert propuesta["cuerpo"] == {
        "raza_id": str(criadero.carniola),
        "material_id": str(criadero.material),
        "fecha_traslarve": fecha,
        "cuadros": 2,
        "retroactivo": False,
    }
    TandaCrear.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta)["Celdas a crear"] == "270"
    assert propuesta["roles_permitidos"] == ["admin", "produccion"]
    # Ventas no puede confirmarla: el modelo lo sabe y el endpoint lo frena.
    assert visto["el_usuario_puede_confirmarla"] is False
    assert confirmar(api, criadero, propuesta, "ventas").status_code == 403
    assert confirmar(api, criadero, propuesta, "produccion").status_code == 201


def test_tanda_en_domingo_o_con_cuadros_de_mas_vuelve_al_modelo(
    api: TestClient, criadero: Criadero
) -> None:
    domingo = domingo_pasado().isoformat()

    en_domingo = error_de(
        api,
        criadero,
        "proponer_tanda",
        {"raza": "Buckfast", "fecha_traslarve": domingo, "cuadros": 1},
    )
    retroactiva, _ = proponer(
        api,
        criadero,
        "proponer_tanda",
        {
            "raza": "Buckfast",
            "fecha_traslarve": domingo,
            "cuadros": 1,
            "retroactivo": True,
            "fila_continuadora": 2,
            "posicion_continuadora": 3,
        },
    )
    cuadros = error_de(
        api,
        criadero,
        "proponer_tanda",
        {"raza": "Buckfast", "fecha_traslarve": domingo, "cuadros": 5},
    )

    assert "domingos" in en_domingo
    assert resumen(retroactiva)["Retroactiva"] == "sí"
    assert resumen(retroactiva)["Continuadora"] == "fila 2, posición 3"
    assert "cuadros" in cuadros


def test_tanda_con_varios_materiales_pregunta_cual(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    with motor_owner.begin() as c:
        fijar_tenant(c, criadero.tenant)
        c.execute(
            text("INSERT INTO material (nombre, unidad) VALUES ('Cúpulas 2', 'u')")
        )
    entrada = {
        "raza": "Buckfast",
        "fecha_traslarve": dia_habil_pasado().isoformat(),
        "cuadros": 1,
    }

    sin_material = error_de(api, criadero, "proponer_tanda", entrada)
    propuesta, _ = proponer(
        api, criadero, "proponer_tanda", {**entrada, "material": "Cúpulas 2"}
    )

    assert "Falta el material" in sin_material
    assert '"Cúpulas", "Cúpulas 2"' in sin_material
    assert resumen(propuesta)["Material"] == "Cúpulas 2 (se descuentan 135)"


# --- operar_tanda ---


@pytest.mark.parametrize(
    ("entrada", "cuerpo", "extra"),
    [
        pytest.param(
            {"accion": "descartar", "cantidad": 4, "fecha": "2026-10-02"},
            {"accion": "descartar", "cantidad": 4, "fecha": "2026-10-02"},
            {},
            id="descartar",
        ),
        pytest.param(
            {
                "accion": "introducir",
                "cantidad": 6,
                "fecha": "2026-10-11",
                "parque": "parque norte",
            },
            {"accion": "introducir", "cantidad": 6, "fecha": "2026-10-11"},
            {"Parque": "Parque Norte"},
            id="introducir",
        ),
        pytest.param(
            # La fecha sobra en pasar_a_banco (extra="forbid"): no se manda.
            {
                "accion": "pasar_a_banco",
                "cantidad": 10,
                "banco": "b-01",
                "fecha": "2026-10-20",
            },
            {"accion": "pasar_a_banco", "cantidad": 10},
            {"Banco": "B-01"},
            id="pasar_a_banco",
        ),
    ],
)
def test_operacion_por_lote_confirmable(
    api: TestClient,
    criadero: Criadero,
    entrada: dict[str, Any],
    cuerpo: dict[str, Any],
    extra: dict[str, str],
) -> None:
    # Introducir necesita una tanda sin cosechar; la otra ya tiene enjauladas.
    nueva = entrada["accion"] == "introducir"
    tanda = criadero.tanda_nueva if nueva else criadero.tanda
    propuesta, _ = proponer(
        api,
        criadero,
        "proponer_operacion_tanda",
        {"tanda_id": str(tanda), **entrada},
    )

    esperado = dict(cuerpo)
    if "Parque" in extra:
        esperado["parque_id"] = str(criadero.parque)
    if "Banco" in extra:
        esperado["banco_id"] = str(criadero.banco)
    assert propuesta["cuerpo"] == esperado
    TypeAdapter(OperacionCrear).validate_python(propuesta["cuerpo"])
    assert propuesta["ruta"] == f"/api/v1/tandas/{tanda}/operaciones"
    raza = "Carniola" if nueva else "Buckfast"
    assert resumen(propuesta)["Tanda"] == f"{raza} del {FECHA_TANDA}"
    assert resumen(propuesta)["Cantidad"] == str(entrada["cantidad"])
    for etiqueta, valor in extra.items():
        assert resumen(propuesta)[etiqueta] == valor
    assert confirmar(api, criadero, propuesta, "produccion").status_code == 201


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        pytest.param(
            {"accion": "introducir", "cantidad": 1, "fecha": "2026-10-11"},
            "Falta el parque",
            id="sin_parque",
        ),
        pytest.param(
            {"accion": "pasar_a_banco", "cantidad": 1},
            "Falta el banco",
            id="sin_banco",
        ),
        pytest.param(
            {"accion": "cosechar", "cantidad": 1},
            "fecha",
            id="sin_fecha",
        ),
        pytest.param(
            {"accion": "descartar", "cantidad": 1, "fecha": "2099-01-01"},
            "no puede ser futura",
            id="futura",
        ),
    ],
)
def test_operacion_invalida_vuelve_al_modelo(
    api: TestClient, criadero: Criadero, entrada: dict[str, Any], esperado: str
) -> None:
    mensaje = error_de(
        api,
        criadero,
        "proponer_operacion_tanda",
        {"tanda_id": str(criadero.tanda), **entrada},
    )

    assert esperado in mensaje


def test_operacion_sobre_una_tanda_inexistente(
    api: TestClient, criadero: Criadero
) -> None:
    mensaje = error_de(
        api,
        criadero,
        "proponer_operacion_tanda",
        {"tanda_id": str(uuid.uuid4()), "accion": "descartar", "cantidad": 1},
    )

    assert mensaje == "No existe la tanda."


# --- pedir_borrador ---


def test_borrador_confirmable(
    api: TestClient, criadero: Criadero, cola: ColaFalsa
) -> None:
    propuesta, _ = proponer(
        api, criadero, "proponer_borrador", {"pedido_id": str(criadero.pedido)}
    )

    assert propuesta["ruta"] == "/api/v1/documentos/borradores"
    assert propuesta["cuerpo"] == {"pedido_id": str(criadero.pedido)}
    BorradorPedido.model_validate(propuesta["cuerpo"])
    assert resumen(propuesta)["Pedido"] == "Apícola del Sur, entrega 2026-12-01"
    assert confirmar(api, criadero, propuesta, "ventas").status_code == 202
    assert len(cola.mensajes) == 1


# --- nada se escribe sin confirmar ---


def test_ninguna_propuesta_toca_la_base(
    api: TestClient, criadero: Criadero, motor_owner: Engine
) -> None:
    antes = foto(motor_owner, criadero.tenant)
    pedido = str(criadero.pedido)
    modelo = ModeloGuionado(
        [
            pide_varias(
                [
                    (
                        "proponer_cliente",
                        {
                            "nombre": "Bee AG",
                            "pais": "CH",
                            "tipo": "exportacion",
                            "condicion_iva": "cliente_exterior",
                            "cuit_o_tax_id": "CHE-1",
                        },
                        "1",
                    ),
                    (
                        "proponer_pedido",
                        {
                            "cliente": "Apícola del Sur",
                            "fecha_entrega_solicitada": "2026-12-15",
                            "items": [
                                {
                                    "producto": "Reina fecundada",
                                    "raza": "Buckfast",
                                    "cantidad": 3,
                                    "precio_unitario": 1,
                                }
                            ],
                        },
                        "2",
                    ),
                    ("proponer_reserva", {"pedido_id": pedido}, "3"),
                    (
                        "proponer_tanda",
                        {
                            "raza": "Buckfast",
                            "fecha_traslarve": dia_habil_pasado().isoformat(),
                            "cuadros": 3,
                        },
                        "4",
                    ),
                    (
                        "proponer_operacion_tanda",
                        {
                            "tanda_id": str(criadero.tanda),
                            "accion": "descartar",
                            "cantidad": 10,
                            "fecha": "2026-10-02",
                        },
                        "5",
                    ),
                    ("proponer_borrador", {"pedido_id": pedido}, "6"),
                ]
            ),
            responde("Te dejé seis propuestas para confirmar."),
        ]
    )

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 200
    acciones = [p["accion"] for p in respuesta.json()["propuestas"]]
    assert acciones == [
        "crear_cliente",
        "crear_pedido",
        "reservar_pedido",
        "crear_tanda",
        "operar_tanda",
        "pedir_borrador",
    ]
    ids = {p["id"] for p in respuesta.json()["propuestas"]}
    assert len(ids) == 6
    assert foto(motor_owner, criadero.tenant) == antes


def test_una_propuesta_invalida_se_corrige_en_la_ronda_siguiente(
    api: TestClient, criadero: Criadero
) -> None:
    fecha = dia_habil_pasado().isoformat()
    modelo = ModeloGuionado(
        [
            pide(
                "proponer_tanda",
                {"raza": "Buckfast", "fecha_traslarve": fecha, "cuadros": 4},
                "a",
            ),
            pide(
                "proponer_tanda",
                {"raza": "Buckfast", "fecha_traslarve": fecha, "cuadros": 3},
                "b",
            ),
            responde("Propuse la tanda con 3 cuadros; falta que la confirmes."),
        ]
    )

    respuesta = conversar(api, criadero, modelo)

    assert respuesta.status_code == 200
    (primero,) = modelo.resultados(1)
    (segundo,) = modelo.resultados(2)
    assert primero["status"] == "error"
    assert "cuadros" in datos_de(primero)["error"]
    assert segundo["status"] == "success"
    (propuesta,) = respuesta.json()["propuestas"]
    assert propuesta["cuerpo"]["cuadros"] == 3
