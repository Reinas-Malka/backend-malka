"""Tests del calculo de importes (#41): cuentas hechas a mano, sin base."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.documentos.importes import redondear


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("777.7728", "777.77"),
        ("0.525", "0.53"),  # prueba del HALF_UP
        ("1.005", "1.01"),
        ("0.0756", "0.08"),
        ("0", "0.00"),  # siempre con dos decimales
    ],
)
def test_redondear(valor: str, esperado: str) -> None:
    assert str(redondear(Decimal(valor))) == esperado
