"""Aprobacion y descarte de borradores, con bitacora y PDF interno (#42, #43).

DoD del #42 (corregido el 17/10): la bitacora registra quien, cuando y sobre
que version se aprobo; un borrador aprobado no se aprueba ni se edita otra
vez; el PDF es el del borrador interno, nunca un comprobante fiscal, y el
comprobante transcripto (#108) solo se referencia.

Contra PostgreSQL real con RLS (migracion 0018). S3 siempre falso: ninguna
llamada de red real (#43).
"""

from __future__ import annotations

import json
import re
import uuid
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import obtener_sesion
from app.errores import registrar_manejadores
from app.main import app
from app.routers.borradores import obtener_cola, obtener_subidor
from app.routers.documentos import obtener_firmador
from app.services.ia.snapshot import sha256_canonico
from tests.conftest import borrar_borradores, crear_tenant, emitir, fijar_tenant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cognito", "con_motor_app", "bucket"),
]

URL = "/api/v1/documentos/borradores"
BUCKET = "bucket-de-prueba"

CONTENIDO = {
    "encabezado": "Nota de venta de reinas a Apícola del Sur",
    "cuerpo": (
        "PARTES\n"
        "El criadero vende a Apícola del Sur, país AR.\n\n"
        "OBJETO\n"
        "Se venden 20 reinas de raza Carniola (ítem Reina fecundada) — "
        "y 5 reinas más.\n\n"
        "CIERRE\n"
        "Queda sujeto a confirmación."
    ),
    "notas": "Recibirlas apenas lleguen…",
}


def _hash(contenido: dict[str, Any]) -> str:
    return sha256_canonico(contenido)


HASH = _hash(CONTENIDO)

# De hijas a padres, para limpiar sin chocar con las claves foraneas.
TABLAS = (
    "comprobante",
    "documento",
    "llamada_ia",
    "pedido_item",
    "pedido",
    "item_vendible",
    "raza",
    "cliente",
    "idempotencia",
)


@dataclass(frozen=True)
class Datos:
    tenant: uuid.UUID
    cliente: uuid.UUID
    pedido: uuid.UUID
    otro_pedido: uuid.UUID
    comprobante: uuid.UUID
    comprobante_de_otro_cliente: uuid.UUID


def _id(c: Connection, sql: str, **params: Any) -> uuid.UUID:
    valor = c.execute(text(sql), params).scalar_one()
    assert isinstance(valor, uuid.UUID)
    return valor


def _cliente(c: Connection, nombre: str, cuit: str) -> uuid.UUID:
    return _id(
        c,
        "INSERT INTO cliente (nombre, pais, tipo, condicion_iva, cuit_o_tax_id) "
        "VALUES (:nombre, 'AR', 'nacional', 'monotributo', :cuit) RETURNING id",
        nombre=nombre,
        cuit=cuit,
    )


def _pedido(c: Connection, cliente: uuid.UUID, item: uuid.UUID, raza: uuid.UUID) -> Any:
    pedido = _id(
        c,
        "INSERT INTO pedido (cliente_id, moneda, fecha_entrega_solicitada) "
        "VALUES (:cliente, 'ARS', '2026-11-20') RETURNING id",
        cliente=cliente,
    )
    for numero, cantidad, precio, alicuota in (
        (1, 20, "35000.50", "0.21"),
        (2, 5, "1000.25", "0.105"),
    ):
        c.execute(
            text(
                "INSERT INTO pedido_item (pedido_id, numero_linea, item_vendible_id, "
                "raza_id, cantidad, precio_unitario, alicuota) "
                "VALUES (:pedido, :numero, :item, :raza, :cantidad, :precio, "
                ":alicuota)"
            ),
            {
                "pedido": pedido,
                "numero": numero,
                "item": item,
                "raza": raza,
                "cantidad": cantidad,
                "precio": precio,
                "alicuota": alicuota,
            },
        )
    return pedido


