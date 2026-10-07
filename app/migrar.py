"""Lambda de migraciones de Malka Suite.

Usa la misma imagen que la API, con otro handler (image_config.command en
infra/migraciones.tf). Corre dentro de la VPC y aplica las migraciones de
Alembic contra RDS con las credenciales del secreto indicado en DB_SECRET_NAME.

Despues de cada upgrade deja lista la clave del rol de aplicacion (#19): la
genera la primera vez y la guarda en el secreto DB_APP_SECRET_NAME; las
siguientes, la vuelve a copiar desde ese secreto. La clave no pasa por
Terraform, asi que no queda en claro en el tfstate (ver ADR 0003 y 0009).

Eventos aceptados:
    {"accion": "upgrade", "revision": "head"}
    {"accion": "downgrade", "revision": "-1"}
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from psycopg import sql
from sqlalchemy import Connection, create_engine, pool, text
from sqlalchemy.engine import URL

from app.config import obtener_url_base_de_datos

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# En Lambda el codigo vive en LAMBDA_TASK_ROOT; en local, en la raiz del repo.
RAIZ = Path(os.getenv("LAMBDA_TASK_ROOT", str(Path(__file__).resolve().parent.parent)))

# Mismo nombre que crea la migracion 0002.
ROL_APP = "malka_app"


def validar_evento(evento: dict[str, Any]) -> tuple[str, str]:
    """Devuelve la accion y la revision, o falla antes de tocar la base."""
    accion = str(evento.get("accion", "upgrade"))
    if accion not in ("upgrade", "downgrade"):
        raise ValueError(f"accion invalida: {accion!r}. Usar 'upgrade' o 'downgrade'.")

    # upgrade va a la ultima version si no se indica otra. downgrade exige
    # una revision explicita, para no deshacer nada por accidente.
    revision = evento.get("revision", "head" if accion == "upgrade" else None)
    if not isinstance(revision, str) or not revision:
        raise ValueError("downgrade necesita una revision explicita, por ejemplo '-1'.")

    return accion, revision


def revision_actual() -> str | None:
    """Consulta en la base que version del esquema esta aplicada."""
    motor = create_engine(obtener_url_base_de_datos(), poolclass=pool.NullPool)
    try:
        with motor.connect() as conexion:
            return MigrationContext.configure(conexion).get_current_revision()
    finally:
        motor.dispose()


def fijar_clave_de_rol(conexion: Connection, usuario: str, clave: str) -> None:
    """Fija la clave de un rol mandando el hash SCRAM, nunca la clave en texto.

    ALTER ROLE no acepta parametros ligados, y calcular el hash del lado del
    cliente evita que la clave quede en los logs de RDS si la sentencia se
    registra.
    """
    pg = conexion.connection.driver_connection
    if not isinstance(pg, psycopg.Connection):
        raise RuntimeError("fijar_clave_de_rol necesita una conexion de psycopg")
    verificador = pg.pgconn.encrypt_password(
        clave.encode(), usuario.encode(), b"scram-sha-256"
    ).decode()
    sentencia = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
        sql.Identifier(usuario), sql.Literal(verificador)
    )
    with pg.cursor() as cursor:
        cursor.execute(sentencia)


def _cargar_clave_rol_app(
    conexion: Connection, nombre_secreto: str, url_owner: URL
) -> str:
    """Copia la clave desde el secreto, o la genera si el secreto esta vacio."""
    # boto3 viene en la imagen de Lambda, no en local ni en el CI.
    cliente = importlib.import_module("boto3").client("secretsmanager")
    try:
        respuesta = cliente.get_secret_value(SecretId=nombre_secreto)
    except cliente.exceptions.ResourceNotFoundException:
        # Primer despliegue: Terraform crea el secreto sin valor.
        clave = secrets.token_urlsafe(32)
        fijar_clave_de_rol(conexion, ROL_APP, clave)
        # Se guarda dentro de la transaccion: si falla, el ALTER ROLE se
        # deshace y la base y el secreto no quedan desparejos.
        cliente.put_secret_value(
            SecretId=nombre_secreto,
            SecretString=json.dumps(
                {
                    "username": ROL_APP,
                    "password": clave,
                    "engine": "postgres",
                    "host": url_owner.host,
                    "port": url_owner.port,
                    "dbname": url_owner.database,
                }
            ),
        )
        return "generada"

    clave = str(json.loads(respuesta["SecretString"])["password"])
    fijar_clave_de_rol(conexion, ROL_APP, clave)
    return "sincronizada"


def preparar_rol_app() -> str:
    """Deja la misma clave del rol de la API en PostgreSQL y en su secreto.

    Devuelve que hizo: "generada", "sincronizada", "rol_inexistente" o
    "sin_secreto_configurado". Nunca loguea ni devuelve la clave.
    """
    nombre_secreto = os.getenv("DB_APP_SECRET_NAME")
    if not nombre_secreto:
        return "sin_secreto_configurado"

    resultado = "rol_inexistente"
    url_owner = obtener_url_base_de_datos()
    motor = create_engine(url_owner, poolclass=pool.NullPool)
    try:
        with motor.begin() as conexion:
            existe = conexion.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :rol"), {"rol": ROL_APP}
            ).scalar()
            if existe:
                resultado = _cargar_clave_rol_app(conexion, nombre_secreto, url_owner)
    finally:
        motor.dispose()
    return resultado


def handler(event: dict[str, Any], context: object) -> dict[str, str | None]:
    """Aplica upgrade o downgrade y devuelve la version antes y despues."""
    accion, revision = validar_evento(event)

    config = Config(str(RAIZ / "alembic.ini"))
    config.attributes["configurar_logs"] = False

    anterior = revision_actual()
    if accion == "upgrade":
        command.upgrade(config, revision)
    else:
        command.downgrade(config, revision)
    actual = revision_actual()
    rol_app = preparar_rol_app() if accion == "upgrade" else "sin_cambios"

    logger.info(
        "migracion accion=%s revision=%s anterior=%s actual=%s rol_app=%s",
        accion,
        revision,
        anterior,
        actual,
        rol_app,
    )
    return {
        "accion": accion,
        "revision_anterior": anterior,
        "revision_actual": actual,
        "rol_app": rol_app,
    }
