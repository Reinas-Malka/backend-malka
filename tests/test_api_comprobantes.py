"""Endpoints de comprobantes transcriptos de punta a punta (#108, DoD).

Requests HTTP reales contra la app: token firmado, sesion con el tenant del
token y PostgreSQL con RLS (migracion 0014). Cubre tambien el repositorio,
que no tiene tests propios.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from app.main import app
from tests.conftest import crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app"),
]

URL = "/api/v1/comprobantes"

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = ("comprobante", "documento", "cliente", "idempotencia")


@dataclass(frozen=True)
class Datos:
    tenant: uuid.UUID
    inscripto: uuid.UUID
    monotributo: uuid.UUID
    exportacion: uuid.UUID
    inactivo: uuid.UUID
    archivos: tuple[str, ...]


INSERTAR_CLIENTE = text(
    "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id, "
    "activo) VALUES (:nombre, :pais, :tipo, :condicion_iva, :cuit, :activo) "
    "RETURNING id"
)


def _cliente(
    c: Connection, nombre: str, valores: str, activo: bool = True
) -> uuid.UUID:
    """valores: "pais, tipo, condicion_iva, cuit", separados por coma."""
    pais, tipo, condicion_iva, cuit = (v.strip(" '") for v in valores.split(","))
    valor = c.execute(
        INSERTAR_CLIENTE,
        {
            "nombre": nombre,
            "pais": pais,
            "tipo": tipo,
            "condicion_iva": condicion_iva,
            "cuit": cuit,
            "activo": activo,
        },
    ).scalar_one()
    assert isinstance(valor, uuid.UUID)
    return valor


def _cargar(c: Connection, tenant: uuid.UUID) -> Datos:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    # Los PDF ya subidos por POST /documentos/subidas (#102): el comprobante
    # solo puede apuntar a un archivo registrado del criadero.
    archivos = tuple(f"documentos/{tenant}/factura-{n}.pdf" for n in range(1, 6))
    for clave in archivos:
        c.execute(
            text(
                "INSERT INTO documento (archivo_s3_key, nombre, tipo_contenido, "
                "tamano_bytes, subido_por) VALUES (:clave, 'factura.pdf', "
                "'application/pdf', 1024, 'usuario-1')"
            ),
            {"clave": clave},
        )
    return Datos(
        tenant=tenant,
        inscripto=_cliente(
            c,
            "Miel SA",
            "'AR', 'nacional', 'responsable_inscripto', '30123456781'",
        ),
        monotributo=_cliente(
            c, "Apicola del Sur", "'AR', 'nacional', 'monotributo', '20123456786'"
        ),
        exportacion=_cliente(
            c,
            "Bienenzucht GmbH",
            "'DE', 'exportacion', 'cliente_exterior', 'DE123456789'",
        ),
        inactivo=_cliente(
            c,
            "Dado de baja",
            "'AR', 'nacional', 'responsable_inscripto', '30111111118'",
            activo=False,
        ),
        archivos=archivos,
    )


@pytest.fixture(scope="module")
def criaderos(motor_owner: Engine) -> Iterator[tuple[Datos, Datos]]:
    """A usa la API; B tiene sus propios clientes y archivos para alcanzar."""
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
def sin_comprobantes(
    motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> Iterator[None]:
    """Cada test arranca sin comprobantes en ninguno de los dos criaderos."""
    yield
    for datos in criaderos:
        with motor_owner.begin() as c:
            fijar_tenant(c, datos.tenant)
            for tabla in ("comprobante", "idempotencia"):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def como(
    tenant: uuid.UUID, rol: str = "ventas", sub: str = "usuario-1"
) -> dict[str, str]:
    token = emitir(
        sub=sub, **{"custom:tenant_id": str(tenant), "cognito:groups": [rol]}
    )
    return {"Authorization": f"Bearer {token}"}


def codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


def factura_a(datos: Datos, **cambios: Any) -> dict[str, Any]:
    return {
        "tipo": "A",
        "cliente_id": str(datos.inscripto),
        "punto_venta": 1,
        "numero": 45,
        "fecha_emision": "2026-10-20",
        "moneda": "ARS",
        "importe": "423506.05",
        "archivo_s3_key": datos.archivos[0],
        **cambios,
    }


def factura_e(datos: Datos, **cambios: Any) -> dict[str, Any]:
    return factura_a(
        datos,
        **{
            "tipo": "E",
            "cliente_id": str(datos.exportacion),
            "punto_venta": 3,
            "moneda": "EUR",
            "importe": "350005.00",
            "tipo_cambio": "1125.500000",
            "archivo_s3_key": datos.archivos[1],
            **cambios,
        },
    )


def registrar(api: TestClient, datos: Datos, cuerpo: dict[str, Any]) -> Any:
    respuesta = api.post(URL, json=cuerpo, headers=como(datos.tenant))
    assert respuesta.status_code == 201, respuesta.json()
    return respuesta.json()


# --- DoD ---


def test_registra_una_factura_a_tal_como_se_transcribio(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos

    respuesta = api.post(
        URL, json=factura_a(datos), headers=como(datos.tenant, sub="facturadora-2")
    )

    assert respuesta.status_code == 201, respuesta.json()
    comprobante = respuesta.json()
    # Los numeros salen exactamente como entraron: el sistema no los genera.
    transcripto = factura_a(datos)
    assert {k: comprobante[k] for k in transcripto} == transcripto
    assert comprobante["tipo_cambio"] is None
    # Auditoria: quien lo cargo (el sub del token), no el emisor fiscal.
    assert comprobante["cargado_por"] == "facturadora-2"


def test_registra_una_factura_e_en_euros_con_tipo_de_cambio(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos

    comprobante = registrar(api, datos, factura_e(datos))

    assert (comprobante["tipo"], comprobante["moneda"]) == ("E", "EUR")
    assert comprobante["importe"] == "350005.00"
    assert comprobante["tipo_cambio"] == "1125.500000"


@pytest.mark.parametrize(
    ("cliente", "letra"),
    [
        pytest.param("inscripto", "B", id="inscripto_con_b"),
        pytest.param("monotributo", "A", id="monotributo_con_a"),
        pytest.param("exportacion", "A", id="exterior_con_a"),
    ],
)
def test_la_letra_tiene_que_ser_coherente_con_el_cliente(
    api: TestClient, criaderos: tuple[Datos, Datos], cliente: str, letra: str
) -> None:
    datos, _ = criaderos
    cuerpo = factura_a(datos, tipo=letra, cliente_id=str(getattr(datos, cliente)))

    respuesta = api.post(URL, json=cuerpo, headers=como(datos.tenant))

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "letra_incoherente"


def test_un_e_para_un_cliente_nacional_es_422(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos

    respuesta = api.post(
        URL,
        json=factura_e(datos, cliente_id=str(datos.inscripto)),
        headers=como(datos.tenant),
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "letra_incoherente"


def test_el_mismo_tipo_punto_de_venta_y_numero_es_409(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    registrar(api, datos, factura_a(datos))

    repetido = api.post(
        URL,
        json=factura_a(datos, archivo_s3_key=datos.archivos[2]),
        headers=como(datos.tenant),
    )

    assert repetido.status_code == 409
    assert codigo(repetido) == "comprobante_duplicado"


def test_otra_letra_con_el_mismo_numero_no_es_duplicado(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    """La clave de ARCA incluye la letra: A 0001-45 y B 0001-45 son distintos."""
    datos, _ = criaderos
    registrar(api, datos, factura_a(datos))

    registrar(
        api,
        datos,
        factura_a(
            datos,
            tipo="B",
            cliente_id=str(datos.monotributo),
            archivo_s3_key=datos.archivos[2],
        ),
    )


def test_el_mismo_numero_en_otro_criadero_no_es_duplicado(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    a, b = criaderos
    registrar(api, a, factura_a(a))

    registrar(api, b, factura_a(b))


def test_el_archivo_tiene_que_estar_subido(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    nunca_subido = f"documentos/{datos.tenant}/no-existe.pdf"

    respuesta = api.post(
        URL,
        json=factura_a(datos, archivo_s3_key=nunca_subido),
        headers=como(datos.tenant),
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "archivo_inexistente"


def test_no_se_registra_un_archivo_de_otro_criadero(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    a, b = criaderos

    respuesta = api.post(
        URL, json=factura_a(a, archivo_s3_key=b.archivos[0]), headers=como(a.tenant)
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "archivo_inexistente"


@pytest.mark.parametrize("cliente", ["de_otro_criadero", "inexistente"])
def test_un_cliente_ajeno_o_inexistente_es_422(
    api: TestClient, criaderos: tuple[Datos, Datos], cliente: str
) -> None:
    a, b = criaderos
    cliente_id = b.inscripto if cliente == "de_otro_criadero" else uuid.uuid4()

    respuesta = api.post(
        URL, json=factura_a(a, cliente_id=str(cliente_id)), headers=como(a.tenant)
    )

    assert respuesta.status_code == 422
    assert codigo(respuesta) == "cliente_inexistente"


def test_un_cliente_dado_de_baja_conserva_su_facturacion(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    """La factura ya la emitio ARCA: se registra aunque el cliente este de baja."""
    datos, _ = criaderos

    registrar(api, datos, factura_a(datos, cliente_id=str(datos.inactivo)))


def test_lista_de_la_emision_mas_nueva_a_la_mas_vieja_y_pagina(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    for n, fecha in enumerate(["2026-10-01", "2026-10-15", "2026-10-08"]):
        registrar(
            api,
            datos,
            factura_a(
                datos,
                numero=100 + n,
                fecha_emision=fecha,
                archivo_s3_key=datos.archivos[n],
            ),
        )

    primera = api.get(URL, params={"limite": 2}, headers=como(datos.tenant)).json()
    segunda = api.get(
        URL,
        params={"limite": 2, "cursor": primera["siguiente"]},
        headers=como(datos.tenant),
    ).json()

    fechas = [c["fecha_emision"] for c in primera["items"] + segunda["items"]]
    assert fechas == ["2026-10-15", "2026-10-08", "2026-10-01"]
    assert segunda["siguiente"] is None


def test_filtra_por_cliente(api: TestClient, criaderos: tuple[Datos, Datos]) -> None:
    datos, _ = criaderos
    registrar(api, datos, factura_a(datos))
    registrar(api, datos, factura_e(datos))

    respuesta = api.get(
        URL, params={"cliente_id": str(datos.exportacion)}, headers=como(datos.tenant)
    )

    assert [c["tipo"] for c in respuesta.json()["items"]] == ["E"]


def test_el_listado_no_muestra_los_de_otro_criadero(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    a, b = criaderos
    registrar(api, b, factura_a(b))

    respuesta = api.get(URL, headers=como(a.tenant))

    assert respuesta.json()["items"] == []


def test_obtiene_uno_y_el_de_otro_criadero_es_404(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    a, b = criaderos
    propio = registrar(api, a, factura_a(a))
    ajeno = registrar(api, b, factura_a(b))

    assert api.get(f"{URL}/{propio['id']}", headers=como(a.tenant)).json() == propio
    for id_ in (ajeno["id"], uuid.uuid4()):
        respuesta = api.get(f"{URL}/{id_}", headers=como(a.tenant))
        assert respuesta.status_code == 404
        assert codigo(respuesta) == "comprobante_no_encontrado"


@pytest.mark.parametrize("rol", ["lectura", "produccion"])
def test_solo_admin_y_ventas_registran(
    api: TestClient, criaderos: tuple[Datos, Datos], rol: str
) -> None:
    datos, _ = criaderos

    respuesta = api.post(URL, json=factura_a(datos), headers=como(datos.tenant, rol))

    assert respuesta.status_code == 403
    # Leer pueden todos los roles.
    assert api.get(URL, headers=como(datos.tenant, rol)).status_code == 200


def test_admin_registra(api: TestClient, criaderos: tuple[Datos, Datos]) -> None:
    datos, _ = criaderos

    respuesta = api.post(
        URL, json=factura_a(datos), headers=como(datos.tenant, "admin")
    )

    assert respuesta.status_code == 201


def test_lo_transcripto_no_se_edita_ni_se_borra(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    comprobante = registrar(api, datos, factura_a(datos))

    for metodo in ("PATCH", "PUT", "DELETE"):
        respuesta = api.request(
            metodo,
            f"{URL}/{comprobante['id']}",
            json={"numero": 46},
            headers=como(datos.tenant, "admin"),
        )
        assert respuesta.status_code == 405


def test_la_misma_clave_registra_un_solo_comprobante(
    api: TestClient, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    headers = {**como(datos.tenant), "Idempotency-Key": str(uuid.uuid4())}

    primera = api.post(URL, json=factura_a(datos), headers=headers)
    segunda = api.post(URL, json=factura_a(datos), headers=headers)

    assert primera.status_code == segunda.status_code == 201
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert primera.json() == segunda.json()
    listado = api.get(URL, headers=como(datos.tenant)).json()
    assert len(listado["items"]) == 1


# --- Restricciones de la base (por si algo esquiva la API) ---


def _insertar(
    motor_owner: Engine, datos: Datos, cliente: uuid.UUID, archivo: str, **columnas: Any
) -> None:
    valores = {
        "tipo": "A",
        "punto_venta": 1,
        "numero": 1,
        "moneda": "ARS",
        "importe": "100.00",
        "tipo_cambio": None,
        **columnas,
    }
    with motor_owner.begin() as c:
        fijar_tenant(c, datos.tenant)
        c.execute(
            text(
                "INSERT INTO comprobante (tipo, cliente_id, punto_venta, numero, "
                "fecha_emision, moneda, importe, tipo_cambio, archivo_s3_key, "
                "cargado_por) VALUES (:tipo, :cliente, :punto_venta, :numero, "
                "'2026-10-20', :moneda, :importe, :tipo_cambio, :archivo, 'sub')"
            ),
            {**valores, "cliente": cliente, "archivo": archivo},
        )


@pytest.mark.parametrize(
    ("columnas", "restriccion"),
    [
        pytest.param({"tipo": "C"}, "ck_comprobante_tipo", id="letra_c"),
        pytest.param({"moneda": "BRL"}, "ck_comprobante_moneda", id="moneda_brl"),
        pytest.param(
            {"tipo_cambio": "1000"}, "ck_comprobante_tipo_cambio", id="ars_con_tc"
        ),
        pytest.param(
            {"moneda": "USD", "tipo": "E"},
            "ck_comprobante_tipo_cambio",
            id="usd_sin_tc",
        ),
        pytest.param(
            {"moneda": "USD", "tipo_cambio": "1000"},
            "ck_comprobante_moneda_segun_tipo",
            id="a_en_dolares",
        ),
        pytest.param({"tipo": "E"}, "ck_comprobante_moneda_segun_tipo", id="e_en_ars"),
        pytest.param({"punto_venta": 0}, "ck_comprobante_punto_venta", id="pv_cero"),
        pytest.param({"numero": 10**8}, "ck_comprobante_numero", id="numero_9_dig"),
        pytest.param({"importe": "0"}, "ck_comprobante_importe", id="importe_cero"),
    ],
)
def test_checks_del_comprobante(
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    columnas: dict[str, Any],
    restriccion: str,
) -> None:
    datos, _ = criaderos

    with pytest.raises(IntegrityError, match=restriccion):
        _insertar(motor_owner, datos, datos.inscripto, datos.archivos[0], **columnas)


def test_el_unique_espeja_la_clave_de_arca(
    motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    _insertar(motor_owner, datos, datos.inscripto, datos.archivos[0])

    with pytest.raises(IntegrityError, match="uq_comprobante_tenant_tipo_punto_venta"):
        _insertar(motor_owner, datos, datos.inscripto, datos.archivos[1])


@pytest.mark.parametrize(
    ("ajeno", "restriccion"),
    [
        pytest.param("cliente", "fk_comprobante_cliente_mismo_tenant", id="cliente"),
        pytest.param("archivo", "fk_comprobante_documento_mismo_tenant", id="archivo"),
    ],
)
def test_no_apunta_a_un_cliente_ni_a_un_archivo_de_otro_tenant(
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    ajeno: str,
    restriccion: str,
) -> None:
    a, b = criaderos
    cliente = b.inscripto if ajeno == "cliente" else a.inscripto
    archivo = b.archivos[0] if ajeno == "archivo" else a.archivos[0]

    with pytest.raises(IntegrityError, match=restriccion):
        _insertar(motor_owner, a, cliente, archivo)