def _comprobante(
    c: Connection, tenant: uuid.UUID, cliente: uuid.UUID, numero: int
) -> uuid.UUID:
    clave = f"documentos/{tenant}/factura-{numero}.pdf"
    c.execute(
        text(
            "INSERT INTO documento (archivo_s3_key, nombre, tipo_contenido, "
            "tamano_bytes, subido_por) VALUES (:clave, 'factura.pdf', "
            "'application/pdf', 1024, 'sub-de-prueba')"
        ),
        {"clave": clave},
    )
    return _id(
        c,
        "INSERT INTO comprobante (tipo, cliente_id, punto_venta, numero, "
        "fecha_emision, moneda, importe, tipo_cambio, archivo_s3_key, cargado_por) "
        "VALUES ('B', :cliente, 3, :numero, '2026-10-20', 'ARS', 852538.48, NULL, "
        ":clave, 'sub-de-prueba') RETURNING id",
        cliente=cliente,
        numero=numero,
        clave=clave,
    )


def _cargar(c: Connection, tenant: uuid.UUID) -> Datos:
    crear_tenant(c, tenant)
    fijar_tenant(c, tenant)
    cliente = _cliente(c, "Apícola del Sur", "20123456786")
    otro_cliente = _cliente(c, "Miel del Norte", "20333333334")
    item = _id(
        c, "INSERT INTO item_vendible (nombre) VALUES ('Reina fecundada') RETURNING id"
    )
    raza = _id(c, "INSERT INTO raza (nombre) VALUES ('Carniola') RETURNING id")
    return Datos(
        tenant=tenant,
        cliente=cliente,
        pedido=_pedido(c, cliente, item, raza),
        otro_pedido=_pedido(c, cliente, item, raza),
        comprobante=_comprobante(c, tenant, cliente, 45),
        comprobante_de_otro_cliente=_comprobante(c, tenant, otro_cliente, 46),
    )


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
            borrar_borradores(c)
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
            borrar_borradores(c)
            for tabla in ("llamada_ia", "idempotencia"):
                c.execute(text(f"DELETE FROM {tabla} WHERE true"))  # noqa: S608


# --- dobles de AWS ---


