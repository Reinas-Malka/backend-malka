"""Reparto de celdas entre nucleos y schemas de las operaciones (#28), sin base."""

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.fechas import hoy
from app.schemas.produccion import (
    OperacionCosechar,
    OperacionCrear,
    OperacionIntroducir,
    OperacionPasarABanco,
)
from app.services.produccion.operaciones import NucleoOcupado, repartir

FECHA = date(2026, 10, 12)


def nucleos(*ocupadas: int) -> list[NucleoOcupado]:
    """Nucleos en la fila 1, posiciones 1, 2, 3... con esas celdas ocupadas."""
    return [
        NucleoOcupado(id=uuid.uuid4(), fila=1, posicion=i, ocupadas=n)
        for i, n in enumerate(ocupadas, start=1)
    ]


# --- reparto ---


def test_120_celdas_en_60_nucleos_vacios_son_2_por_nucleo() -> None:
    """DoD de la #28."""
    vacios = nucleos(*[0] * 60)
    reparto = repartir(vacios, 120)
    assert list(reparto.values()) == [2] * 60
    assert list(reparto) == [n.id for n in vacios]


def test_primero_van_a_los_mas_libres() -> None:
    """DoD de la #28: con nucleos ya ocupados, los mas libres se llenan antes."""
    a, b, c = nucleos(3, 0, 1)
    reparto = repartir([a, b, c], 3)
    # b (0) recibe hasta igualar a c (1); despues b y c suben a 2 a la par.
    assert reparto == {b.id: 2, c.id: 1}


def test_queda_parejo_contando_lo_que_ya_tenian() -> None:
    a, b, c = nucleos(3, 0, 1)
    reparto = repartir([a, b, c], 6)
    total = {n.id: n.ocupadas + reparto.get(n.id, 0) for n in (a, b, c)}
    assert sorted(total.values()) == [3, 3, 4]


def test_a_igual_ocupacion_va_por_fila_y_posicion() -> None:
    lejos = NucleoOcupado(id=uuid.uuid4(), fila=2, posicion=1, ocupadas=0)
    cerca = NucleoOcupado(id=uuid.uuid4(), fila=1, posicion=5, ocupadas=0)
    assert repartir([lejos, cerca], 1) == {cerca.id: 1}


def test_sin_tope_por_nucleo() -> None:
    """docs/dominio.md: un baby nuc recibe todas las celdas que hagan falta."""
    (unico,) = nucleos(0)
    assert repartir([unico], 405) == {unico.id: 405}


def test_el_reparto_suma_la_cantidad_pedida() -> None:
    reparto = repartir(nucleos(5, 2, 0, 7, 1), 137)
    assert sum(reparto.values()) == 137


def test_sin_nucleos_falla() -> None:
    with pytest.raises(ValueError, match="nucleos"):
        repartir([], 10)


# --- schemas ---

ADAPTADOR: TypeAdapter[Any] = TypeAdapter(OperacionCrear)


def test_la_accion_elige_el_tipo() -> None:
    parque = str(uuid.uuid4())
    introducir = ADAPTADOR.validate_python(
        {"accion": "introducir", "cantidad": 10, "fecha": FECHA, "parque_id": parque}
    )
    banco = ADAPTADOR.validate_python(
        {"accion": "pasar_a_banco", "cantidad": 5, "banco_id": parque}
    )
    assert isinstance(introducir, OperacionIntroducir)
    assert isinstance(banco, OperacionPasarABanco)


def test_cosechar_admite_cero() -> None:
    """Se perdio todo: 0 cosechadas y el resto a descartada."""
    cosecha = ADAPTADOR.validate_python(
        {"accion": "cosechar", "cantidad": 0, "fecha": FECHA}
    )
    assert isinstance(cosecha, OperacionCosechar)


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"accion": "fecundar", "cantidad": 1, "fecha": FECHA},
        {"cantidad": 1, "fecha": FECHA},
        {"accion": "descartar", "cantidad": 0, "fecha": FECHA},
        {"accion": "descartar", "cantidad": "5", "fecha": FECHA},
        {"accion": "descartar", "cantidad": 5},
        {"accion": "descartar", "cantidad": 5, "fecha": hoy() + timedelta(days=1)},
        {"accion": "cosechar", "cantidad": -1, "fecha": FECHA},
        {"accion": "introducir", "cantidad": 5, "fecha": FECHA},
        {"accion": "pasar_a_banco", "cantidad": 5},
        {"accion": "descartar", "cantidad": 5, "fecha": FECHA, "nucleo_id": "x"},
    ],
    ids=[
        "accion_desconocida",
        "sin_accion",
        "descartar_cero",
        "cantidad_texto",
        "sin_fecha",
        "fecha_futura",
        "cosecha_negativa",
        "introducir_sin_parque",
        "banco_sin_banco",
        "elegir_nucleo_a_mano",
    ],
)
def test_operacion_invalida(cuerpo: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ADAPTADOR.validate_python(cuerpo)
