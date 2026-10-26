"""Acceso a tanda, celda y al consumo de material del alta (#27).

Reglas de docs/dominio.md (confirmado 07/10 y 16/10):

- La tanda se identifica por su raza. La madre es opcional: las madres
  quedan fuera de alcance (16/10). Si viene, tiene que existir y estar activa.
- El alta recibe cuadros de madera (1, 2 o 3) y crea una fila de celda por
  cupula traslarvada (cuadros x CUPULAS_POR_CUADRO), en estado trasladada, sin
  nucleo y sin banco. La cantidad de celdas no se guarda: es COUNT(*) al leer.
- La ubicacion de los primeros 10 dias (fila y posicion en la continuadora)
  va en la propia tanda, las dos o ninguna.
- El descuento de material es automatico: un movimiento de consumo, con la
  tanda que lo genero, en la misma transaccion. El stock puede quedar
  negativo: no se bloquea, la respuesta trae la advertencia stock_negativo.
- fecha_traslarve en domingo es 422 fecha_no_laborable, salvo un registro
  retroactivo (carga tardia de una tanda ya hecha).
- Las celdas de la tanda (total y por estado) se cuentan al leer. El detalle
  suma donde estan: los nucleos con celdas en el parque y los bancos con
  reinas cosechadas, tambien contados al leer.

Todo pasa en la transaccion de obtener_sesion: si algo falla, no queda ni la
tanda, ni sus celdas, ni el consumo. Como en el resto de los repositorios,
ninguna consulta filtra por tenant: RLS se encarga (ADR 0009).

Los ids del cuerpo que no existen son 422, no 404 (misma convencion que
pedidos, #34): el recurso de la URL existe, lo que esta mal es un dato. Una
raza, madre o material de otro criadero responde igual que uno inexistente.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.errores import NoEncontradoError, ValidacionError
from app.paginacion import Pagina, ParametrosDePagina, armar_pagina, valores_del_cursor
from app.schemas.produccion import (
    Advertencia,
    BancoDeTanda,
    MovimientoRespuesta,
    NucleoDeTanda,
    TandaCreada,
    TandaCrear,
    TandaDetalle,
    TandaRespuesta,
)
from app.services.produccion import repositorio_materiales
from app.services.produccion.tandas import cupulas_para, fecha_de_traslarve_admitida

# FOR SHARE: mientras se crea la tanda, nadie da de baja la raza ni la madre.
LEER_RAZA = text("SELECT activo FROM raza WHERE id = :id FOR SHARE")
LEER_MADRE = text("SELECT activo FROM madre WHERE id = :id FOR SHARE")
# FOR UPDATE, como en los movimientos a mano: dos altas simultaneas con el
# mismo material calculan el stock en orden y las dos ven el descuento de la
# otra.
LEER_MATERIAL = text("SELECT activo FROM material WHERE id = :id FOR UPDATE")

INSERTAR_TANDA = text(
    "INSERT INTO tanda (raza_id, madre_id, fecha_traslarve, retroactivo, "
    "fila_continuadora, posicion_continuadora) "
    "VALUES (:raza_id, :madre_id, :fecha_traslarve, :retroactivo, "
    ":fila_continuadora, :posicion_continuadora) "
    "RETURNING id"
)

# Una fila por cupula en un solo INSERT. estado (trasladada), nucleo_id y
# banco_id (NULL) quedan en el default de la columna.
INSERTAR_CELDAS = text(
    "INSERT INTO celda (tanda_id) "
    "SELECT CAST(:tanda_id AS uuid) FROM generate_series(1, :cantidad)"
)

# El consumo va con signo negativo (ck_movimiento_signo) y con la fecha del
# traslarve: el dia en que se usaron las cupulas, no el de la carga.
INSERTAR_CONSUMO = text(
    "INSERT INTO movimiento_material (material_id, tipo, cantidad, tanda_id, fecha) "
    "VALUES (:material_id, 'consumo', :cantidad, :tanda_id, :fecha) "
    "RETURNING id, material_id, tipo, cantidad, tanda_id, motivo, fecha"
)

OBTENER = text(
    "SELECT t.id, t.raza_id, t.madre_id, t.fecha_traslarve, t.retroactivo, "
    "t.fila_continuadora, t.posicion_continuadora "
    "FROM tanda t WHERE t.id = :id"
)

# Celdas por estado de varias tandas en una sola consulta (no una por tanda).
# RLS tambien filtra celda.
CONTAR_CELDAS = text(
    "SELECT tanda_id, estado, count(*) AS cantidad FROM celda "
    "WHERE tanda_id = ANY(:ids) GROUP BY tanda_id, estado"
)

# Nucleos con celdas de la tanda que siguen en el parque (introducida): al
# cosecharse, las celdas salen y el nucleo queda libre. RLS filtra celda y
# nucleo, asi que nunca aparece uno de otro criadero.
NUCLEOS_DE_LA_TANDA = text(
    "SELECT n.id AS nucleo_id, n.parque_id, n.fila, n.posicion, "
    "count(*) AS celdas "
    "FROM celda c JOIN nucleo n ON n.id = c.nucleo_id "
    "WHERE c.tanda_id = :tanda AND c.estado = 'introducida' "
    "GROUP BY n.id, n.parque_id, n.fila, n.posicion "
    "ORDER BY n.parque_id, n.fila, n.posicion"
)

# Bancos con reinas cosechadas de la tanda: las enjauladas con banco, igual
# que reinas_en_banco del banco (una despachada ya no esta en el banco).
BANCOS_DE_LA_TANDA = text(
    "SELECT b.id AS banco_id, b.identificador, count(*) AS reinas "
    "FROM celda c JOIN banco b ON b.id = c.banco_id "
    "WHERE c.tanda_id = :tanda AND c.estado = 'enjaulada' "
    "GROUP BY b.id, b.identificador "
    "ORDER BY b.identificador"
)

# Filtros opcionales: con el parametro en NULL, la condicion deja pasar todo
# (el CAST es obligatorio, igual que en pedidos). De la tanda mas nueva a la
# mas vieja por fecha de traslarve; despues por fecha de carga; el id
# desempata. El cursor es esa terna de la ultima fila.
LISTAR = text(
    "SELECT t.id, t.raza_id, t.madre_id, t.fecha_traslarve, t.retroactivo, "
    "t.fila_continuadora, t.posicion_continuadora, t.creado_en "
    "FROM tanda t "
    "WHERE (CAST(:raza_id AS uuid) IS NULL OR t.raza_id = :raza_id) "
    "AND (CAST(:desde AS date) IS NULL OR t.fecha_traslarve >= :desde) "
    "AND (CAST(:hasta AS date) IS NULL OR t.fecha_traslarve <= :hasta) "
    "AND (:sin_cursor OR (t.fecha_traslarve, t.creado_en, t.id) < "
    "(CAST(:c_fecha AS date), CAST(:c_creado AS timestamptz), CAST(:c_id AS uuid))) "
    "ORDER BY t.fecha_traslarve DESC, t.creado_en DESC, t.id DESC LIMIT :limite"
)


def _activo(sesion: Session, consulta: Any, id_: UUID) -> bool | None:
    """activo de la referencia; None si no existe (o es de otro criadero)."""
    fila = sesion.execute(consulta, {"id": id_}).one_or_none()
    return None if fila is None else bool(fila.activo)


def _validar(sesion: Session, datos: TandaCrear) -> None:
    if not fecha_de_traslarve_admitida(
        datos.fecha_traslarve, retroactivo=datos.retroactivo
    ):
        raise ValidacionError(
            "El traslarve no se hace los domingos. Si es la carga tardia de una "
            "tanda ya hecha, marcala como retroactiva.",
            code="fecha_no_laborable",
            details={"fecha_traslarve": datos.fecha_traslarve.isoformat()},
        )

    raza = _activo(sesion, LEER_RAZA, datos.raza_id)
    if raza is None:
        raise ValidacionError("No existe la raza.", code="raza_inexistente")
    if not raza:
        raise ValidacionError(
            "La raza esta dada de baja: no se le pueden cargar tandas.",
            code="raza_inactiva",
        )

    # La madre es opcional (16/10); si viene, tiene que ser valida.
    if datos.madre_id is not None:
        madre = _activo(sesion, LEER_MADRE, datos.madre_id)
        if madre is None:
            raise ValidacionError("No existe la madre.", code="madre_inexistente")
        if not madre:
            raise ValidacionError(
                "La madre esta dada de baja: no se le pueden cargar tandas.",
                code="madre_inactiva",
            )

    material = _activo(sesion, LEER_MATERIAL, datos.material_id)
    if material is None:
        raise ValidacionError("No existe el material.", code="material_inexistente")
    if not material:
        raise ValidacionError(
            "El material esta dado de baja: no admite consumos.",
            code="material_inactivo",
        )


def _celdas_por_tanda(sesion: Session, ids: list[UUID]) -> dict[UUID, dict[str, int]]:
    """Celdas por estado de cada tanda, contadas al leer (nunca guardadas)."""
    conteo: dict[UUID, dict[str, int]] = {id_: {} for id_ in ids}
    for fila in sesion.execute(CONTAR_CELDAS, {"ids": ids}):
        conteo[fila.tanda_id][fila.estado] = fila.cantidad
    return conteo


def _a_respuesta(fila: Row[Any], por_estado: dict[str, int]) -> TandaRespuesta:
    return TandaRespuesta.model_validate(
        {
            **fila._mapping,
            "celdas": sum(por_estado.values()),
            "celdas_por_estado": por_estado,
        }
    )


def crear(sesion: Session, datos: TandaCrear) -> TandaCreada:
    """Crea la tanda, sus celdas y el consumo de cupulas, todo junto."""
    _validar(sesion, datos)
    cupulas = cupulas_para(datos.cuadros)

    tanda_id = sesion.execute(
        INSERTAR_TANDA,
        datos.model_dump(exclude={"material_id", "cuadros"}),
    ).scalar_one()
    sesion.execute(INSERTAR_CELDAS, {"tanda_id": tanda_id, "cantidad": cupulas})
    consumo = sesion.execute(
        INSERTAR_CONSUMO,
        {
            "material_id": datos.material_id,
            "cantidad": -cupulas,
            "tanda_id": tanda_id,
            "fecha": datos.fecha_traslarve,
        },
    ).one()

    stock = repositorio_materiales.obtener(sesion, datos.material_id).stock
    advertencias = []
    if stock < 0:
        # El mismo aviso que un movimiento cargado a mano (#30).
        advertencias.append(
            Advertencia(
                code="stock_negativo",
                message="El stock del material quedo negativo.",
                details={"stock": str(stock)},
            )
        )
    return TandaCreada(
        **obtener(sesion, tanda_id).model_dump(),
        consumo=MovimientoRespuesta.model_validate(dict(consumo._mapping)),
        stock=stock,
        advertencias=advertencias,
    )


def obtener(sesion: Session, tanda_id: UUID) -> TandaRespuesta:
    """Una tanda con sus celdas por estado. 404 si no existe o es de otro."""
    fila = sesion.execute(OBTENER, {"id": tanda_id}).one_or_none()
    if fila is None:
        raise NoEncontradoError("No existe la tanda.", code="tanda_no_encontrada")
    return _a_respuesta(fila, _celdas_por_tanda(sesion, [tanda_id])[tanda_id])


def detalle(sesion: Session, tanda_id: UUID) -> TandaDetalle:
    """La tanda con sus celdas por estado y donde estan: nucleos y bancos."""
    tanda = obtener(sesion, tanda_id)
    parametros = {"tanda": tanda_id}
    return TandaDetalle(
        **tanda.model_dump(),
        nucleos=[
            NucleoDeTanda.model_validate(dict(fila._mapping))
            for fila in sesion.execute(NUCLEOS_DE_LA_TANDA, parametros)
        ],
        bancos=[
            BancoDeTanda.model_validate(dict(fila._mapping))
            for fila in sesion.execute(BANCOS_DE_LA_TANDA, parametros)
        ],
    )


def listar(
    sesion: Session,
    pagina: ParametrosDePagina,
    *,
    raza_id: UUID | None = None,
    desde: date | None = None,
    hasta: date | None = None,
) -> Pagina[TandaRespuesta]:
    """Tandas de la mas nueva a la mas vieja; desde y hasta incluyen el dia."""
    c_fecha, c_creado, c_id = valores_del_cursor(
        pagina, (date.fromisoformat, datetime.fromisoformat, UUID)
    )
    filas = sesion.execute(
        LISTAR,
        {
            "raza_id": raza_id,
            "desde": desde,
            "hasta": hasta,
            "sin_cursor": pagina.cursor is None,
            "c_fecha": c_fecha,
            "c_creado": c_creado,
            "c_id": c_id,
            # Uno de mas: si viene, hay pagina siguiente.
            "limite": pagina.limite + 1,
        },
    ).all()
    if not filas:
        return Pagina[TandaRespuesta](items=[])

    conteo = _celdas_por_tanda(sesion, [fila.id for fila in filas])
    # creado_en solo sirve para el cursor: no sale en la respuesta.
    creados = {fila.id: fila.creado_en for fila in filas}
    return armar_pagina(
        [_a_respuesta(fila, conteo[fila.id]) for fila in filas],
        pagina.limite,
        lambda tanda: (
            tanda.fecha_traslarve.isoformat(),
            creados[tanda.id].isoformat(),
            tanda.id,
        ),
    )
