"""API de Malka Suite.

Expone los endpoints de salud y los modulos del negocio, que se van
agregando sobre esta misma aplicacion como routers.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import FastAPI
from mangum import Mangum
from pydantic import BaseModel

from app.errores import registrar_manejadores
from app.observabilidad import instalar_observabilidad
from app.routers import clientes

VERSION = os.getenv("APP_VERSION", "0.1.0")
ENTORNO = os.getenv("APP_ENVIRONMENT", "dev")

app = FastAPI(
    title="Malka Suite API",
    version=VERSION,
    description="Backend de gestion para la cabania apicola Malka.",
)

registrar_manejadores(app)
instalar_observabilidad(app)
app.include_router(clientes.router)


class Salud(BaseModel):
    """Respuesta de la prueba de vida."""

    estado: str
    version: str
    entorno: str
    momento: datetime


class Disponibilidad(BaseModel):
    """Respuesta de la prueba de disponibilidad."""

    estado: str
    dependencias: dict[str, str]


@app.get("/health", response_model=Salud, tags=["salud"])
def health() -> Salud:
    """Prueba de vida: responde sin consultar ninguna dependencia externa."""
    return Salud(
        estado="ok",
        version=VERSION,
        entorno=ENTORNO,
        momento=datetime.now(UTC),
    )


@app.get("/health/ready", response_model=Disponibilidad, tags=["salud"])
def ready() -> Disponibilidad:
    """Prueba de disponibilidad.

    Cuando exista la base de datos, aca se verifica la conexion antes de
    declarar que la API esta en condiciones de recibir trafico.
    """
    dependencias = {"base_de_datos": "todavia no configurada"}
    return Disponibilidad(estado="ok", dependencias=dependencias)


handler = Mangum(app, lifespan="off")
