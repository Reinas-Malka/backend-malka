"""Los servicios no filtran por tenant a mano: lo hace RLS (#19)."""

import re
from pathlib import Path

FILTRO_MANUAL = re.compile(
    r"\bwhere\b[\s\S]{0,200}?\btenant_id\s*="
    r"|\.tenant_id\s*=="
    r"|filter_by\([^)]*tenant_id\s*=",
    re.IGNORECASE,
)


def test_ningun_modulo_filtra_por_tenant_a_mano() -> None:
    raiz = Path(__file__).resolve().parent.parent / "app"
    culpables = [
        str(archivo.relative_to(raiz))
        for archivo in raiz.rglob("*.py")
        if FILTRO_MANUAL.search(archivo.read_text(encoding="utf-8"))
    ]
    assert culpables == [], f"Filtrar por tenant es tarea de RLS: {culpables}"