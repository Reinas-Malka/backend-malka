"""Tests de la configuracion de conexion a la base de datos."""

import json
import sys
import types
from typing import Any

import pytest

from app.config import obtener_url_base_de_datos

pytestmark = pytest.mark.usefixtures("config_limpia")


def test_usa_database_url_si_esta_definida(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://usuario:clave@localhost:5432/malka"
    )
    monkeypatch.delenv("DB_SECRET_NAME", raising=False)

    url = obtener_url_base_de_datos()

    assert url.drivername == "postgresql+psycopg"
    assert url.host == "localhost"
    assert url.database == "malka"


def test_falla_si_no_hay_ninguna_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_SECRET_NAME", raising=False)

    with pytest.raises(RuntimeError, match="Falta configurar la base de datos"):
        obtener_url_base_de_datos()


NOMBRE = "malka-suite-dev/db/app"

SECRETO = {
    "username": "malka_app",
    "password": "clave#con%raros:?",
    "host": "malka.xxxx.us-east-1.rds.amazonaws.com",
    "port": 5432,
    "dbname": "malka",
}


def _boto_falso(
    monkeypatch: pytest.MonkeyPatch, respuesta: Exception | dict[str, Any]
) -> dict[str, Any]:
    """boto3 y botocore de mentira: no estan instalados en local ni en el CI."""
    usado: dict[str, Any] = {}

    class Config:
        def __init__(self, **opciones: Any) -> None:
            usado["config"] = opciones

    class Cliente:
        def get_secret_value(self, SecretId: str) -> dict[str, Any]:  # noqa: N803
            usado["secret_id"] = SecretId
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

    def client(servicio: str, config: Config) -> Cliente:
        usado["servicio"] = servicio
        return Cliente()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setitem(
        sys.modules, "botocore.config", types.SimpleNamespace(Config=Config)
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_NAME", NOMBRE)
    return usado


def test_lee_el_secreto_con_timeouts_cortos(monkeypatch: pytest.MonkeyPatch) -> None:
    usado = _boto_falso(monkeypatch, {"SecretString": json.dumps(SECRETO)})

    url = obtener_url_base_de_datos()

    assert url.password == SECRETO["password"]
    assert url.query["sslmode"] == "require"
    assert usado["secret_id"] == NOMBRE
    assert usado["config"]["connect_timeout"] <= 5
    assert usado["config"]["read_timeout"] <= 5


def test_un_secreto_que_no_se_puede_leer_da_un_error_claro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResourceNotFoundException(Exception):
        pass

    _boto_falso(monkeypatch, ResourceNotFoundException("Secrets Manager can't find"))

    with pytest.raises(RuntimeError, match="DB_SECRET_NAME") as error:
        obtener_url_base_de_datos()

    assert "ResourceNotFoundException" in str(error.value)


@pytest.mark.parametrize(
    "valor",
    [
        "",  # sin JSON
        "{}",  # JSON sin campos
        json.dumps({"username": "malka_app"}),  # le faltan campos
    ],
    ids=["vacio", "sin-campos", "incompleto"],
)
def test_un_secreto_con_contenido_invalido_da_un_error_claro(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    _boto_falso(monkeypatch, {"SecretString": valor})

    with pytest.raises(RuntimeError, match="DB_SECRET_NAME") as error:
        obtener_url_base_de_datos()

    # El mensaje nunca incluye lo que tenia el secreto.
    assert SECRETO["password"] not in str(error.value)
