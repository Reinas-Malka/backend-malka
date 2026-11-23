"""Reglas del alta de tanda que no necesitan la base (#27).

Viven aca, separadas del repositorio, para probarlas sin PostgreSQL y para
que haya un solo lugar donde tocarlas (docs/dominio.md, "Materiales y
consumo").
"""

from __future__ import annotations

from datetime import date, timedelta

# Cupulas de un cuadro de madera: cada cuadro tiene listones y cada liston
# 15 cupulas. AMBIGUEDAD ABIERTA (docs/dominio.md, no resolver): no esta
# claro si un cuadro son 135 cupulas o si las 135 son entre los tres cuadros.
# Repreguntado a la clienta; mientras tanto, este es el UNICO lugar a tocar.
CUPULAS_POR_CUADRO = 135

# El alta recibe cuadros, nunca un numero libre de cupulas.
CUADROS_MINIMOS = 1
CUADROS_MAXIMOS = 3

# date.weekday(): lunes es 0 y domingo es 6.
DOMINGO = 6


def cupulas_para(cuadros: int) -> int:
    """Cupulas traslarvadas: una celda y una cupula consumida por cada una."""
    if not CUADROS_MINIMOS <= cuadros <= CUADROS_MAXIMOS:
        raise ValueError(
            f"La cantidad de cuadros va de {CUADROS_MINIMOS} a {CUADROS_MAXIMOS}."
        )
    return cuadros * CUPULAS_POR_CUADRO


def es_dia_laborable(fecha: date) -> bool:
    """El domingo no es laborable para las tareas humanas, como el traslarve.

    No aplica a las fechas biologicas de la celda (nacimiento, fecundacion,
    extravio): esas son dias calendario (#28).
    """
    return fecha.weekday() != DOMINGO


def fecha_de_traslarve_admitida(fecha: date, *, retroactivo: bool) -> bool:
    """Un traslarve en domingo se rechaza, salvo en un registro retroactivo.

    El registro retroactivo es la carga tardia de una tanda ya hecha: se
    acepta tal cual, aunque la fecha caiga en domingo, y queda marcada.
    """
    return retroactivo or es_dia_laborable(fecha)


# Eje B de docs/dominio.md ("Ciclo de cria"): la celda se introduce ~D10 e
# I0+16 queda enjaulada, lista para despacho o para pasar a un banco: ≈ D26
# contado desde el traslarve (D0). Es una estimacion para planificar, no una
# fecha registrada: la real es la de la cosecha.
DIAS_HASTA_LA_SALIDA = 26


def fecha_de_salida_estimada(fecha_traslarve: date) -> date:
    """Cuando se espera cosechar la tanda: el traslarve mas ~26 dias (D26)."""
    return fecha_traslarve + timedelta(days=DIAS_HASTA_LA_SALIDA)
