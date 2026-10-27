"""Formato del cuerpo de POST /api/v1/pedidos/{id}/estado (#36).

Si el pedido puede pasar a ese estado y si el despacho coincide con sus
reservas no se prueba aca: lo revisa el repositorio con las reglas de
tests/test_transiciones.py.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.fechas import hoy
from app.schemas.pedidos import MAX_CANTIDAD, MAX_ITEMS
from app.schemas.transiciones import (
    MAX_BANCOS_POR_LINEA,
    CambioDeEstado,
    Cancelar,
    Entregar,
)
from app.services.ventas.pedidos import EstadoPedido

# El cuerpo es una union: se valida igual que lo hace FastAPI.
CAMBIO = TypeAdapter(CambioDeEstado)

NORTE = str(uuid.uuid4())
SUR = str(uuid.uuid4())
LINEA_A = str(uuid.uuid4())
LINEA_B = str(uuid.uuid4())

# El ejemplo del issue: la linea A sale de dos bancos y la B de reinas
# enjauladas que no estan en ningun banco.
ENTREGAR: dict[str, Any] = {
    "estado": "entregado",
    "lineas": [
        {
            "pedido_item_id": LINEA_A,
            "bancos": [
                {"banco_id": NORTE, "cantidad": 6},
                {"banco_id": SUR, "cantidad": 4},
            ],
        },
        {"pedido_item_id": LINEA_B, "bancos": [{"banco_id": None, "cantidad": 5}]},
    ],
}


def _linea(*bancos: str | None, cantidad: int = 1) -> dict[str, Any]:
    return {
        "pedido_item_id": str(uuid.uuid4()),
        "bancos": [{"banco_id": banco, "cantidad": cantidad} for banco in bancos],
    }


def _entregar(*lineas: dict[str, Any], **cambios: Any) -> dict[str, Any]:
    return {"estado": "entregado", "lineas": list(lineas), **cambios}


def _tipo_de_error(cuerpo: dict[str, Any]) -> str:
    """El tipo del primer error: asegura que falla por lo que el test dice."""
    with pytest.raises(ValidationError) as error:
        CAMBIO.validate_python(cuerpo)
    tipo: str = error.value.errors()[0]["type"]
    return tipo


def test_entregar_repartido_entre_dos_bancos() -> None:
    cambio = CAMBIO.validate_python(ENTREGAR)

    assert isinstance(cambio, Entregar)
    assert cambio.estado == EstadoPedido.ENTREGADO
    assert [s.cantidad for s in cambio.lineas[0].bancos] == [6, 4]
    assert cambio.lineas[1].bancos[0].banco_id is None


def test_entregar_sin_fecha_la_deja_para_el_repositorio() -> None:
    # El repositorio completa con hoy(): el schema no inventa la fecha.
    assert CAMBIO.validate_python(ENTREGAR).fecha is None  # type: ignore[union-attr]


def test_entregar_con_fecha_de_hoy_o_anterior() -> None:
    for fecha in (hoy(), hoy() - timedelta(days=3)):
        cambio = CAMBIO.validate_python({**ENTREGAR, "fecha": fecha.isoformat()})
        assert cambio.fecha == fecha  # type: ignore[union-attr]


def test_desde_json_como_lo_manda_el_frontend() -> None:
    cambio = CAMBIO.validate_json('{"estado": "cancelado"}')

    assert isinstance(cambio, Cancelar)
    assert cambio.estado == EstadoPedido.CANCELADO


@pytest.mark.parametrize(
    ("cuerpo", "tipo"),
    [
        # reservado lo hace POST /reservas (#35), no este endpoint.
        pytest.param({"estado": "reservado"}, "union_tag_invalid", id="reservado"),
        pytest.param({"estado": "borrador"}, "union_tag_invalid", id="borrador"),
        pytest.param({"estado": "perdido"}, "union_tag_invalid", id="inventado"),
        pytest.param({}, "union_tag_not_found", id="sin_estado"),
    ],
)
def test_solo_acepta_entregado_o_cancelado(cuerpo: dict[str, Any], tipo: str) -> None:
    assert _tipo_de_error(cuerpo) == tipo


def test_cancelar_no_lleva_lineas() -> None:
    cuerpo = {"estado": "cancelado", "lineas": ENTREGAR["lineas"]}

    assert _tipo_de_error(cuerpo) == "extra_forbidden"


def test_rechaza_campos_desconocidos_en_cada_nivel() -> None:
    linea = _linea(NORTE)
    linea["bancos"][0]["banco"] = "Norte"

    assert _tipo_de_error({**ENTREGAR, "motivo": "x"}) == "extra_forbidden"
    assert _tipo_de_error(_entregar({**_linea(NORTE), "raza_id": SUR})) == (
        "extra_forbidden"
    )
    assert _tipo_de_error(_entregar(linea)) == "extra_forbidden"


def test_banco_id_es_obligatorio_aunque_acepte_null() -> None:
    # Olvidarse el banco no es lo mismo que decir "sin banco".
    linea = {"pedido_item_id": LINEA_A, "bancos": [{"cantidad": 3}]}

    assert _tipo_de_error(_entregar(linea)) == "missing"


@pytest.mark.parametrize(
    ("cantidad", "tipo"),
    [
        pytest.param(0, "greater_than", id="cero"),
        pytest.param(-2, "greater_than", id="negativa"),
        # Por encima, PostgreSQL rechazaria el numero y la API daria 500.
        pytest.param(MAX_CANTIDAD + 1, "less_than_equal", id="no_entra_en_integer"),
    ],
)
def test_rechaza_cantidades_fuera_de_rango(cantidad: int, tipo: str) -> None:
    assert _tipo_de_error(_entregar(_linea(NORTE, cantidad=cantidad))) == tipo


def test_rechaza_un_despacho_sin_lineas() -> None:
    assert _tipo_de_error(_entregar()) == "too_short"


def test_rechaza_una_linea_sin_bancos() -> None:
    assert _tipo_de_error(_entregar(_linea())) == "too_short"


def test_topes_de_lineas_y_de_bancos() -> None:
    lineas = [_linea(NORTE) for _ in range(MAX_ITEMS + 1)]
    bancos = [str(uuid.uuid4()) for _ in range(MAX_BANCOS_POR_LINEA + 1)]

    assert _tipo_de_error(_entregar(*lineas)) == "too_long"
    assert _tipo_de_error(_entregar(_linea(*bancos))) == "too_long"


@pytest.mark.parametrize(
    "bancos",
    [
        pytest.param((NORTE, SUR, NORTE), id="mismo_banco"),
        # "Sin banco" tambien va una sola vez por linea.
        pytest.param((None, None), id="dos_veces_sin_banco"),
    ],
)
def test_rechaza_un_banco_repetido_en_la_linea(bancos: tuple[str | None, ...]) -> None:
    with pytest.raises(ValidationError, match="banco aparece dos veces"):
        CAMBIO.validate_python(_entregar(_linea(*bancos)))


def test_el_mismo_banco_en_lineas_distintas_es_valido() -> None:
    CAMBIO.validate_python(_entregar(_linea(NORTE), _linea(NORTE)))


def test_rechaza_una_linea_repetida() -> None:
    linea = _linea(NORTE)

    with pytest.raises(ValidationError, match="linea del pedido aparece dos veces"):
        CAMBIO.validate_python(_entregar(linea, linea))


def test_rechaza_una_fecha_futura() -> None:
    # El despacho registra algo que ya paso: el stock baja al cargarlo.
    manana = (hoy() + timedelta(days=1)).isoformat()

    with pytest.raises(ValidationError, match="no puede ser futura"):
        CAMBIO.validate_python({**ENTREGAR, "fecha": manana})
