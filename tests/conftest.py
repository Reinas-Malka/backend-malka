"""Fixtures compartidas por los tests."""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, create_engine, pool, text
from sqlalchemy.engine import URL, make_url
from testcontainers.postgres import PostgresContainer

from app import auth, db
from app.auth import obtener_config_cognito
from app.config import obtener_url_base_de_datos
from app.errores import ConflictoError, ValidacionError, registrar_manejadores
from app.migrar import fijar_clave_de_rol
from app.observabilidad import FormateadorJson, instalar_observabilidad


class Pedido(BaseModel):
    cantidad: int
    cuit: str


def crear_app_de_prueba() -> FastAPI:
    """Una app con la misma configuracion que la real y rutas que fallan a proposito."""
    app = FastAPI()
    registrar_manejadores(app)
    instalar_observabilidad(app)

    @app.get("/conflicto")
    def conflicto() -> None:
        raise ConflictoError(
            "La tanda ya esta cerrada.",
            code="tanda_cerrada",
            details={"tanda_id": 7},
        )

    @app.get("/regla")
    def regla() -> None:
        raise ValidacionError("La fecha de traslarve es posterior a la de fecundacion.")

    @app.post("/pedidos")
    def pedidos(pedido: Pedido) -> Pedido:
        return pedido

    @app.get("/explota")
    def explota() -> None:
        raise RuntimeError("detalle interno que no debe salir")

    @app.get("/tandas/{tanda_id}")
    def tanda(tanda_id: int) -> dict[str, int]:
        return {"tanda_id": tanda_id}

    return app


@pytest.fixture
def cliente() -> TestClient:
    return TestClient(crear_app_de_prueba())


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    """Captura lo que el logger `malka` escribe, ya formateado en JSON."""
    salida = io.StringIO()
    handler = logging.StreamHandler(salida)
    handler.setFormatter(FormateadorJson())
    logger = logging.getLogger("malka")
    logger.addHandler(handler)
    yield salida
    logger.removeHandler(handler)


@pytest.fixture
def config_limpia() -> Iterator[None]:
    """La URL de la base se guarda en cache: se limpia antes y despues del test."""
    obtener_url_base_de_datos.cache_clear()
    yield
    obtener_url_base_de_datos.cache_clear()


RAIZ = Path(__file__).resolve().parent.parent


def crear_tenant(conexion: Connection, tenant: uuid.UUID) -> None:
    """Unico lugar donde los tests crean un tenant.

    Si la tabla suma columnas obligatorias (por ejemplo cuit, en la #26), se
    actualiza solo aca.
    """
    conexion.execute(
        text("INSERT INTO tenant (id, nombre, cuit) VALUES (:id, :nombre, :cuit)"),
        {
            "id": tenant,
            "nombre": f"Criadero {tenant}",
            # 11 digitos derivados del id: distinto para cada tenant de prueba.
            "cuit": f"{tenant.int % 10**11:011d}",
        },
    )


def fijar_tenant(conexion: Connection, tenant: uuid.UUID) -> None:
    """SET LOCAL del tenant en una conexion de prueba."""
    conexion.execute(db.FIJAR_TENANT, {"tenant_id": str(tenant)})


@dataclass(frozen=True)
class BaseDePrueba:
    """PostgreSQL 16 real con las migraciones aplicadas, armado como en RDS."""

    url_owner: URL
    url_app: URL


