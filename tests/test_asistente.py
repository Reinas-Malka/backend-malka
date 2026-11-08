"""Piezas del asistente que no necesitan base: resolucion de nombres,
historial, schemas de las herramientas y el contrato del pedido."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.errores import ValidacionError
from app.schemas.asistente import MensajeAsistente, PedidoAsistente
from app.services.ia.asistente import herramientas
from app.services.ia.asistente.conversacion import _historial
from app.services.ia.asistente.resolucion import (
    ErrorDeHerramienta,
    Opcion,
    _patron,
    normalizar,
    resolver,
)
from app.services.ia.prompts import cargar_prompt

RAZAS = [
    Opcion(uuid.uuid4(), "Buckfast"),
    Opcion(uuid.uuid4(), "Carniola"),
    Opcion(uuid.uuid4(), "Carniola Negra"),
]


def test_normalizar_ignora_tildes_mayusculas_y_espacios() -> None:
    assert normalizar("  Apícola   del SUR ") == "apicola del sur"


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [
        ("buckfast", "Buckfast"),
        ("BUCK", "Buckfast"),
        ("carniola", "Carniola"),
        ("negra", "Carniola Negra"),
    ],
)
def test_resolver_exacta_o_unica_parcial(nombre: str, esperado: str) -> None:
    assert resolver(nombre, RAZAS, "la raza").nombre == esperado


@pytest.mark.parametrize(
    ("nombre", "mensaje"),
    [
        ("carn", "ambiguo"),
        ("Italiana", 'No existe la raza "Italiana"'),
        ("   ", "Falta el nombre de la raza"),
    ],
)
def test_resolver_ambiguo_inexistente_o_vacio(nombre: str, mensaje: str) -> None:
    with pytest.raises(ErrorDeHerramienta, match=mensaje):
        resolver(nombre, RAZAS, "la raza")


def test_sin_opciones_el_error_no_inventa_una_lista() -> None:
    with pytest.raises(ErrorDeHerramienta) as error:
        resolver("Pepe", [], "el cliente")

    assert "Opciones" not in str(error.value)


def test_el_patron_escapa_los_comodines_del_usuario() -> None:
    assert _patron("50%_off") == "%50\\%\\_off%"
    assert _patron(None) is None
    assert _patron("  ") is None


def test_dos_clientes_con_el_mismo_nombre_son_ambiguos() -> None:
    iguales = [Opcion(uuid.uuid4(), "Miel SA"), Opcion(uuid.uuid4(), "Miel SA")]

    with pytest.raises(ErrorDeHerramienta, match="ambiguo"):
        resolver("Miel SA", iguales, "el cliente")


def test_historial_solo_del_asistente_es_invalido() -> None:
    with pytest.raises(ValidacionError):
        _historial([MensajeAsistente(rol="asistente", texto="Hola")])


def test_el_mensaje_admite_saltos_de_linea_pero_no_controles() -> None:
    PedidoAsistente.model_validate(
        {"mensajes": [{"rol": "usuario", "texto": "uno\ndos\tthree"}]}
    )
    with pytest.raises(ValidationError):
        MensajeAsistente(rol="usuario", texto="a\x07b")


def test_las_especificaciones_son_json_sin_titulos() -> None:
    especificaciones = herramientas.especificaciones()

    texto = json.dumps(especificaciones)
    assert '"title"' not in texto
    nombres = [e["toolSpec"]["name"] for e in especificaciones]
    assert len(nombres) == len(set(nombres)) == 16
    escritura = {h.nombre for h in herramientas.CATALOGO if h.escritura}
    assert all(nombre.startswith("proponer_") for nombre in escritura)
    assert len(escritura) == 6


def test_el_prompt_marca_las_reglas_del_dominio() -> None:
    prompt = cargar_prompt("asistente_v1")

    for regla in ("<datos>", "CAE", "tipo de cambio", "confirmar", "solo reinas"):
        assert regla in prompt
