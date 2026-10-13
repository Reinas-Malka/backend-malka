"""Paginacion por cursor (#30): codificacion del cursor, sin base ni red."""

import uuid

import pytest

from app.errores import ValidacionError
from app.paginacion import (
    ParametrosDePagina,
    armar_pagina,
    codificar_cursor,
    decodificar_cursor,
    valores_del_cursor,
)


def test_el_cursor_ida_y_vuelta() -> None:
    id_ = uuid.uuid4()
    cursor = codificar_cursor(("Carniola", id_, 3))
    assert decodificar_cursor(cursor, (str, uuid.UUID, int)) == ["Carniola", id_, 3]


def test_el_cursor_sirve_en_una_url() -> None:
    """base64 url-safe y sin relleno: no hay que escaparlo en ?cursor=."""
    cursor = codificar_cursor(("ñandú / ? & =", uuid.uuid4()))
    assert all(caracter.isalnum() or caracter in "-_" for caracter in cursor)


@pytest.mark.parametrize(
    "cursor",
    [
        "basura!!",
        codificar_cursor(("solo-uno",)),
        codificar_cursor(("Carniola", "no-es-un-uuid")),
        "eyJhIjogMX0",  # {"a": 1}: JSON valido pero no una lista
        "WzEsIDJd",  # [1, 2]: valores que no son texto
    ],
    ids=["no_base64", "cantidad", "uuid_invalido", "no_lista", "no_texto"],
)
def test_cursor_invalido_es_422(cursor: str) -> None:
    with pytest.raises(ValidacionError) as error:
        decodificar_cursor(cursor, (str, uuid.UUID))
    assert error.value.code == "cursor_invalido"
    assert error.value.status_code == 422


def test_primera_pagina_sin_cursor() -> None:
    pagina = ParametrosDePagina(limite=10, cursor=None)
    assert valores_del_cursor(pagina, (str, uuid.UUID)) == [None, None]


def test_armar_pagina_con_siguiente() -> None:
    pagina = armar_pagina(["a", "b", "c"], 2, lambda item: (item,))
    assert pagina.items == ["a", "b"]
    assert pagina.siguiente is not None
    assert decodificar_cursor(pagina.siguiente, (str,)) == ["b"]


def test_armar_pagina_ultima() -> None:
    pagina = armar_pagina(["a", "b"], 2, lambda item: (item,))
    assert pagina.items == ["a", "b"]
    assert pagina.siguiente is None
