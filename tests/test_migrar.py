"""Tests de la Lambda de migraciones que no necesitan base de datos."""

import pytest

from app.migrar import handler, preparar_rol_app, validar_evento


def test_upgrade_va_a_la_ultima_version_por_defecto() -> None:
    assert validar_evento({}) == ("upgrade", "head")


def test_downgrade_con_revision() -> None:
    assert validar_evento({"accion": "downgrade", "revision": "-1"}) == (
        "downgrade",
        "-1",
    )


def test_rechaza_una_accion_invalida() -> None:
    with pytest.raises(ValueError, match="accion invalida"):
        handler({"accion": "borrar_todo"}, None)


def test_downgrade_exige_revision_explicita() -> None:
    with pytest.raises(ValueError, match="revision explicita"):
        handler({"accion": "downgrade"}, None)

def test_sin_secreto_del_rol_app_no_hace_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_APP_SECRET_NAME", raising=False)
    assert preparar_rol_app() == "sin_secreto_configurado"
