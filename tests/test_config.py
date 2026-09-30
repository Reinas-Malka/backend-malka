"""Tests de la configuracion de conexion a la base de datos."""

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
