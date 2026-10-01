"""Prompts versionados del servicio de IA.

Cada prompt vive en un archivo de texto cuyo nombre lleva la version
(borrador_v1, borrador_v2, ...): cambiar un prompt es cambiar de archivo,
asi queda en el historial de git y se puede registrar que version se uso
en cada documento generado.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS = Path(__file__).parent


def cargar_prompt(nombre: str) -> str:
    """Devuelve el contenido de un prompt versionado, sin su extension."""
    archivo = _PROMPTS / f"{nombre}.txt"
    if not archivo.is_file():
        raise FileNotFoundError(f"no existe el prompt {nombre!r} en {_PROMPTS}")
    return archivo.read_text(encoding="utf-8")
