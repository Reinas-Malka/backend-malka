"""Conexion a PostgreSQL con el tenant fijado por transaccion (#19).

Toda consulta de negocio pasa por sesion_de_tenant: abre una transaccion, fija
app.tenant_id con alcance LOCAL y recien ahi entrega la sesion. Las politicas
de RLS filtran por ese valor: los servicios no filtran por tenant a mano, de
eso se encarga PostgreSQL.

En AWS la API se conecta con el rol malka_app (migracion 0002), que no es
duenio de las tablas ni tiene BYPASSRLS.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.auth import Identidad, identidad_actual
from app.config import obtener_url_base_de_datos

# set_config(..., true) es exactamente SET LOCAL, pero acepta parametros: el
# tenant viaja como valor ligado y nunca se concatena al SQL.
FIJAR_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


@lru_cache(maxsize=1)
def obtener_motor() -> Engine:
    """Un motor por contenedor de Lambda, reutilizado entre invocaciones."""
    return create_engine(
        obtener_url_base_de_datos(),
        # Lambda atiende un request por vez: con una conexion alcanza.
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=300,
    )


@contextmanager
def sesion_de_tenant(tenant_id: UUID) -> Iterator[Session]:
    """Sesion dentro de una transaccion con app.tenant_id fijado.

    Al salir se hace commit (o rollback si hubo error) y el valor desaparece
    con la transaccion: la conexion vuelve al pool sin tenant.
    """
    with Session(obtener_motor()) as sesion, sesion.begin():
        sesion.execute(FIJAR_TENANT, {"tenant_id": str(tenant_id)})
        yield sesion


def obtener_sesion(
    identidad: Identidad = Depends(identidad_actual),
) -> Iterator[Session]:
    """Dependencia de las rutas de negocio.

    Sin un token valido con tenant, identidad_actual corta con 401 antes de
    abrir ninguna conexion.
    """
    with sesion_de_tenant(identidad.tenant_id) as sesion:
        yield sesion
