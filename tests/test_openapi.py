"""El OpenAPI es el contrato del frontend (frontend-malka/contrato).

El front genera sus tipos de este documento: un nombre de schema raro le
cambia los tipos sin que nadie lo note en el backend. Que estén las rutas
lo cubre test_api_produccion.py; acá, los nombres.
"""

from app.main import app


def test_los_schemas_no_chocan_de_nombre() -> None:
    """Dos clases con el mismo nombre en módulos distintos (pasó con dos
    TipoDocumento) hacen que FastAPI publique "app__services__..." y
    renombre también la del otro módulo en el contrato."""
    nombres = app.openapi()["components"]["schemas"]

    raros = sorted(n for n in nombres if "__" in n or n.endswith(("-Input", "-Output")))

    assert raros == []
