"""Fixtures compartidas por los tests."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errores import ConflictoError, ValidacionError, registrar_manejadores
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
