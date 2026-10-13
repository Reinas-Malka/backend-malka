"""API de Malka Suite.

Expone los endpoints de salud y los modulos del negocio, que se van
agregando sobre esta misma aplicacion como routers.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import FastAPI, Response
from mangum import Mangum
from pydantic import BaseModel

from app.errores import registrar_manejadores
from app.observabilidad import instalar_observabilidad
from app.routers import clientes, pedidos
from app.salud import Estado, verificar_dependencias

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
app.include_router(pedidos.router)


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


@app.get(
    "/health/ready",
    response_model=Disponibilidad,
    tags=["salud"],
    responses={
        503: {"model": Disponibilidad, "description": "Alguna dependencia falla"}
    },
)
def ready(response: Response) -> Disponibilidad:
    """Prueba de disponibilidad (#23).

    Verifica de verdad cada dependencia: 200 si todas responden, 503 si alguna
    falla. El smoke test del deploy (#10) usa este endpoint, asi que un deploy
    que no llega a la base queda en rojo.
    """
    dependencias = verificar_dependencias()
    sanas = all(estado == Estado.OK for estado in dependencias.values())

    if not sanas:
        response.status_code = 503
    # Que nadie en el camino (navegador, proxy) guarde un estado viejo.
    response.headers["Cache-Control"] = "no-store"

    return Disponibilidad(
        estado="ok" if sanas else "degradado",
        dependencias={nombre: estado.value for nombre, estado in dependencias.items()},
    )


handler = Mangum(app, lifespan="off")
