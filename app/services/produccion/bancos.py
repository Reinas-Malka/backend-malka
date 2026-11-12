"""Identificador automatico del banco, sin base ni FastAPI.

Si el alta no trae identificador, el sistema asigna el siguiente de la serie
B-01, B-02, ... del criadero: el numero mas alto ya usado con esa forma, mas
uno. No rellena huecos: un banco nunca se borra (baja logica), asi que un
hueco solo aparece si alguien cargo a mano, por ejemplo, "B-07" sin los
anteriores, y reusar numeros confundiria a quien ya los vio en el campo.
"""

from __future__ import annotations

PREFIJO = "B-"
# Al menos dos digitos (B-01); desde el 100 crece solo (B-100).
DIGITOS_MINIMOS = 2
# Identificadores que cuentan para la serie: "B-" y solo digitos. Hasta 9
# digitos, asi el numero siempre entra en un integer de PostgreSQL. Se le
# pasa a la consulta como parametro (repositorio_bancos.MAYOR_NUMERO).
PATRON_SERIE = r"^B-([0-9]{1,9})$"


def identificador_siguiente(mayor_usado: int) -> str:
    """El identificador que sigue al numero mas alto ya usado (0 si no hay)."""
    if mayor_usado < 0:
        raise ValueError("El numero mas alto usado no puede ser negativo.")
    return f"{PREFIJO}{mayor_usado + 1:0{DIGITOS_MINIMOS}d}"
