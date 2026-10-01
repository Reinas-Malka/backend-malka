"""Tests del borrador generado por IA (#40), con el modelo mockeado."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.ia.borrador import (
    CAMPOS_PROHIBIDOS,
    PROMPT_VERSION,
    SalidaInvalida,
    generar_borrador,
    validar_salida,
)
from app.services.ia.cliente import MODELO
from app.services.ia.prompts import cargar_prompt

CONTEXTO = "Pedido 12: 20 reinas fecundadas raza italiana, entrega en octubre."

JSON_VALIDO = (
    '{"encabezado": "Remito de venta", '
    '"cuerpo": "Se entregan 20 reinas fecundadas.", '
    '"notas": "Entrega en deposito"}'
)


class ModeloFalso:
    """Doble de ClienteIA: devuelve respuestas encoladas y cuenta las llamadas."""

    def __init__(self, respuestas: list[str]) -> None:
        self.respuestas = list(respuestas)
        self.llamadas = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.llamadas += 1
        return {
            "output": {"message": {"content": [{"text": self.respuestas.pop(0)}]}},
            "usage": {"inputTokens": 13, "outputTokens": 4},
        }


def test_salida_valida_genera_borrador() -> None:
    resultado = generar_borrador(ModeloFalso([JSON_VALIDO]), CONTEXTO)

    assert resultado.estado == "borrador"
    assert resultado.salida is not None
    assert resultado.salida.encabezado == "Remito de venta"
    assert resultado.motivo_rechazo is None

    assert resultado.metricas is not None
    assert resultado.metricas.modelo == MODELO
    assert resultado.metricas.version_prompt == PROMPT_VERSION
    assert resultado.metricas.tokens_entrada == 13
    assert resultado.metricas.reintentos == 0
    assert resultado.metricas.resultado_validacion == "valida"


def test_salida_invalida_se_reintenta_una_vez_y_lista() -> None:
    modelo = ModeloFalso(["no soy json", JSON_VALIDO])

    resultado = generar_borrador(modelo, CONTEXTO)

    assert resultado.estado == "borrador"
    assert modelo.llamadas == 2
    assert resultado.metricas is not None
    assert resultado.metricas.reintentos == 1
    assert resultado.metricas.resultado_validacion == "valida_tras_reintento"


def test_dos_salidas_invalidas_rechazan_sin_persistir_nada() -> None:
    modelo = ModeloFalso(["no soy json", "tampoco soy json"])

    resultado = generar_borrador(modelo, CONTEXTO)

    assert resultado.estado == "rechazado"
    assert resultado.motivo_rechazo == "salida_invalida"
    assert resultado.salida is None
    assert modelo.llamadas == 2
    assert resultado.metricas is not None
    assert resultado.metricas.reintentos == 1
    assert resultado.metricas.tokens_salida == 8  # las dos llamadas sumadas


def test_campo_fiscal_como_clave_se_rechaza() -> None:
    con_total = '{"encabezado": "X", "cuerpo": "Y", "notas": "", "total": 1500}'

    resultado = generar_borrador(ModeloFalso([con_total, con_total]), CONTEXTO)

    assert resultado.estado == "rechazado"
    assert resultado.motivo_rechazo == "salida_invalida"


def test_campo_fiscal_en_prosa_tambien_se_rechaza() -> None:
    con_prosa = (
        '{"encabezado": "X", '
        '"cuerpo": "El CAE de la operacion es 1234.", '
        '"notas": ""}'
    )

    resultado = generar_borrador(ModeloFalso([con_prosa, con_prosa]), CONTEXTO)

    assert resultado.estado == "rechazado"


def test_palabra_que_contiene_campo_fiscal_no_se_rechaza() -> None:
    """El limite es palabra completa: totalidad no es total."""

    texto = (
        '{"encabezado": "X", '
        '"cuerpo": "Se detalla la totalidad de las reinas.", '
        '"notas": ""}'
    )

    assert validar_salida(texto).cuerpo.startswith("Se detalla")


def test_salida_sin_json_levanta_error_claro() -> None:
    with pytest.raises(SalidaInvalida, match="no contiene un objeto JSON"):
        validar_salida("perdi el json")


def test_el_prompt_v1_existe_y_nombra_los_campos_fiscales() -> None:
    prompt = cargar_prompt(PROMPT_VERSION)

    for campo in CAMPOS_PROHIBIDOS:
        assert campo in prompt
