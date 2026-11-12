"""Subida y descarga de documentos en S3 con URLs prefirmadas (#102).

La API nunca recibe ni entrega los bytes: firma una URL corta y el navegador
habla directo con S3. Una URL prefirmada hereda los permisos de quien la
firma: el rol de la Lambda solo puede PutObject/GetObject bajo documentos/
(infra/s3_documentos.tf).

boto3 viene en la imagen de Lambda, no en local ni en el CI: se carga solo
cuando hace falta (mismo criterio que app/config.py). Los tests inyectan un
Firmador (y un Subidor) propio.

La API tambien guarda un archivo que genera ella misma: el PDF del borrador
aprobado (#42), con put_object desde la Lambda.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import unicodedata
import uuid
from typing import Any, Protocol, cast

from app.errores import ErrorDeApi

logger = logging.getLogger("malka")

REGION = "us-east-1"

# Mismos que validarArchivo del frontend y que el CHECK de la migracion 0011.
TIPOS_PERMITIDOS = ("application/pdf", "image/jpeg", "image/png")
TAMANO_MAXIMO = 10 * 1024 * 1024  # 10 MB

# Cortas a proposito: alcanzan para subir un PDF y una URL filtrada caduca
# enseguida. Si vence, el frontend pide otra y reintenta una vez.
SEGUNDOS_SUBIDA = 300
SEGUNDOS_DESCARGA = 120

# Prefijo con retencion de 5 anios (lifecycle de infra/s3_documentos.tf).
PREFIJO = "documentos"

_NO_PERMITIDOS = re.compile(r"[^A-Za-z0-9._-]+")
MAXIMO_NOMBRE_EN_CLAVE = 80


class SubidaNoConfiguradaError(ErrorDeApi):
    """Falta el bucket en el entorno: es un problema de despliegue, no del pedido."""

    status_code = 503
    codigo_por_defecto = "subida_no_configurada"


class AlmacenamientoNoDisponibleError(ErrorDeApi):
    """S3 no respondio al guardar un archivo que genera la API (503)."""

    status_code = 503
    codigo_por_defecto = "almacenamiento_no_disponible"


class Subidor(Protocol):
    """Lo unico que se usa del cliente de S3 para guardar lo que genera la API."""

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> Any: ...


class Firmador(Protocol):
    """Lo unico que se usa del cliente de S3."""

    def generate_presigned_url(
        self, ClientMethod: str, Params: dict[str, Any], ExpiresIn: int
    ) -> str: ...


def bucket_documentos() -> str:
    """Nombre del bucket, que pasa Terraform a la Lambda (BUCKET_DOCUMENTOS)."""
    bucket = os.getenv("BUCKET_DOCUMENTOS")
    if not bucket:
        raise SubidaNoConfiguradaError(
            "La subida de documentos no esta configurada en este entorno."
        )
    return bucket


def firmador_s3() -> Firmador:
    """Cliente de S3 con firma v4 y URL del bucket en el dominio (virtual)."""
    boto3 = importlib.import_module("boto3")
    config = importlib.import_module("botocore.config")
    cliente = boto3.client(
        "s3",
        region_name=REGION,
        config=config.Config(
            signature_version="s3v4", s3={"addressing_style": "virtual"}
        ),
    )
    return cast(Firmador, cliente)


def subidor_s3() -> Subidor:
    """Cliente de S3 para put_object. Sale por el endpoint gateway de la VPC."""
    boto3 = importlib.import_module("boto3")
    return cast(Subidor, boto3.client("s3", region_name=REGION))


def subir_pdf(subidor: Subidor, clave: str, contenido: bytes) -> None:
    """Guarda un PDF generado por la API. Si S3 falla, 503 y nada cambia."""
    bucket = bucket_documentos()
    try:
        subidor.put_object(
            Bucket=bucket, Key=clave, Body=contenido, ContentType="application/pdf"
        )
    except Exception as error:
        # Sin el mensaje de AWS en la respuesta: queda en el log con su traza.
        logger.exception("no se pudo subir el PDF a S3 key=%s", clave)
        raise AlmacenamientoNoDisponibleError(
            "No se pudo guardar el PDF. Probá de nuevo en unos minutos."
        ) from error


def clave_pdf_borrador(tenant_id: uuid.UUID, borrador_id: uuid.UUID) -> str:
    """documentos/<tenant_id>/borradores/<borrador_id>.pdf (CHECK de la 0018)."""
    return f"{PREFIJO}/{tenant_id}/borradores/{borrador_id}.pdf"


def nombre_para_clave(nombre: str) -> str:
    """El nombre del archivo, sin carpetas ni caracteres raros, para la key.

    "../Factura E Nº 41.pdf" -> "Factura-E-No-41.pdf". El nombre original se
    guarda aparte, tal cual, para mostrarlo.
    """
    base = nombre.replace("\\", "/").rsplit("/", 1)[-1]
    sin_acentos = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    limpio = _NO_PERMITIDOS.sub("-", sin_acentos).strip("-.")
    return limpio[-MAXIMO_NOMBRE_EN_CLAVE:] or "documento"


def armar_clave(tenant_id: uuid.UUID, nombre: str) -> str:
    """documentos/<tenant_id>/<uuid>-<nombre>: unica y del criadero."""
    return f"{PREFIJO}/{tenant_id}/{uuid.uuid4()}-{nombre_para_clave(nombre)}"


def firmar_subida(firmador: Firmador, clave: str, tipo_contenido: str) -> str:
    """URL de PUT. El Content-Type queda firmado: el navegador tiene que
    mandar el mismo, asi no se puede subir otra cosa con esta URL."""
    return firmador.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket_documentos(),
            "Key": clave,
            "ContentType": tipo_contenido,
        },
        ExpiresIn=SEGUNDOS_SUBIDA,
    )


def firmar_descarga(
    firmador: Firmador, clave: str, nombre: str, *, en_linea: bool = False
) -> str:
    """URL de GET con el nombre original del archivo.

    Por defecto el navegador lo baja (attachment); en_linea lo muestra en la
    pestaña (inline), para "Abrir" un PDF sin descargarlo.
    """
    disposicion = "inline" if en_linea else "attachment"
    return firmador.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket_documentos(),
            "Key": clave,
            "ResponseContentDisposition": (
                f'{disposicion}; filename="{nombre_para_clave(nombre)}"'
            ),
        },
        ExpiresIn=SEGUNDOS_DESCARGA,
    )
