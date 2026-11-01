"""Borrador de documento con IA de punta a punta (#40, DoD).

POST /api/v1/documentos/borradores deja el borrador en `generando`, reserva
la llamada (limite del #45) y lo encola; el worker arma el contexto leyendo
la base, llama al modelo (mockeado), valida la salida y deja el borrador en
`borrador` o `rechazado`. Nada fiscal ni invalido se persiste.

Contra PostgreSQL real con RLS (migracion 0016).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app.main import app
from app.routers.borradores import obtener_cola
from app.services.ia import generacion
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/documentos/borradores"

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = (
    "borrador",
    "llamada_ia",
    "pedido_item",
    "pedido",
    "item_vendible",
    "raza",
    "cliente",
    "idempotencia",
)

JSON_VALIDO = json.dumps(
    {
        "encabezado": "Remito de venta",
        "cuerpo": "Se entregan 20 reinas fecundadas Carniola a Apicola del Sur.",
        "notas": "",
    }
)


@dataclass(frozen=True)
class Datos:
    tenant: uuid.UUID
    pedido: uuid.UUID


def _id(c: Connection, sql: str, **params: Any) -> uuid.UUID:
    valor = c.execute(text(sql), params).scalar_one()
    assert isinstance(valor, uuid.UUID)
    return valor


def _cargar(c: Connection, tenant: uuid.UUID) -> Datos:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    cliente = _id(
        c,
        "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
        "VALUES ('Apicola del Sur', 'AR', 'nacional', 'monotributo', "
        "'20123456786') RETURNING id",
    )
    item = _id(
        c, "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') RETURNING id"
    )
    raza = _id(c, "INSERT INTO raza (nombre) VALUES ('Carniola') RETURNING id")
    pedido = _id(
        c,
        "INSERT INTO pedido (cliente_id, moneda, fecha_entrega_solicitada) "
        "VALUES (:cliente, 'ARS', '2026-11-20') RETURNING id",
        cliente=cliente,
    )
    c.execute(
        text(
            "INSERT INTO pedido_item (pedido_id, numero_linea, item_vendible_id, "
            "raza_id, cantidad, precio_unitario, alicuota) "
            "VALUES (:pedido, 1, :item, :raza, 20, 35000.50, 0.21)"
        ),
        {"pedido": pedido, "item": item, "raza": raza},
    )
    return Datos(tenant=tenant, pedido=pedido)


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[Datos, Datos]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    with motor_owner.begin() as c:
        datos_a = _cargar(c, a)
    with motor_owner.begin() as c:
        datos_b = _cargar(c, b)
    yield datos_a, datos_b
    for tenant in (a, b):
        with motor_owner.begin() as c:
            fijar_tenant(c, tenant)
            for tabla in TABLAS:
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608
            c.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tenant})


@pytest.fixture(autouse=True)
def sin_borradores(
    motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> Iterator[None]:
    yield
    for datos in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, datos.tenant)
            for tabla in ("borrador", "llamada_ia", "idempotencia"):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


class ColaFalsa:
    """Doble de la cola SQS: guarda lo que se encola."""

    def __init__(self) -> None:
        self.mensajes: list[dict[str, Any]] = []

    def enviar(self, cuerpo: dict[str, Any]) -> None:
        self.mensajes.append(cuerpo)


class ColaRota:
    def enviar(self, cuerpo: dict[str, Any]) -> None:
        raise RuntimeError("SQS no responde")


class ModeloFalso:
    """Doble de bedrock-runtime: respuestas encoladas, cuenta las llamadas."""

    def __init__(self, respuestas: list[str]) -> None:
        self.respuestas = list(respuestas)
        self.mensajes: list[str] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.mensajes.append(kwargs["messages"][0]["content"][0]["text"])
        return {
            "output": {"message": {"content": [{"text": self.respuestas.pop(0)}]}},
            "usage": {"inputTokens": 300, "outputTokens": 80},
        }


class ModeloCaido:
    def converse(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ThrottlingException")


@pytest.fixture
def cola() -> Iterator[ColaFalsa]:
    falsa = ColaFalsa()
    app.dependency_overrides[obtener_cola] = lambda: falsa
    yield falsa
    app.dependency_overrides.pop(obtener_cola, None)


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "ventas") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def pedir(api: TestClient, datos: Datos, **headers: str) -> Any:
    respuesta = api.post(
        URL,
        json={"pedido_id": str(datos.pedido)},
        headers={**como(datos.tenant), **headers},
    )
    assert respuesta.status_code == 202, respuesta.json()
    return respuesta.json()


def ver(api: TestClient, datos: Datos, borrador_id: str) -> Any:
    return api.get(f"{URL}/{borrador_id}", headers=como(datos.tenant)).json()


# --- el pedido ---


def test_pedir_un_borrador_lo_deja_generando_y_lo_encola(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos

    borrador = pedir(api, datos)

    assert borrador["estado"] == "generando"
    assert borrador["pedido_id"] == str(datos.pedido)
    assert borrador["contenido"] is None
    # El mensaje lleva ids, nunca datos del cliente.
    [mensaje] = cola.mensajes
    assert mensaje == {
        "borrador_id": borrador["id"],
        "tenant_id": str(datos.tenant),
    }


def test_la_misma_clave_encola_una_sola_vez(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    clave = str(uuid.uuid4())

    primera = pedir(api, datos, **{"Idempotency-Key": clave})
    segunda = pedir(api, datos, **{"Idempotency-Key": clave})

    assert primera == segunda
    assert len(cola.mensajes) == 1


@pytest.mark.parametrize("pedido", ["de_otro_criadero", "inexistente"])
def test_un_pedido_ajeno_o_inexistente_es_422(
    api: TestClient,
    cola: ColaFalsa,
    criaderos: tuple[Datos, Datos],
    pedido: str,
) -> None:
    a, b = criaderos
    pedido_id = b.pedido if pedido == "de_otro_criadero" else uuid.uuid4()

    respuesta = api.post(
        URL, json={"pedido_id": str(pedido_id)}, headers=como(a.tenant)
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "pedido_inexistente"
    assert cola.mensajes == []


def test_pasado_el_tope_de_ia_es_429_y_no_queda_nada(
    api: TestClient,
    cola: ColaFalsa,
    criaderos: tuple[Datos, Datos],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datos, _ = criaderos
    monkeypatch.setenv("IA_LLAMADAS_POR_MINUTO", "1")
    pedir(api, datos)

    respuesta = api.post(
        URL, json={"pedido_id": str(datos.pedido)}, headers=como(datos.tenant)
    )

    assert respuesta.status_code == 429
    assert respuesta.headers["Retry-After"] == "60"
    assert len(cola.mensajes) == 1


@pytest.mark.parametrize("rol", ["lectura", "produccion"])
def test_solo_admin_y_ventas_piden_borradores(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos], rol: str
) -> None:
    datos, _ = criaderos

    respuesta = api.post(
        URL, json={"pedido_id": str(datos.pedido)}, headers=como(datos.tenant, rol)
    )

    assert respuesta.status_code == 403


def test_si_la_cola_falla_el_borrador_queda_rechazado(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    """Sin esto quedaria en generando para siempre."""
    datos, _ = criaderos
    app.dependency_overrides[obtener_cola] = ColaRota
    try:
        borrador = pedir(api, datos)
    finally:
        app.dependency_overrides.pop(obtener_cola, None)

    leido = ver(api, datos, borrador["id"])
    assert leido["estado"] == "rechazado"
    assert leido["motivo_rechazo"] == "cola_no_disponible"


# --- el worker ---


def procesar(cola: ColaFalsa, modelo: Any) -> None:
    for mensaje in cola.mensajes:
        generacion.procesar(mensaje, modelo)


def test_el_worker_deja_el_borrador_listo_con_sus_metricas(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = pedir(api, datos)
    modelo = ModeloFalso([JSON_VALIDO])

    procesar(cola, modelo)

    leido = ver(api, datos, borrador["id"])
    assert leido["estado"] == "borrador"
    assert leido["contenido"]["encabezado"] == "Remito de venta"
    assert leido["version_prompt"] == "borrador_v2"
    assert leido["motivo_rechazo"] is None
    # El contexto sale de la base (el modelo no descubre datos) y va
    # delimitado como datos.
    [mensaje] = modelo.mensajes
    assert mensaje.startswith("<datos>")
    for dato in ("Apicola del Sur", "Carniola", "20", "Reina fecundada"):
        assert dato in mensaje


def test_el_worker_anota_los_tokens_en_el_limite_de_ia(
    api: TestClient,
    cola: ColaFalsa,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
) -> None:
    datos, _ = criaderos
    pedir(api, datos)

    procesar(cola, ModeloFalso([JSON_VALIDO]))

    with motor_owner.begin() as c:
        fijar_tenant(c, datos.tenant)
        fila = c.execute(
            text("SELECT tokens_entrada, tokens_salida FROM llamada_ia")
        ).one()
    assert (fila.tokens_entrada, fila.tokens_salida) == (300, 80)


def test_dos_salidas_invalidas_rechazan_sin_persistir_contenido(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    """DoD: con una salida invalida no se persiste nada."""
    datos, _ = criaderos
    borrador = pedir(api, datos)
    fiscal = json.dumps(
        {"encabezado": "X", "cuerpo": "Total USD 1500 con IVA 21%", "notas": ""}
    )

    procesar(cola, ModeloFalso([fiscal, fiscal]))

    leido = ver(api, datos, borrador["id"])
    assert leido["estado"] == "rechazado"
    assert leido["motivo_rechazo"] == "salida_invalida"
    assert leido["contenido"] is None


def test_si_el_modelo_falla_el_borrador_queda_rechazado(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = pedir(api, datos)

    procesar(cola, ModeloCaido())

    leido = ver(api, datos, borrador["id"])
    assert (leido["estado"], leido["motivo_rechazo"]) == ("rechazado", "error_modelo")


def test_un_mensaje_repetido_no_vuelve_a_llamar_al_modelo(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    """SQS entrega al menos una vez: el segundo mensaje se ignora."""
    datos, _ = criaderos
    pedir(api, datos)
    modelo = ModeloFalso([JSON_VALIDO])
    procesar(cola, modelo)

    procesar(cola, modelo)

    assert len(modelo.mensajes) == 1


# --- lectura ---


def test_lista_los_borradores_de_un_pedido(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    primero = pedir(api, datos)

    respuesta = api.get(
        URL,
        params={"pedido_id": str(datos.pedido)},
        headers=como(datos.tenant, "lectura"),
    )

    assert [b["id"] for b in respuesta.json()["items"]] == [primero["id"]]


def test_el_borrador_de_otro_criadero_es_404(
    api: TestClient, cola: ColaFalsa, criaderos: tuple[Datos, Datos]
) -> None:
    a, b = criaderos
    ajeno = pedir(api, b)

    respuesta = api.get(f"{URL}/{ajeno['id']}", headers=como(a.tenant))

    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["code"] == "borrador_no_encontrado"


def test_checks_del_borrador(
    motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    """Un borrador listo tiene contenido; uno rechazado, motivo y nada mas."""
    datos, _ = criaderos
    with pytest.raises(Exception, match="ck_borrador_contenido_segun_estado"):
        with motor_owner.begin() as c:
            fijar_tenant(c, datos.tenant)
            c.execute(
                text(
                    "INSERT INTO borrador (pedido_id, estado, solicitado_por) "
                    "VALUES (:p, 'borrador', 'sub')"
                ),
                {"p": datos.pedido},
            )
