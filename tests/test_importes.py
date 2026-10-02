"""Tests del calculo de importes (#41): cuentas hechas a mano, sin base."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.documentos.importes import (
    Alicuota,
    Linea,
    SubtotalAlicuota,
    calcular_importes,
    redondear,
)


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


def test_una_linea_al_21() -> None:
    # 3 x 1234.56 = 3703.68 | IVA 3703.68 x 0.21 = 777.7728 -> 777.77
    r = calcular_importes([Linea(3, Decimal("1234.56"), Alicuota.IVA_21)])

    assert r.lineas_neto == (Decimal("3703.68"),)
    assert r.neto == Decimal("3703.68")
    assert r.iva == Decimal("777.77")
    assert r.total == Decimal("4481.45")
    assert r.moneda == "ARS"
    assert r.tipo_cambio is None
    assert r.total_en_pesos == r.total


def test_alicuotas_mezcladas_se_agrupan_de_menor_a_mayor() -> None:
    # 2 x 2500 = 5000.00 al 21% -> IVA 1050.00
    # 10 x 15000 = 150000.00 al 10.5% -> IVA 15750.00
    r = calcular_importes(
        [
            Linea(2, Decimal("2500.00"), Alicuota.IVA_21),
            Linea(10, Decimal("15000.00"), Alicuota.IVA_10_5),
        ]
    )

    # Aunque la linea al 21% viene primero, el 10.5% sale antes.
    assert r.por_alicuota == (
        SubtotalAlicuota(Alicuota.IVA_10_5, Decimal("150000.00"), Decimal("15750.00")),
        SubtotalAlicuota(Alicuota.IVA_21, Decimal("5000.00"), Decimal("1050.00")),
    )
    assert r.neto == Decimal("155000.00")
    assert r.iva == Decimal("16800.00")
    assert r.total == Decimal("171800.00")


def test_el_medio_centavo_redondea_hacia_arriba() -> None:
    # IVA 2.50 x 0.21 = 0.525 -> 0.53 (con ROUND_HALF_EVEN daria 0.52)
    r = calcular_importes([Linea(1, Decimal("2.50"), Alicuota.IVA_21)])

    assert r.iva == Decimal("0.53")
    assert r.total == Decimal("3.03")


def test_el_iva_se_calcula_por_alicuota_y_no_por_linea() -> None:
    # Por alicuota: 0.36 x 0.21 = 0.0756 -> 0.08
    # Por linea habria sido 0.0252 -> 0.03, tres veces = 0.09
    r = calcular_importes([Linea(1, Decimal("0.12"), Alicuota.IVA_21)] * 3)

    assert len(r.por_alicuota) == 1
    assert r.neto == Decimal("0.36")
    assert r.iva == Decimal("0.08")
    assert r.total == Decimal("0.44")


def test_el_neto_de_cada_linea_se_redondea() -> None:
    # 3 x 0.335 = 1.005 -> 1.01
    r = calcular_importes([Linea(3, Decimal("0.335"), Alicuota.IVA_0)])

    assert r.lineas_neto == (Decimal("1.01"),)
    assert r.total == Decimal("1.01")


def test_los_importes_salen_siempre_con_dos_decimales() -> None:
    # 5 x 100 = 500 al 0%: el IVA es "0.00", no "0"
    r = calcular_importes([Linea(5, Decimal("100"), Alicuota.IVA_0)])

    assert str(r.neto) == "500.00"
    assert str(r.iva) == "0.00"
    assert str(r.total) == "500.00"