class S3Falso:
    """Doble de put_object y de la firma: guarda lo que se sube y firma."""

    def __init__(self) -> None:
        self.subidos: list[dict[str, Any]] = []
        self.firmas: list[dict[str, Any]] = []

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> dict[str, Any]:
        self.subidos.append(
            {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
        )
        return {}

    def generate_presigned_url(
        self, ClientMethod: str, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        self.firmas.append(Params)
        return f"https://s3.test/{Params['Key']}?firma={len(self.firmas)}"


class S3Caido:
    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("S3 no responde")


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUCKET_DOCUMENTOS", BUCKET)


@pytest.fixture
def s3() -> Iterator[S3Falso]:
    falso = S3Falso()
    app.dependency_overrides[obtener_subidor] = lambda: falso
    app.dependency_overrides[obtener_firmador] = lambda: falso
    yield falso
    app.dependency_overrides.pop(obtener_subidor, None)
    app.dependency_overrides.pop(obtener_firmador, None)


@pytest.fixture
def api(s3: S3Falso) -> TestClient:
    return TestClient(app)


def como(tenant: uuid.UUID, rol: str = "admin") -> dict[str, str]:
    token = emitir(**{"custom:tenant_id": str(tenant), "cognito:groups": [rol]})
    return {"Authorization": f"Bearer {token}"}


def crear_borrador(
    motor_owner: Engine,
    datos: Datos,
    estado: str = "borrador",
    *,
    pedido: uuid.UUID | None = None,
) -> str:
    """Un borrador como lo deja el worker (o ya revisado), cargado como duenio."""
    contenido = None if estado in ("generando", "rechazado") else CONTENIDO
    with motor_owner.begin() as c:
        fijar_tenant(c, datos.tenant)
        borrador = _id(
            c,
            "INSERT INTO borrador (pedido_id, estado, contenido, motivo_rechazo, "
            "motivo_descarte, modelo, version_prompt, solicitado_por) "
            "VALUES (:pedido, :estado, CAST(:contenido AS jsonb), :rechazo, "
            ":descarte, 'modelo-de-prueba', 'borrador_v3', 'sub-de-prueba') "
            "RETURNING id",
            pedido=pedido or datos.pedido,
            estado=estado,
            contenido=None if contenido is None else json.dumps(contenido),
            rechazo="salida_invalida" if estado == "rechazado" else None,
            descarte="No sirve" if estado == "descartado" else None,
        )
    return str(borrador)


def aprobar(
    api: TestClient,
    datos: Datos,
    borrador: str,
    *,
    rol: str = "admin",
    **cuerpo: Any,
) -> Any:
    clave = cuerpo.pop("clave", None)
    headers = como(datos.tenant, rol)
    if clave is not None:
        headers["Idempotency-Key"] = clave
    return api.post(
        f"{URL}/{borrador}/aprobar",
        json={"hash_contenido": HASH, **cuerpo},
        headers=headers,
    )


def leer(motor_owner: Engine, datos: Datos, sql: str, **params: Any) -> list[Any]:
    with motor_owner.begin() as c:
        fijar_tenant(c, datos.tenant)
        return list(c.execute(text(sql), params).all())


def estado_de(motor_owner: Engine, datos: Datos, borrador: str) -> str:
    [fila] = leer(
        motor_owner, datos, "SELECT estado FROM borrador WHERE id = :id", id=borrador
    )
    return str(fila.estado)


def revisiones_de(motor_owner: Engine, datos: Datos, borrador: str) -> list[Any]:
    return leer(
        motor_owner,
        datos,
        "SELECT * FROM revision_borrador WHERE borrador_id = :id",
        id=borrador,
    )


def texto_del_pdf(pdf: bytes) -> str:
    """El texto de los Tj del PDF (fpdf2 comprime cada pagina con zlib)."""
    paginas = [
        zlib.decompress(m.group(1))
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S)
    ]
    cadenas = re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", b"\n".join(paginas))
    return "\n".join(
        re.sub(rb"\\(.)", rb"\1", cadena).decode("latin-1") for cadena in cadenas
    )


# --- aprobar ---


def test_admin_aprueba_y_congela_exactamente_lo_aprobado(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = aprobar(api, datos, borrador, comprobante_id=str(datos.comprobante))

    assert respuesta.status_code == 200, respuesta.json()
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "aprobado"
    assert cuerpo["hash_contenido"] == HASH
    assert cuerpo["aprobado_por"] == "usuario-1"
    assert cuerpo["comprobante_id"] == str(datos.comprobante)
    assert cuerpo["pdf_disponible"] is True
    # La respuesta no lleva URL prefirmada: venceria guardada en idempotencia.
    assert "url" not in json.dumps(cuerpo)

    [fila] = leer(
        motor_owner,
        datos,
        "SELECT snapshot, hash_contenido, pdf_s3_key FROM borrador WHERE id = :id",
        id=borrador,
    )
    snapshot = fila.snapshot
    assert snapshot["aprobacion"]["momento"]
    assert snapshot == {
        "version": 1,
        "borrador": {
            "id": borrador,
            "modelo": "modelo-de-prueba",
            "version_prompt": "borrador_v3",
            "corrige_a": None,
            "contenido": CONTENIDO,
        },
        "pedido": {
            "id": str(datos.pedido),
            "cliente": {
                "id": str(datos.cliente),
                "nombre": "Apícola del Sur",
                "pais": "AR",
                "tipo": "nacional",
                "condicion_iva": "monotributo",
                "cuit_o_tax_id": "20123456786",
            },
            "moneda": "ARS",
            "fecha_entrega_solicitada": "2026-11-20",
            "destino_pais": None,
            "incoterm": None,
            "lineas": [
                {
                    "numero_linea": 1,
                    "cantidad": 20,
                    "item": "Reina fecundada",
                    "raza": "Carniola",
                    "precio_unitario": "35000.50",
                    "alicuota": "0.21",
                    "neto": "700010.00",
                },
                {
                    "numero_linea": 2,
                    "cantidad": 5,
                    "item": "Reina fecundada",
                    "raza": "Carniola",
                    "precio_unitario": "1000.25",
                    "alicuota": "0.105",
                    "neto": "5001.25",
                },
            ],
            # Los calcula el sistema (ADR 0008): IVA por alicuota, HALF_UP.
            "totales": {
                "exportacion": False,
                "por_alicuota": [
                    {"alicuota": "0.105", "neto": "5001.25", "iva": "525.13"},
                    {"alicuota": "0.21", "neto": "700010.00", "iva": "147002.10"},
                ],
                "neto": "705011.25",
                "iva": "147527.23",
                "total": "852538.48",
            },
        },
        # Tal como se transcribio (#108), sin nada calculado.
        "comprobante": {
            "id": str(datos.comprobante),
            "tipo": "B",
            "cliente_id": str(datos.cliente),
            "punto_venta": 3,
            "numero": 45,
            "fecha_emision": "2026-10-20",
            "moneda": "ARS",
            "importe": "852538.48",
            "tipo_cambio": None,
            "archivo_s3_key": f"documentos/{datos.tenant}/factura-45.pdf",
            "cargado_por": "sub-de-prueba",
            "creado_en": snapshot["comprobante"]["creado_en"],
        },
        "aprobacion": {
            "usuario": "usuario-1",
            "momento": snapshot["aprobacion"]["momento"],
            "hash_contenido": HASH,
        },
    }
    assert fila.hash_contenido == HASH
    assert fila.pdf_s3_key == f"documentos/{datos.tenant}/borradores/{borrador}.pdf"


def test_la_bitacora_registra_quien_cuando_y_sobre_que_version(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    """DoD del #42."""
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    aprobado = aprobar(api, datos, borrador, comprobante_id=str(datos.comprobante))

    respuesta = api.get(
        f"{URL}/{borrador}/revisiones", headers=como(datos.tenant, "lectura")
    )

    assert respuesta.status_code == 200
    [revision] = respuesta.json()
    assert revision["accion"] == "aprobado"
    assert revision["usuario"] == "usuario-1"
    assert revision["hash_contenido"] == HASH
    assert revision["version_prompt"] == "borrador_v3"
    assert revision["comprobante_id"] == str(datos.comprobante)
    assert revision["motivo"] is None
    [fila] = leer(
        motor_owner, datos, "SELECT snapshot FROM borrador WHERE id = :id", id=borrador
    )
    assert revision["hash_snapshot"] == sha256_canonico(fila.snapshot)
    # El mismo instante en la bitacora, en el snapshot y en la respuesta.
    assert (
        revision["momento"].replace("Z", "+00:00")
        == fila.snapshot["aprobacion"]["momento"]
    )
    assert aprobado.json()["aprobado_en"] == revision["momento"]


def test_aprobar_sin_comprobante_lo_deja_sin_referencia(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = aprobar(api, datos, borrador)

    assert respuesta.status_code == 200, respuesta.json()
    assert respuesta.json()["comprobante_id"] is None
    [fila] = leer(
        motor_owner, datos, "SELECT snapshot FROM borrador WHERE id = :id", id=borrador
    )
    assert fila.snapshot["comprobante"] is None


@pytest.mark.parametrize("rol", ["ventas", "produccion", "lectura"])
def test_solo_admin_aprueba(
    api: TestClient,
    s3: S3Falso,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    rol: str,
) -> None:
    """Nunca auto-aprobacion ni otro rol (dominio, "Aprobacion de borradores")."""
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = aprobar(api, datos, borrador, rol=rol)

    assert respuesta.status_code == 403
    assert estado_de(motor_owner, datos, borrador) == "borrador"
    assert s3.subidos == []


def test_un_aprobado_no_se_aprueba_dos_veces(
    api: TestClient, s3: S3Falso, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    """DoD del #42 y del #43: doble aprobacion rechazada."""
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    assert aprobar(api, datos, borrador).status_code == 200

    segunda = aprobar(api, datos, borrador)

    assert segunda.status_code == 409
    error = segunda.json()["error"]
    assert error["code"] == "transicion_invalida"
    assert error["details"] == {"desde": "aprobado", "hacia": "aprobado"}
    assert len(s3.subidos) == 1
    assert len(revisiones_de(motor_owner, datos, borrador)) == 1


@pytest.mark.parametrize("estado", ["descartado", "generando", "rechazado"])
def test_solo_se_aprueba_un_borrador_en_revision(
    api: TestClient,
    s3: S3Falso,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    estado: str,
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos, estado)

    respuesta = aprobar(api, datos, borrador)

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "transicion_invalida"
    assert respuesta.json()["error"]["details"]["desde"] == estado
    assert s3.subidos == []


def test_con_un_hash_viejo_no_se_aprueba(
    api: TestClient, s3: S3Falso, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    """Se aprueba la version que se vio, no otra."""
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    viejo = _hash({**CONTENIDO, "notas": "otra version"})

    respuesta = aprobar(api, datos, borrador, hash_contenido=viejo)

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "version_desactualizada"
    assert estado_de(motor_owner, datos, borrador) == "borrador"
    assert s3.subidos == []


@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param({}, id="sin_hash"),
        pytest.param({"hash_contenido": "abc"}, id="hash_mal_formado"),
        pytest.param({"hash_contenido": HASH, "estado": "aprobado"}, id="campo_extra"),
    ],
)
def test_el_cuerpo_de_aprobar_se_valida(
    api: TestClient,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    cuerpo: dict[str, Any],
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = api.post(
        f"{URL}/{borrador}/aprobar", json=cuerpo, headers=como(datos.tenant)
    )

    assert respuesta.status_code == 422


@pytest.mark.parametrize(
    ("comprobante", "code"),
    [
        ("de_otro_cliente", "comprobante_de_otro_cliente"),
        ("de_otro_criadero", "comprobante_inexistente"),
        ("inexistente", "comprobante_inexistente"),
    ],
)
def test_el_comprobante_tiene_que_ser_del_cliente_y_del_criadero(
    api: TestClient,
    s3: S3Falso,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    comprobante: str,
    code: str,
) -> None:
    a, b = criaderos
    borrador = crear_borrador(motor_owner, a)
    comprobante_id = {
        "de_otro_cliente": a.comprobante_de_otro_cliente,
        "de_otro_criadero": b.comprobante,
        "inexistente": uuid.uuid4(),
    }[comprobante]

    respuesta = aprobar(api, a, borrador, comprobante_id=str(comprobante_id))

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == code
    assert estado_de(motor_owner, a, borrador) == "borrador"
    assert s3.subidos == []


# --- PDF ---


def test_el_pdf_interno_se_sube_con_su_clave_y_su_tipo(
    api: TestClient, s3: S3Falso, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    aprobar(api, datos, borrador, comprobante_id=str(datos.comprobante))

    [subido] = s3.subidos
    assert subido["Bucket"] == BUCKET
    assert subido["Key"] == f"documentos/{datos.tenant}/borradores/{borrador}.pdf"
    assert subido["ContentType"] == "application/pdf"
    pdf = subido["Body"]
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    texto = texto_del_pdf(pdf)
    for esperado in (
        "Nota de venta de reinas a Apícola del Sur",
        # Los titulos del cuerpo y la raya pasada a latin-1.
        "PARTES",
        "(ítem Reina fecundada) - y 5 reinas más.",
        "Detalle del pedido (calculado por el sistema)",
        "700010.00",
        "ARS 147527.23",
        "ARS 852538.48",
        "Recibirlas apenas lleguen...",
        # El comprobante referenciado, con el relleno de impresion.
        "Factura B 0003-00000045 del 20/10/2026",
        f"versión {HASH[:12]}",
        "No es un comprobante fiscal",
        "aprobado por usuario-1 el",
    ):
        assert esperado in texto


def test_si_s3_falla_es_503_y_no_cambia_nada(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    clave = str(uuid.uuid4())
    app.dependency_overrides[obtener_subidor] = S3Caido

    respuesta = aprobar(api, datos, borrador, clave=clave)

    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["code"] == "almacenamiento_no_disponible"
    assert "S3 no responde" not in respuesta.text
    assert estado_de(motor_owner, datos, borrador) == "borrador"
    assert revisiones_de(motor_owner, datos, borrador) == []
    # La clave no quedo tomada: con S3 de vuelta, el reintento aprueba.
    app.dependency_overrides.pop(obtener_subidor)
    falso = S3Falso()
    app.dependency_overrides[obtener_subidor] = lambda: falso
    reintento = aprobar(api, datos, borrador, clave=clave)
    assert reintento.status_code == 200, reintento.json()
    assert len(falso.subidos) == 1


def test_la_misma_clave_aprueba_y_sube_una_sola_vez(
    api: TestClient, s3: S3Falso, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    clave = str(uuid.uuid4())

    primera = aprobar(api, datos, borrador, clave=clave)
    segunda = aprobar(api, datos, borrador, clave=clave)

    assert primera.status_code == segunda.status_code == 200
    assert primera.json() == segunda.json()
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert len(s3.subidos) == 1
    assert len(revisiones_de(motor_owner, datos, borrador)) == 1


def test_sin_bucket_configurado_no_se_aprueba(
    api: TestClient,
    s3: S3Falso,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    monkeypatch.delenv("BUCKET_DOCUMENTOS")

    respuesta = aprobar(api, datos, borrador)

    assert respuesta.status_code == 503
    assert respuesta.json()["error"]["code"] == "subida_no_configurada"
    assert estado_de(motor_owner, datos, borrador) == "borrador"


@pytest.mark.parametrize(
    ("en_linea", "disposicion"), [(False, "attachment"), (True, "inline")]
)
def test_el_pdf_se_baja_con_una_url_nueva_cada_vez(
    api: TestClient,
    s3: S3Falso,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    en_linea: bool,
    disposicion: str,
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    aprobar(api, datos, borrador)
    ruta = f"{URL}/{borrador}/pdf"
    parametros = {"en_linea": str(en_linea).lower()}

    una = api.get(ruta, params=parametros, headers=como(datos.tenant, "lectura"))
    otra = api.get(ruta, params=parametros, headers=como(datos.tenant, "lectura"))

    assert una.status_code == otra.status_code == 200
    assert una.json()["url_descarga"] != otra.json()["url_descarga"]
    assert una.json()["expira_en_segundos"] == 120
    firma = s3.firmas[-1]
    assert firma["Key"] == f"documentos/{datos.tenant}/borradores/{borrador}.pdf"
    assert firma["ResponseContentDisposition"].startswith(disposicion)


def test_sin_aprobar_no_hay_pdf(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = api.get(f"{URL}/{borrador}/pdf", headers=como(datos.tenant))
    inexistente = api.get(f"{URL}/{uuid.uuid4()}/pdf", headers=como(datos.tenant))

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "pdf_no_disponible"
    assert inexistente.status_code == 404
    assert inexistente.json()["error"]["code"] == "borrador_no_encontrado"


# --- descartar ---


@pytest.mark.parametrize("rol", ["admin", "ventas"])
def test_admin_y_ventas_descartan_con_motivo(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos], rol: str
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = api.post(
        f"{URL}/{borrador}/rechazar",
        json={"motivo": "  Falta la raza en el objeto  "},
        headers=como(datos.tenant, rol),
    )

    assert respuesta.status_code == 200, respuesta.json()
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "descartado"
    assert cuerpo["motivo_descarte"] == "Falta la raza en el objeto"
    # El contenido queda: se descarto algo que existia (no es un rechazado).
    assert cuerpo["contenido"] == CONTENIDO
    assert cuerpo["pdf_disponible"] is False
    [revision] = revisiones_de(motor_owner, datos, borrador)
    assert (revision.accion, revision.motivo) == (
        "descartado",
        "Falta la raza en el objeto",
    )
    assert revision.hash_contenido == HASH
    assert revision.hash_snapshot is None


@pytest.mark.parametrize("rol", ["produccion", "lectura"])
def test_produccion_y_lectura_no_descartan(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos], rol: str
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = api.post(
        f"{URL}/{borrador}/rechazar",
        json={"motivo": "No sirve"},
        headers=como(datos.tenant, rol),
    )

    assert respuesta.status_code == 403
    assert estado_de(motor_owner, datos, borrador) == "borrador"


@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param({}, id="sin_motivo"),
        pytest.param({"motivo": "   "}, id="en_blanco"),
        pytest.param({"motivo": " ab "}, id="corto"),
        pytest.param({"motivo": "x" * 501}, id="largo"),
        pytest.param({"motivo": "con\x00nul"}, id="control"),
    ],
)
def test_descartar_exige_un_motivo_valido(
    api: TestClient,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    cuerpo: dict[str, Any],
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    respuesta = api.post(
        f"{URL}/{borrador}/rechazar", json=cuerpo, headers=como(datos.tenant)
    )

    assert respuesta.status_code == 422
    assert estado_de(motor_owner, datos, borrador) == "borrador"


def test_un_aprobado_no_se_descarta(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    aprobar(api, datos, borrador)

    respuesta = api.post(
        f"{URL}/{borrador}/rechazar",
        json={"motivo": "Me arrepenti"},
        headers=como(datos.tenant),
    )

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["details"] == {
        "desde": "aprobado",
        "hacia": "descartado",
    }


def test_la_misma_clave_descarta_una_sola_vez(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    headers = {**como(datos.tenant, "ventas"), "Idempotency-Key": str(uuid.uuid4())}
    ruta = f"{URL}/{borrador}/rechazar"

    primera = api.post(ruta, json={"motivo": "No sirve"}, headers=headers)
    segunda = api.post(ruta, json={"motivo": "No sirve"}, headers=headers)

    assert primera.json() == segunda.json()
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert len(revisiones_de(motor_owner, datos, borrador)) == 1


# --- correccion ---


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


def test_la_correccion_es_un_borrador_nuevo_que_apunta_al_aprobado(
    api: TestClient,
    cola: ColaFalsa,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
) -> None:
    datos, _ = criaderos
    aprobado = crear_borrador(motor_owner, datos)
    antes = aprobar(api, datos, aprobado).json()

    respuesta = api.post(
        URL,
        json={"pedido_id": str(datos.pedido), "corrige_a": aprobado},
        headers=como(datos.tenant, "ventas"),
    )

    assert respuesta.status_code == 202, respuesta.json()
    nuevo = respuesta.json()
    assert nuevo["estado"] == "generando"
    assert nuevo["corrige_a"] == aprobado
    assert len(cola.mensajes) == 1
    # El aprobado sigue igual.
    despues = api.get(f"{URL}/{aprobado}", headers=como(datos.tenant)).json()
    assert despues == antes


@pytest.mark.parametrize(
    "caso", ["de_otro_pedido", "sin_aprobar", "de_otro_criadero", "inexistente"]
)
def test_solo_se_corrige_un_aprobado_del_mismo_pedido(
    api: TestClient,
    cola: ColaFalsa,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    caso: str,
) -> None:
    a, b = criaderos
    if caso == "de_otro_pedido":
        corregido = crear_borrador(motor_owner, a, pedido=a.otro_pedido)
        aprobar(api, a, corregido)
    elif caso == "sin_aprobar":
        corregido = crear_borrador(motor_owner, a)
    elif caso == "de_otro_criadero":
        corregido = crear_borrador(motor_owner, b)
        aprobar(api, b, corregido)
    else:
        corregido = str(uuid.uuid4())

    respuesta = api.post(
        URL,
        json={"pedido_id": str(a.pedido), "corrige_a": corregido},
        headers=como(a.tenant, "ventas"),
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "correccion_invalida"
    assert cola.mensajes == []


# --- aislamiento entre criaderos ---


@pytest.mark.parametrize(
    ("metodo", "sufijo", "cuerpo"),
    [
        ("POST", "aprobar", {"hash_contenido": HASH}),
        ("POST", "rechazar", {"motivo": "Robado"}),
        ("GET", "revisiones", None),
        ("GET", "pdf", None),
        ("GET", "", None),
    ],
)
def test_el_borrador_de_otro_criadero_responde_como_inexistente(
    api: TestClient,
    s3: S3Falso,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    metodo: str,
    sufijo: str,
    cuerpo: dict[str, Any] | None,
) -> None:
    a, b = criaderos
    de_a = crear_borrador(motor_owner, a)
    aprobar(api, a, de_a)
    subidos = len(s3.subidos)

    def pedir(borrador: str) -> Any:
        ruta = f"{URL}/{borrador}/{sufijo}".rstrip("/")
        return api.request(metodo, ruta, json=cuerpo, headers=como(b.tenant))

    ajeno, inexistente = pedir(de_a), pedir(str(uuid.uuid4()))

    assert ajeno.status_code == inexistente.status_code == 404
    assert {**ajeno.json()["error"], "request_id": None} == {
        **inexistente.json()["error"],
        "request_id": None,
    }
    assert len(s3.subidos) == subidos
    assert estado_de(motor_owner, a, de_a) == "aprobado"
    assert len(revisiones_de(motor_owner, a, de_a)) == 1


# --- la base: inmutabilidad y bitacora ---


@pytest.mark.parametrize("estado", ["aprobado", "descartado"])
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE borrador SET motivo_descarte = 'otro motivo' WHERE id = :id",
        "UPDATE borrador SET estado = 'borrador' WHERE id = :id",
        "DELETE FROM borrador WHERE id = :id",
    ],
)
def test_ni_el_duenio_modifica_un_borrador_revisado(
    api: TestClient,
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    estado: str,
    sql: str,
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    if estado == "aprobado":
        aprobar(api, datos, borrador)
    else:
        api.post(
            f"{URL}/{borrador}/rechazar",
            json={"motivo": "No sirve"},
            headers=como(datos.tenant),
        )

    with pytest.raises(DBAPIError, match="no se modifica ni se borra") as error:
        with motor_owner.begin() as c:
            fijar_tenant(c, datos.tenant)
            c.execute(text(sql), {"id": borrador})

    assert getattr(error.value.orig, "sqlstate", None) == "MK409"
    assert estado_de(motor_owner, datos, borrador) == estado


def test_si_un_update_llega_a_un_aprobado_la_api_responde_409(
    api: TestClient, motor_owner: Engine, criaderos: tuple[Datos, Datos]
) -> None:
    """El trigger es la ultima barrera: su error se traduce, no es un 500."""
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)
    aprobar(api, datos, borrador)
    prueba = FastAPI()
    registrar_manejadores(prueba)

    @prueba.post("/pisar")
    def pisar(sesion: Session = Depends(obtener_sesion)) -> None:
        sesion.execute(
            text("UPDATE borrador SET estado = 'borrador' WHERE id = :id"),
            {"id": borrador},
        )

    respuesta = TestClient(prueba).post("/pisar", headers=como(datos.tenant))

    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "borrador_inmutable"


def test_la_bitacora_no_se_edita_ni_se_borra(motor_owner: Engine) -> None:
    privilegio = text("SELECT has_table_privilege('malka_app', :tabla, :permiso)")
    with motor_owner.connect() as c:
        permisos = {
            permiso: c.execute(
                privilegio, {"tabla": "revision_borrador", "permiso": permiso}
            ).scalar()
            for permiso in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        sin_delete_borrador = not c.execute(
            privilegio, {"tabla": "borrador", "permiso": "DELETE"}
        ).scalar()

    assert permisos == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
    }
    assert sin_delete_borrador


@pytest.mark.parametrize(
    ("sql", "restriccion"),
    [
        pytest.param(
            "UPDATE borrador SET estado = 'aprobado' WHERE id = :id",
            "ck_borrador_aprobado",
            id="aprobado_sin_snapshot",
        ),
        pytest.param(
            "UPDATE borrador SET estado = 'descartado' WHERE id = :id",
            "ck_borrador_descartado",
            id="descartado_sin_motivo",
        ),
        pytest.param(
            "UPDATE borrador SET corrige_a = id WHERE id = :id",
            "ck_borrador_no_se_corrige_a_si_mismo",
            id="se_corrige_a_si_mismo",
        ),
    ],
)
def test_checks_de_la_aprobacion(
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    sql: str,
    restriccion: str,
) -> None:
    datos, _ = criaderos
    borrador = crear_borrador(motor_owner, datos)

    with pytest.raises(DBAPIError, match=restriccion):
        with motor_owner.begin() as c:
            fijar_tenant(c, datos.tenant)
            c.execute(text(sql), {"id": borrador})


@pytest.mark.parametrize(
    ("clave", "hash_contenido", "restriccion"),
    [
        pytest.param(
            "documentos/{otro}/borradores/{id}.pdf",
            HASH,
            "ck_borrador_pdf_del_tenant",
            id="pdf_de_otro_criadero",
        ),
        pytest.param(
            "documentos/{tenant}/borradores/{otro}.pdf",
            HASH,
            "ck_borrador_pdf_del_tenant",
            id="pdf_de_otro_borrador",
        ),
        pytest.param(
            "documentos/{tenant}/borradores/{id}.pdf",
            "ABC",
            "ck_borrador_hash_contenido",
            id="hash_mal_formado",
        ),
    ],
)
def test_un_aprobado_guarda_su_pdf_y_un_hash_valido(
    motor_owner: Engine,
    criaderos: tuple[Datos, Datos],
    clave: str,
    hash_contenido: str,
    restriccion: str,
) -> None:
    datos, _ = criaderos
    borrador, otro = uuid.uuid4(), uuid.uuid4()
    clave = clave.format(tenant=datos.tenant, id=borrador, otro=otro)

    with pytest.raises(DBAPIError, match=restriccion):
        with motor_owner.begin() as c:
            fijar_tenant(c, datos.tenant)
            c.execute(
                text(
                    "INSERT INTO borrador (id, pedido_id, estado, contenido, "
                    "snapshot, hash_contenido, pdf_s3_key, solicitado_por) "
                    "VALUES (:id, :pedido, 'aprobado', '{}', '{}', :hash, :clave, "
                    "'sub')"
                ),
                {
                    "id": borrador,
                    "pedido": datos.pedido,
                    "hash": hash_contenido,
                    "clave": clave,
                },
            )
