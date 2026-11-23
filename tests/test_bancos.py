"""Regla pura del identificador automatico del banco (sin base)."""

import re

import pytest

from app.services.produccion.bancos import PATRON_SERIE, identificador_siguiente


@pytest.mark.parametrize(
    ("mayor", "esperado"),
    [(0, "B-01"), (1, "B-02"), (9, "B-10"), (98, "B-99"), (99, "B-100")],
)
def test_identificador_siguiente(mayor: int, esperado: str) -> None:
    assert identificador_siguiente(mayor) == esperado


def test_identificador_siguiente_no_acepta_negativos() -> None:
    with pytest.raises(ValueError, match="negativo"):
        identificador_siguiente(-1)


@pytest.mark.parametrize(
    ("identificador", "es_de_la_serie"),
    [
        ("B-01", True),
        ("B-100", True),
        ("B-1", True),
        ("b-01", False),
        ("B-01a", False),
        ("Banco 3", False),
        ("B-1234567890", False),
    ],
)
def test_que_identificadores_cuentan_para_la_serie(
    identificador: str, es_de_la_serie: bool
) -> None:
    """La consulta usa este patron: lo que no matchea no mueve la serie."""
    assert (re.match(PATRON_SERIE, identificador) is not None) is es_de_la_serie


def test_lo_que_genera_es_de_la_serie() -> None:
    assert re.match(PATRON_SERIE, identificador_siguiente(41))
