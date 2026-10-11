"""Configuracion de la aplicacion.

La direccion de la base de datos se arma de dos formas:

- En local: con la variable de entorno DATABASE_URL.
- En AWS: con el secreto de Secrets Manager cuyo nombre esta en DB_SECRET_NAME.
  La API usa el del rol de aplicacion (malka-suite-dev/db/app, sin
  privilegios) y la Lambda de migraciones el del duenio de las tablas
  (malka-suite-dev/db/owner).
"""

from __future__ import annotations

import importlib
import json
import os
from functools import lru_cache

from sqlalchemy.engine import URL, make_url


@lru_cache(maxsize=1)
def obtener_url_base_de_datos() -> URL:
    """Devuelve la URL de conexion a PostgreSQL.

    Se usa URL.create en lugar de armar un texto a mano porque la clave que
    genera Terraform tiene caracteres especiales (#, %, :, ?) que romperian
    una URL escrita como texto.
    """
    url_local = os.getenv("DATABASE_URL")
    if url_local:
        return make_url(url_local)

    nombre_secreto = os.getenv("DB_SECRET_NAME")
    if not nombre_secreto:
        raise RuntimeError(
            "Falta configurar la base de datos: definir DATABASE_URL (local) "
            "o DB_SECRET_NAME (AWS)."
        )

    boto3 = importlib.import_module("boto3")
    botocore_config = importlib.import_module("botocore.config")
    cliente = boto3.client(
        "secretsmanager",
        config=botocore_config.Config(
            connect_timeout=3, read_timeout=3, retries={"max_attempts": 2}
        ),
    )
    try:
        respuesta = cliente.get_secret_value(SecretId=nombre_secreto)
        secreto = json.loads(respuesta["SecretString"])
        return URL.create(
            drivername="postgresql+psycopg",
            username=secreto["username"],
            password=secreto["password"],
            host=secreto["host"],
            port=int(secreto["port"]),
            database=secreto["dbname"],
            query={"sslmode": "require"},
        )
    except Exception as error:
        raise RuntimeError(
            f"No se pudo leer el secreto de la base indicado en DB_SECRET_NAME "
            f"({nombre_secreto}): {type(error).__name__}."
        ) from error
