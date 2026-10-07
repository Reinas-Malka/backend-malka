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

    # boto3 viene incluido en la imagen oficial de Lambda, pero no en local ni
    # en el CI. Se carga solo cuando hace falta.
    boto3 = importlib.import_module("boto3")
    cliente = boto3.client("secretsmanager")
    respuesta = cliente.get_secret_value(SecretId=nombre_secreto)
    secreto = json.loads(respuesta["SecretString"])

    return URL.create(
        drivername="postgresql+psycopg",
        username=secreto["username"],
        password=secreto["password"],
        host=secreto["host"],
        port=int(secreto["port"]),
        database=secreto["dbname"],
        # RDS exige conexiones cifradas (rds.force_ssl = 1 en infra/rds.tf).
        query={"sslmode": "require"},
    )
