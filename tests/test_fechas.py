"""El "hoy" de las reglas de negocio es el de Argentina, no el de UTC.

La Lambda corre en UTC: a las 22 h de Argentina ya son las 01 h del dia
siguiente. Estos tests fijan la hora y verifican que las validaciones de
fecha usan el dia de Argentina, sin base de datos.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app import fechas
from app.fechas import hoy
from app.schemas.produccion import MovimientoCrear, TandaCrear

# Miercoles 21/10/2026 a las 22 h de Argentina = jueves 22/10 a la 01 h UTC.
NOCHE_EN_ARGENTINA = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
HOY_AR = date(2026, 10, 21)
HOY_UTC = date(2026, 10, 22)

FijarHora = Callable[[datetime], None]


@pytest.fixture
def fijar_hora(monkeypatch: pytest.MonkeyPatch) -> FijarHora:
    """Cambia el instante que ve app.fechas.hoy()."""

    def fijar(instante: datetime) -> None:
        monkeypatch.setattr(fechas, "_ahora_utc", lambda: instante)

    return fijar


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        # 21 h de Argentina: en UTC ya es medianoche del dia siguiente.
        (datetime(2026, 10, 22, 0, 0, tzinfo=UTC), date(2026, 10, 21)),
        (datetime(2026, 10, 22, 2, 59, tzinfo=UTC), date(2026, 10, 21)),
        # 00 h de Argentina: recien ahi cambia el dia.
        (datetime(2026, 10, 22, 3, 0, tzinfo=UTC), date(2026, 10, 22)),
        (datetime(2026, 10, 22, 15, 0, tzinfo=UTC), date(2026, 10, 22)),
    ],
    ids=["21h", "23h59", "medianoche", "mediodia"],
)
def test_hoy_cambia_a_medianoche_de_argentina(
    fijar_hora: FijarHora, instante: datetime, esperado: date
) -> None:
    fijar_hora(instante)
    assert hoy() == esperado


def test_la_zona_es_la_de_buenos_aires() -> None:
    assert str(fechas.ZONA_ARGENTINA) == "America/Argentina/Buenos_Aires"


# --- tandas (#27) ---


def _tanda(**cambios: object) -> dict[str, object]:
    return {
        "raza_id": "00000000-0000-0000-0000-000000000002",
        "material_id": "00000000-0000-0000-0000-000000000003",
        "cuadros": 1,
        **cambios,
    }


def test_traslarve_de_manana_en_argentina_se_rechaza(fijar_hora: FijarHora) -> None:
    """A las 22 h, la fecha UTC (manana en Argentina) todavia es futura."""
    fijar_hora(NOCHE_EN_ARGENTINA)
    with pytest.raises(ValidationError, match="futura"):
        TandaCrear.model_validate(_tanda(fecha_traslarve=HOY_UTC.isoformat()))


def test_traslarve_de_hoy_en_argentina_se_acepta(fijar_hora: FijarHora) -> None:
    fijar_hora(NOCHE_EN_ARGENTINA)
    tanda = TandaCrear.model_validate(_tanda(fecha_traslarve=HOY_AR.isoformat()))
    assert tanda.fecha_traslarve == HOY_AR


def test_retroactivo_de_hoy_en_argentina_se_rechaza(fijar_hora: FijarHora) -> None:
    """Para el servidor es "ayer", pero en Argentina es hoy: no es retroactivo."""
    fijar_hora(NOCHE_EN_ARGENTINA)
    with pytest.raises(ValidationError, match="retroactivo"):
        TandaCrear.model_validate(
            _tanda(fecha_traslarve=HOY_AR.isoformat(), retroactivo=True)
        )


# --- movimientos de material (#30) ---


def test_movimiento_de_manana_en_argentina_se_rechaza(fijar_hora: FijarHora) -> None:
    fijar_hora(NOCHE_EN_ARGENTINA)
    with pytest.raises(ValidationError, match="futura"):
        MovimientoCrear.model_validate(
            {"tipo": "compra", "cantidad": 10, "fecha": HOY_UTC.isoformat()}
        )


def test_movimiento_de_hoy_en_argentina_se_acepta(fijar_hora: FijarHora) -> None:
    fijar_hora(NOCHE_EN_ARGENTINA)
    movimiento = MovimientoCrear.model_validate(
        {"tipo": "compra", "cantidad": 10, "fecha": HOY_AR.isoformat()}
    )
    assert movimiento.fecha == HOY_AR
