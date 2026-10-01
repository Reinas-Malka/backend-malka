"""Lambda de migraciones de Malka Suite.

Usa la misma imagen que la API, con otro handler (image_config.command en
infra/migraciones.tf). Corre dentro de la VPC y aplica las migraciones de
Alembic contra RDS con las credenciales del secreto indicado en DB_SECRET_NAME.

Eventos aceptados:
    {"accion": "upgrade", "revision": "head"}
    {"accion": "downgrade", "revision": "-1"}
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, pool

from app.config import obtener_url_base_de_datos

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# En Lambda el codigo vive en LAMBDA_TASK_ROOT; en local, en la raiz del repo.
RAIZ = Path(os.getenv("LAMBDA_TASK_ROOT", str(Path(__file__).resolve().parent.parent)))


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

    logger.info(
        "migracion accion=%s revision=%s anterior=%s actual=%s",
        accion,
        revision,
        anterior,
        actual,
    )
    return {"accion": accion, "revision_anterior": anterior, "revision_actual": actual}
