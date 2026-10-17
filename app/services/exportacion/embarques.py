"""Reglas de los embarques de exportacion (#109).

No depende de la base, de FastAPI ni de AWS. Reglas en docs/dominio.md,
"Exportacion" y "Modalidades de envio" (11/10): las dos modalidades
(directo y consolidado) se tratan igual, sin logica por tenant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class EstadoEmbarque(StrEnum):
    EN_PREPARACION = "en_preparacion"
    EN_TRANSITO = "en_transito"
    ENTREGADO = "entregado"
    CANCELADO = "cancelado"


class TipoDocumento(StrEnum):
    """Los 6 documentos de un envio, en el orden de docs/dominio.md."""

    FACTURA_E = "factura_e"
    PERMISO_EMBARQUE = "permiso_embarque"
    GUIA_AEREA = "guia_aerea"
    LISTA_EMPAQUE = "lista_empaque"
    DTE = "dte"
    SHIPPER_CERTIFICATE = "shipper_certificate"


# NNN-NNNNNNNN: prefijo de la aerolinea, numero de serie de 7 digitos y
# digito verificador. Se acepta sin guion o con un espacio y se guarda con
# guion.
_AWB = re.compile(r"^(\d{3})[\s-]?(\d{7})(\d)$")


def normalizar_awb(valor: str) -> str:
    """Valida una guia aerea IATA y la devuelve como NNN-NNNNNNNN.

    El digito verificador es el resto de dividir el numero de serie por 7:
    asi se detecta un digito mal tipeado antes de que llegue a la aerolinea.
    """
    coincidencia = _AWB.match(valor.strip())
    if coincidencia is None:
        raise ValueError("La guia aerea tiene que tener el formato NNN-NNNNNNNN.")
    prefijo, serie, verificador = coincidencia.groups()
    if int(serie) % 7 != int(verificador):
        raise ValueError("El digito verificador de la guia aerea no coincide.")
    return f"{prefijo}-{serie}{verificador}"


@dataclass(frozen=True)
class Completitud:
    presentes: tuple[TipoDocumento, ...]
    faltantes: tuple[TipoDocumento, ...]

    @property
    def completo(self) -> bool:
        return not self.faltantes


def completitud(tipos_cargados: Iterable[str]) -> Completitud:
    """Que documentos tiene el embarque y cuales faltan, en orden fijo."""
    cargados = set(tipos_cargados)
    return Completitud(
        presentes=tuple(tipo for tipo in TipoDocumento if tipo in cargados),
        faltantes=tuple(tipo for tipo in TipoDocumento if tipo not in cargados),
    )
