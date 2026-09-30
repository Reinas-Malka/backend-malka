"""Pruebas de los endpoints de salud."""

from fastapi.testclient import TestClient

from app.main import app

cliente = TestClient(app)


def test_health_responde_200_con_sus_claves() -> None:
    respuesta = cliente.get("/health")

    assert respuesta.status_code == 200
    assert set(respuesta.json()) == {"estado", "version", "entorno", "momento"}


def test_health_ready_responde_200_con_dependencias() -> None:
    respuesta = cliente.get("/health/ready")

    assert respuesta.status_code == 200
    assert isinstance(respuesta.json()["dependencias"], dict)
