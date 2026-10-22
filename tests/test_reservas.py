"""Reglas de las reservas (#35), sin base."""

import uuid

from app.services.ventas.reservas import faltantes

ITALIANA = uuid.uuid4()
CARNIOLA = uuid.uuid4()


def test_si_alcanza_no_falta_nada() -> None:
    assert faltantes({ITALIANA: 10}, {ITALIANA: 10}) == {}


def test_si_no_alcanza_devuelve_cuanto_falta() -> None:
    assert faltantes({ITALIANA: 10}, {ITALIANA: 7}) == {ITALIANA: 3}


def test_una_raza_sin_disponible_cuenta_como_cero() -> None:
    assert faltantes({ITALIANA: 4}, {}) == {ITALIANA: 4}


def test_un_disponible_negativo_suma_a_lo_que_falta() -> None:
    # Pasa si se descartan reinas que ya estaban reservadas.
    assert faltantes({ITALIANA: 2}, {ITALIANA: -1}) == {ITALIANA: 3}


def test_solo_devuelve_las_razas_que_no_alcanzan() -> None:
    pedidas = {ITALIANA: 5, CARNIOLA: 8}
    disponibles = {ITALIANA: 20, CARNIOLA: 6}

    assert faltantes(pedidas, disponibles) == {CARNIOLA: 2}
