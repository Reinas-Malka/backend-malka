"""Datos y dobles compartidos por los tests del asistente.

Un criadero completo (razas, productos, clientes, parque, banco, material,
una tanda con reinas, un pedido, un embarque y un comprobante) cargado como
duenio, sin pasar por la API, y un modelo falso que responde segun un guion.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Connection, Engine, text

from app.main import app
from app.routers.asistente import obtener_modelo
from tests.conftest import crear_tenant, emitir, fijar_tenant

URL = "/api/v1/asistente"
HOLA = [{"rol": "usuario", "texto": "Hola"}]

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = (
    "borrador",
    "llamada_ia",
    "idempotencia",
    "reserva",
    "documento_embarque",
    "embarque",
    "comprobante",
    "documento",
    "movimiento_material",
    "celda",
    "tanda",
    "pedido_item",
    "pedido",
    "nucleo",
    "parque",
    "banco",
    "material",
    "item_vendible",
    "raza",
    "madre",
    "cliente",
)

FECHA_TANDA = "2026-10-01"


@dataclass(frozen=True)
class Criadero:
    tenant: uuid.UUID
    buckfast: uuid.UUID
    carniola: uuid.UUID
    reina_fecundada: uuid.UUID
    apicola_del_sur: uuid.UUID
    bienenhof: uuid.UUID
    parque: uuid.UUID
    banco: uuid.UUID
    material: uuid.UUID
    tanda: uuid.UUID
    # Carniola, solo trasladadas: todavia se puede introducir.
    tanda_nueva: uuid.UUID
    pedido: uuid.UUID


def _id(c: Connection, sql: str, **params: Any) -> uuid.UUID:
    return uuid.UUID(str(c.execute(text(sql), params).scalar_one()))


def _cliente(c: Connection, nombre: str, valores: tuple[str, str, str, str]) -> Any:
    pais, tipo, condicion, cuit = valores
    return _id(
        c,
        "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
        "VALUES (:nombre, :pais, :tipo, :condicion, :cuit) RETURNING id",
        nombre=nombre,
        pais=pais,
        tipo=tipo,
        condicion=condicion,
        cuit=cuit,
    )


def cargar_criadero(c: Connection, tenant: uuid.UUID) -> Criadero:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    razas = {
        nombre: _id(
            c, "INSERT INTO raza (nombre) VALUES (:nombre) RETURNING id", nombre=nombre
        )
        for nombre in ("Buckfast", "Carniola", "Carniola Negra")
    }
    fecundada = _id(
        c, "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') RETURNING id"
    )
    _id(c, "INSERT INTO item_vendible (nombre) VALUES ('Reina virgen') RETURNING id")
    sur = _cliente(
        c, "Apícola del Sur", ("AR", "nacional", "monotributo", "20123456786")
    )
    _cliente(
        c,
        "Apícola del Norte",
        ("AR", "nacional", "responsable_inscripto", "30123456781"),
    )
    bienenhof = _cliente(
        c, "Bienenhof GmbH", ("DE", "exportacion", "cliente_exterior", "DE123456789")
    )
    parque = _id(
        c,
        "INSERT INTO parque (nombre, ubicacion, capacidad_max) "
        "VALUES ('Parque Norte', 'Chacra 12', 50) RETURNING id",
    )
    nucleo = _id(
        c,
        "INSERT INTO nucleo (parque_id, fila, posicion) VALUES (:p, 1, 1) RETURNING id",
        p=parque,
    )
    _id(
        c,
        "INSERT INTO nucleo (parque_id, fila, posicion) VALUES (:p, 1, 2) RETURNING id",
        p=parque,
    )
    banco = _id(c, "INSERT INTO banco (identificador) VALUES ('B-01') RETURNING id")
    material = _id(
        c,
        "INSERT INTO material (nombre, unidad) VALUES ('Cúpulas', 'unidad') "
        "RETURNING id",
    )
    tanda = _id(
        c,
        "INSERT INTO tanda (raza_id, fecha_traslarve) VALUES (:raza, :fecha) "
        "RETURNING id",
        raza=razas["Buckfast"],
        fecha=FECHA_TANDA,
    )
    # 50 enjauladas (30 en el banco) y 10 trasladadas todavia sin introducir.
    for cantidad, banco_id in ((30, banco), (20, None)):
        c.execute(
            text(
                "INSERT INTO celda (tanda_id, estado, nucleo_id, fecha_introduccion, "
                "banco_id) SELECT :tanda, 'enjaulada', :nucleo, '2026-10-10', :banco "
                "FROM generate_series(1, :cantidad)"
            ),
            {"tanda": tanda, "nucleo": nucleo, "banco": banco_id, "cantidad": cantidad},
        )
    c.execute(
        text("INSERT INTO celda (tanda_id) SELECT :tanda FROM generate_series(1, 10)"),
        {"tanda": tanda},
    )
    tanda_nueva = _id(
        c,
        "INSERT INTO tanda (raza_id, fecha_traslarve) VALUES (:raza, :fecha) "
        "RETURNING id",
        raza=razas["Carniola"],
        fecha=FECHA_TANDA,
    )
    c.execute(
        text("INSERT INTO celda (tanda_id) SELECT :tanda FROM generate_series(1, 20)"),
        {"tanda": tanda_nueva},
    )
    pedido = _id(
        c,
        "INSERT INTO pedido (cliente_id, moneda, fecha_entrega_solicitada) "
        "VALUES (:cliente, 'ARS', '2026-12-01') RETURNING id",
        cliente=sur,
    )
    c.execute(
        text(
            "INSERT INTO pedido_item (pedido_id, numero_linea, item_vendible_id, "
            "raza_id, cantidad, precio_unitario, alicuota) "
            "VALUES (:pedido, 1, :item, :raza, 5, 35000.00, 0.21)"
        ),
        {"pedido": pedido, "item": fecundada, "raza": razas["Buckfast"]},
    )
    c.execute(
        text(
            "INSERT INTO embarque (destino_pais, fecha_vuelo) "
            "VALUES ('DE', '2026-11-20')"
        )
    )
    archivo = f"documentos/{tenant}/factura-1.pdf"
    c.execute(
        text(
            "INSERT INTO documento (archivo_s3_key, nombre, tipo_contenido, "
            "tamano_bytes, subido_por) VALUES (:clave, 'factura.pdf', "
            "'application/pdf', 1024, 'usuario-1')"
        ),
        {"clave": archivo},
    )
    c.execute(
        text(
            "INSERT INTO comprobante (tipo, cliente_id, punto_venta, numero, "
            "fecha_emision, moneda, importe, archivo_s3_key, cargado_por) "
            "VALUES ('B', :cliente, 3, 45, '2026-10-20', 'ARS', 175000.00, "
            ":archivo, 'usuario-1')"
        ),
        {"cliente": sur, "archivo": archivo},
    )
    return Criadero(
        tenant=tenant,
        buckfast=razas["Buckfast"],
        carniola=razas["Carniola"],
        reina_fecundada=fecundada,
        apicola_del_sur=sur,
        bienenhof=bienenhof,
        parque=parque,
        banco=banco,
        material=material,
        tanda=tanda,
        tanda_nueva=tanda_nueva,
        pedido=pedido,
    )


def cargar_ajeno(c: Connection, tenant: uuid.UUID) -> uuid.UUID:
    """Otro criadero con un cliente y un pedido propios. Devuelve el pedido."""
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    c.execute(text("INSERT INTO raza (nombre) VALUES ('Ligustica')"))
    cliente = _cliente(
        c, "Secreto de Otro Criadero", ("AR", "nacional", "monotributo", "20123456786")
    )
    return _id(
        c,
        "INSERT INTO pedido (cliente_id, moneda, fecha_entrega_solicitada) "
        "VALUES (:cliente, 'ARS', '2026-12-01') RETURNING id",
        cliente=cliente,
    )


def limpiar(motor: Engine, tenant: uuid.UUID) -> None:
    with motor.begin() as c:
        fijar_tenant(c, tenant)
        for tabla in TABLAS:
            c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
        c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


# --- el modelo falso ---

USO = {"inputTokens": 120, "outputTokens": 30}


def pide(nombre: str, entrada: dict[str, Any], uso_id: str = "uso-1") -> dict[str, Any]:
    """Una respuesta de Converse que pide una herramienta."""
    return pide_varias([(nombre, entrada, uso_id)])


def pide_varias(usos: list[tuple[str, dict[str, Any], str]]) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": i, "name": n, "input": e}}
                    for n, e, i in usos
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": USO,
    }


def responde(texto: str) -> dict[str, Any]:
    """Una respuesta final de Converse, solo texto."""
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": texto}]}},
        "stopReason": "end_turn",
        "usage": USO,
    }


class ModeloGuionado:
    """Doble de bedrock-runtime: devuelve las respuestas del guion en orden y
    guarda una copia de cada pedido que recibio."""

    def __init__(self, guion: list[dict[str, Any]]) -> None:
        self.guion = list(guion)
        self.pedidos: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.pedidos.append(copy.deepcopy(kwargs))
        return self.guion.pop(0)

    def resultados(self, pedido: int) -> list[dict[str, Any]]:
        """Los toolResult que el servidor le mando al modelo en ese pedido."""
        ultimo = self.pedidos[pedido]["messages"][-1]
        return [bloque["toolResult"] for bloque in ultimo["content"]]


class ModeloCaido:
    def converse(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ThrottlingException: detalle interno de AWS")


def datos_de(resultado: dict[str, Any]) -> Any:
    """El JSON adentro de <datos>...</datos> de un toolResult."""
    contenido = resultado["content"][0]["text"]
    coincidencia = re.fullmatch(r"<datos>\n(.*)\n</datos>", contenido, re.DOTALL)
    assert coincidencia, contenido
    return json.loads(coincidencia.group(1))


# --- pedidos al endpoint ---


def como(tenant: uuid.UUID, rol: str | None = "admin") -> dict[str, str]:
    grupos = [] if rol is None else [rol]
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": grupos})
    return {"Authorization": f"Bearer {token}"}


def conversar(
    api: TestClient,
    criadero: Criadero,
    modelo: Any,
    *,
    rol: str | None = "admin",
    mensajes: list[dict[str, str]] | None = None,
) -> Response:
    app.dependency_overrides[obtener_modelo] = lambda: modelo
    try:
        return api.post(
            URL,
            json={"mensajes": HOLA if mensajes is None else mensajes},
            headers=como(criadero.tenant, rol),
        )
    finally:
        app.dependency_overrides.pop(obtener_modelo, None)


def usar(
    api: TestClient,
    criadero: Criadero,
    nombre: str,
    entrada: dict[str, Any],
    rol: str = "admin",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Una ronda con una herramienta: la respuesta y el toolResult que vio el
    modelo."""
    modelo = ModeloGuionado([pide(nombre, entrada), responde("Listo.")])
    respuesta = conversar(api, criadero, modelo, rol=rol)
    assert respuesta.status_code == 200, respuesta.json()
    return respuesta.json(), modelo.resultados(1)[0]


def leer(
    api: TestClient, criadero: Criadero, nombre: str, entrada: dict[str, Any]
) -> Any:
    """Los datos de una herramienta de lectura que respondio bien."""
    _, resultado = usar(api, criadero, nombre, entrada)
    assert resultado["status"] == "success", resultado
    return datos_de(resultado)


def error_de(
    api: TestClient, criadero: Criadero, nombre: str, entrada: dict[str, Any]
) -> str:
    """El mensaje de error que vio el modelo; la respuesta no trae propuestas."""
    cuerpo, resultado = usar(api, criadero, nombre, entrada)
    assert resultado["status"] == "error", resultado
    assert cuerpo["propuestas"] == []
    mensaje: str = datos_de(resultado)["error"]
    return mensaje
