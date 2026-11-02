"""Cuentas de los KPIs de produccion (#31), sin base."""

from decimal import Decimal

import pytest

from app.services.produccion.kpis import porcentaje


def test_el_ejemplo_del_dominio() -> None:
    """DoD: 120 introducidas, 95 cosechadas y 25 descartadas."""
    assert porcentaje(95, 120) == Decimal("79.2")
    assert porcentaje(25, 120) == Decimal("20.8")


@pytest.mark.parametrize(
    ("parte", "total", "esperado"),
    [
        (0, 10, Decimal("0.0")),
        (10, 10, Decimal("100.0")),
        (1, 3, Decimal("33.3")),
        (2, 3, Decimal("66.7")),
        # Mitad exacta: redondea para arriba, como se hace a mano.
        (1, 8, Decimal("12.5")),
        (1, 16, Decimal("6.3")),
    ],
)
def test_un_decimal(parte: int, total: int, esperado: Decimal) -> None:
    assert porcentaje(parte, total) == esperado


def test_sin_introducidas_no_hay_porcentaje() -> None:
    """DoD: sin division por cero. Sin base, None y no un 0 que parezca dato."""
    assert porcentaje(0, 0) is None
