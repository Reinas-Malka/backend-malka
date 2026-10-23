"""Reglas del alta de tanda sin base de datos (#27)."""

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.produccion import TandaCrear
from app.services.produccion import tandas
from app.services.produccion.tandas import (
    CUPULAS_POR_CUADRO,
    cupulas_para,
    es_dia_laborable,
    fecha_de_traslarve_admitida,
)

# Una semana cualquiera: el 5/10/2026 fue lunes y el 11/10/2026, domingo.
LUNES = date(2026, 10, 5)
SABADO = date(2026, 10, 10)
DOMINGO = date(2026, 10, 11)


def test_cupulas_por_cuadro_es_135() -> None:
    """docs/dominio.md: constante nombrada en un solo lugar (ambiguedad abierta)."""
    assert CUPULAS_POR_CUADRO == 135


@pytest.mark.parametrize("cuadros", [1, 2, 3])
def test_cada_cuadro_suma_135_cupulas(cuadros: int) -> None:
    assert cupulas_para(cuadros) == cuadros * 135


def test_las_cupulas_salen_de_la_constante(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si la clienta aclara la ambiguedad, alcanza con tocar la constante."""
    monkeypatch.setattr(tandas, "CUPULAS_POR_CUADRO", 45)
    assert cupulas_para(3) == 135


@pytest.mark.parametrize("cuadros", [0, 4, -1])
def test_fuera_de_1_a_3_cuadros_falla(cuadros: int) -> None:
    with pytest.raises(ValueError, match="cuadros"):
        cupulas_para(cuadros)


def test_el_domingo_no_es_laborable() -> None:
    assert DOMINGO.weekday() == 6
    assert not es_dia_laborable(DOMINGO)


@pytest.mark.parametrize("dias", range(6))
def test_de_lunes_a_sabado_es_laborable(dias: int) -> None:
    assert es_dia_laborable(LUNES + timedelta(days=dias))


def test_domingo_se_rechaza_salvo_retroactivo() -> None:
    assert not fecha_de_traslarve_admitida(DOMINGO, retroactivo=False)
    assert fecha_de_traslarve_admitida(DOMINGO, retroactivo=True)
    assert fecha_de_traslarve_admitida(SABADO, retroactivo=False)


# --- schema del alta ---


def _datos(**cambios: object) -> dict[str, object]:
    datos: dict[str, object] = {
        "raza_id": "00000000-0000-0000-0000-000000000002",
        "material_id": "00000000-0000-0000-0000-000000000003",
        "fecha_traslarve": LUNES.isoformat(),
        "cuadros": 2,
    }
    datos.update(cambios)
    return datos


def test_alta_minima_sin_madre_ni_continuadora() -> None:
    """Dominio 16/10: la madre es opcional y la continuadora puede faltar."""
    tanda = TandaCrear.model_validate(_datos())
    assert tanda.cuadros == 2
    assert tanda.retroactivo is False
    assert tanda.madre_id is None
    assert tanda.fila_continuadora is None
    assert tanda.posicion_continuadora is None


def test_retroactiva_con_fecha_pasada() -> None:
    tardia = TandaCrear.model_validate(_datos(retroactivo=True))
    assert tardia.retroactivo is True


def test_alta_con_madre_y_continuadora() -> None:
    tanda = TandaCrear.model_validate(
        _datos(
            madre_id="00000000-0000-0000-0000-000000000001",
            fila_continuadora=2,
            posicion_continuadora=7,
        )
    )
    assert tanda.madre_id is not None
    assert (tanda.fila_continuadora, tanda.posicion_continuadora) == (2, 7)


@pytest.mark.parametrize(
    "cambios",
    [
        {"cuadros": 0},
        {"cuadros": 4},
        {"cuadros": "2"},
        {"cuadros": 2.5},
        {"cuadros": None},
        {"cupulas": 270},
        {"tenant_id": "00000000-0000-0000-0000-000000000009"},
        {"fecha_traslarve": (date.today() + timedelta(days=1)).isoformat()},
        {"fecha_traslarve": date.today().isoformat(), "retroactivo": True},
        {"raza_id": None},
        {"fila_continuadora": 2},
        {"posicion_continuadora": 7},
        {"fila_continuadora": 0, "posicion_continuadora": 7},
        {"fila_continuadora": "2", "posicion_continuadora": 7},
    ],
    ids=[
        "cero_cuadros",
        "cuatro_cuadros",
        "cuadros_texto",
        "cuadros_decimal",
        "sin_cuadros",
        "cupulas_libres",
        "tenant_en_el_body",
        "fecha_futura",
        "retroactivo_de_hoy",
        "raza_null",
        "fila_sin_posicion",
        "posicion_sin_fila",
        "fila_cero",
        "fila_texto",
    ],
)
def test_alta_invalida(cambios: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TandaCrear.model_validate(_datos(**cambios))


@pytest.mark.parametrize("campo", ["raza_id", "material_id", "cuadros"])
def test_campos_obligatorios(campo: str) -> None:
    """La raza identifica la tanda (16/10); sin material no hay consumo."""
    datos = _datos()
    del datos[campo]
    with pytest.raises(ValidationError, match=campo):
        TandaCrear.model_validate(datos)
