"""Entorno de ejecucion de Alembic.

Toma la direccion de la base desde la configuracion de la app
(app/config.py), asi no queda escrita en ningun archivo del repositorio.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import obtener_url_base_de_datos

config = context.config

# Desde la Lambda de migraciones (app/migrar.py) no se toca la configuracion de
# logs: la maneja el runtime de Lambda y la lleva a CloudWatch.
if config.config_file_name is not None and config.attributes.get(
    "configurar_logs", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Las migraciones se escriben a mano, sin modelos ORM todavia.
target_metadata = None


def run_migrations_offline() -> None:
    """Genera el SQL de las migraciones sin conectarse a la base."""
    context.configure(
        url=obtener_url_base_de_datos().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Aplica las migraciones conectandose a la base."""
    motor = create_engine(obtener_url_base_de_datos(), poolclass=pool.NullPool)

    with motor.connect() as conexion:
        context.configure(connection=conexion, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
