"""Planes de varios pasos sin base: marcadores, dependencias y resolución de
nombres contra la base y las propuestas del turno."""

from __future__ import annotations

import uuid

import pytest

from app.schemas.asistente import AccionPropuesta, ParResumen, Propuesta
from app.services.ia.asistente import plan
from app.services.ia.asistente.resolucion import ErrorDeHerramienta, Opcion


def _propuesta(accion: AccionPropuesta, nombre: str) -> Propuesta:
    return Propuesta(
        id=uuid.uuid4(),
        accion=accion,
        ruta="/x",
        cuerpo={"nombre": nombre},
        resumen=[ParResumen(etiqueta="Cliente", valor="Miel SA")],
        roles_permitidos=["admin"],
    )


def test_el_marcador_va_solo_donde_va_un_id() -> None:
    propuesta_id = uuid.uuid4()
    texto = plan.marcador(propuesta_id)

    assert texto == f"{{{{ref:{propuesta_id}.id}}}}"
    assert plan.MARCADOR.fullmatch(texto)
    assert not plan.MARCADOR.fullmatch("{{ref:no-es-un-uuid.id}}")
    assert not plan.MARCADOR.fullmatch(f"{{{{ref:{propuesta_id}.nombre}}}}")


def test_dependencias_de_ruta_y_cuerpo_sin_repetir() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    ruta = f"/api/v1/pedidos/{plan.marcador(a)}/reservas"
    cuerpo = {"items": [{"raza_id": plan.marcador(b)}, {"raza_id": plan.marcador(b)}]}

    assert plan.dependencias(ruta, cuerpo) == [a, b]
    assert plan.dependencias("/api/v1/razas", {"nombre": "x"}) == []


def test_uuids_de_prueba_y_vuelta_a_los_marcadores() -> None:
    a = uuid.uuid4()
    cuerpo = {"cliente_id": plan.marcador(a), "otro": "texto", "n": 3}

    prueba, inverso = plan.con_uuids_de_prueba(cuerpo)

    uuid.UUID(prueba["cliente_id"])
    assert prueba["otro"] == "texto" and prueba["n"] == 3
    assert plan.sustituir(prueba, inverso) == cuerpo


def test_resolver_prefiere_la_base_y_despues_el_turno() -> None:
    base = [Opcion(uuid.uuid4(), "Parque Norte")]
    propuesto = _propuesta(AccionPropuesta.CREAR_PARQUE, "Casa Santi")
    pendientes = plan.pendientes_de([propuesto], AccionPropuesta.CREAR_PARQUE)

    assert (
        plan.resolver("parque norte", base, pendientes, "el parque").pendiente is None
    )
    elegido = plan.resolver("casa santi", base, pendientes, "el parque")
    assert elegido.pendiente == propuesto
    assert elegido.id == plan.marcador(propuesto.id)


@pytest.mark.parametrize(
    ("nombre", "mensaje"),
    [("Casa", "ambiguo"), ("Loma", 'No existe el parque "Loma"'), (" ", "Falta")],
)
def test_resolver_ambiguo_inexistente_o_vacio(nombre: str, mensaje: str) -> None:
    pendientes = plan.pendientes_de(
        [
            _propuesta(AccionPropuesta.CREAR_PARQUE, "Casa Santi"),
            _propuesta(AccionPropuesta.CREAR_PARQUE, "Casa Grande"),
        ],
        AccionPropuesta.CREAR_PARQUE,
    )

    with pytest.raises(ErrorDeHerramienta, match=mensaje):
        plan.resolver(nombre, [], pendientes, "el parque")


def test_pendiente_por_id_y_accion() -> None:
    propuesta = _propuesta(AccionPropuesta.CREAR_PEDIDO, "x")

    assert plan.pendiente([propuesta], propuesta.id, AccionPropuesta.CREAR_PEDIDO)
    assert not plan.pendiente([propuesta], propuesta.id, AccionPropuesta.CREAR_RAZA)
    assert plan.valor_del_resumen(propuesta, "Cliente") == "Miel SA"
    assert plan.valor_del_resumen(propuesta, "Nada") == "?"