@pytest.fixture(scope="session")
def base_de_prueba() -> Iterator[BaseDePrueba]:
    """Levanta Postgres con testcontainers (SQLite no tiene RLS).

    En local, sin Docker, los tests de integracion se saltean. En el CI
    (CI=true) fallan: ahi Docker tiene que estar.
    """
    try:
        contenedor = PostgresContainer("postgres:16-alpine", driver="psycopg").start()
    except Exception as error:
        if os.getenv("CI"):
            raise
        pytest.skip(f"Docker no disponible: {error}")

    try:
        url_admin = make_url(contenedor.get_connection_url())
        # Claves descartables, generadas en cada corrida: no existen en el repo.
        clave_owner = secrets.token_urlsafe(24)
        clave_app = secrets.token_urlsafe(24)

        # En RDS el duenio NO es superusuario (tiene CREATEROLE). Se arma
        # igual aca: un superusuario saltea RLS y esconderia errores.
        admin = create_engine(
            url_admin, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool
        )
        with admin.connect() as conexion:
            conexion.execute(text("CREATE ROLE malka_owner LOGIN CREATEROLE"))
            fijar_clave_de_rol(conexion, "malka_owner", clave_owner)
            conexion.execute(text("CREATE DATABASE malka OWNER malka_owner"))
        admin.dispose()

        url_owner = url_admin.set(
            username="malka_owner", password=clave_owner, database="malka"
        )
        with pytest.MonkeyPatch.context() as parche:
            parche.setenv(
                "DATABASE_URL", url_owner.render_as_string(hide_password=False)
            )
            obtener_url_base_de_datos.cache_clear()
            config = Config(str(RAIZ / "alembic.ini"))
            config.attributes["configurar_logs"] = False
            # Ida, vuelta e ida: si algun downgrade esta roto, falla aca.
            command.upgrade(config, "head")
            command.downgrade(config, "base")
            command.upgrade(config, "head")
        obtener_url_base_de_datos.cache_clear()

        motor = create_engine(url_owner, poolclass=pool.NullPool)
        with motor.begin() as conexion:
            fijar_clave_de_rol(conexion, "malka_app", clave_app)
        motor.dispose()

        yield BaseDePrueba(
            url_owner=url_owner,
            url_app=url_owner.set(username="malka_app", password=clave_app),
        )
    finally:
        contenedor.stop()


@pytest.fixture(scope="session")
def motor_owner(base_de_prueba: BaseDePrueba) -> Iterator[Engine]:
    motor = create_engine(base_de_prueba.url_owner, poolclass=pool.NullPool)
    yield motor
    motor.dispose()


@pytest.fixture(scope="session")
def motor_app(base_de_prueba: BaseDePrueba) -> Iterator[Engine]:
    # Pool de una conexion, igual que la API en Lambda.
    motor = create_engine(base_de_prueba.url_app, pool_size=1, max_overflow=0)
    yield motor
    motor.dispose()


@pytest.fixture
def con_motor_app(motor_app: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hace que app.db use el rol de aplicacion del contenedor."""
    monkeypatch.setattr(db, "obtener_motor", lambda: motor_app)


# --- Cognito de prueba (#21): ID tokens firmados con una clave local ---

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PRUEBA"
CLIENT_ID = "cliente-spa-de-prueba"
KID = "clave-de-prueba"
TENANT = "aaaaaaaa-0000-4000-8000-000000000001"

CLAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)

JWKS_PRODUCCION = json.loads(auth.RUTA_JWKS.read_text(encoding="utf-8"))


@pytest.fixture
def cognito(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """El JWKS de produccion mas la clave de prueba, como si fuera del pool."""
    publica = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(CLAVE.public_key()))
    publica.update({"kid": KID, "alg": "RS256", "use": "sig"})
    claves = jwt.PyJWKSet.from_dict({"keys": [*JWKS_PRODUCCION["keys"], publica]})
    monkeypatch.setattr(auth, "CLAVES", claves)
    monkeypatch.setenv("COGNITO_ISSUER", ISSUER)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT_ID)
    obtener_config_cognito.cache_clear()
    yield
    obtener_config_cognito.cache_clear()


def emitir(
    clave: Any = CLAVE, algoritmo: str = "RS256", kid: str = KID, **cambios: Any
) -> str:
    """Un ID token como los de Cognito; cada test cambia lo que quiere probar."""
    ahora = int(time.time())
    claims: dict[str, Any] = {
        "sub": "usuario-1",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "iat": ahora,
        "exp": ahora + 3600,
        "custom:tenant_id": TENANT,
        "cognito:groups": ["admin"],
    }
    claims.update(cambios)
    claims = {nombre: valor for nombre, valor in claims.items() if valor is not None}
    return jwt.encode(claims, clave, algorithm=algoritmo, headers={"kid": kid})
