"""Contrato de la API de datos maestros de produccion (#30, #29).

Razas, madres, parques de fecundacion, nucleos, materiales (con sus
movimientos) e items vendibles. Ninguno lleva tenant_id: lo completa
PostgreSQL con el tenant de la sesion (ADR 0009), y extra="forbid" rechaza
cualquier campo de mas, incluido ese.

Los schemas de edicion (PATCH) no repiten reglas: el repositorio aplica los
cambios sobre lo guardado y valida el resultado con el schema de alta.
"""

from __future__ import annotations

import unicodedata
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.fechas import hoy
from app.services.documentos.importes import Alicuota
from app.services.produccion.tandas import CUADROS_MAXIMOS, CUADROS_MINIMOS

# Tope fisico razonable para fila, posicion y capacidad de un parque.
MAXIMO_UBICACION = 1000
MAXIMO_CAPACIDAD = 10_000
# numeric(12, 3) de la migracion 0004: 9 enteros y 3 decimales.
MAXIMO_CANTIDAD = Decimal("999999999.999")


def _sin_caracteres_de_control(valor: str) -> str:
    """Un byte NUL llega a PostgreSQL y da 500; un salto de linea ensucia logs."""
    if any(unicodedata.category(caracter) == "Cc" for caracter in valor):
        raise ValueError("El texto no puede tener caracteres de control.")
    return valor


def _texto(maximo: int) -> object:
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=maximo)


Nombre = Annotated[str, _texto(100), AfterValidator(_sin_caracteres_de_control)]
Identificacion = Annotated[str, _texto(50), AfterValidator(_sin_caracteres_de_control)]
Ubicacion = Annotated[str, _texto(200), AfterValidator(_sin_caracteres_de_control)]
Descripcion = Annotated[str, _texto(500), AfterValidator(_sin_caracteres_de_control)]
# Igual que el CHECK ck_material_unidad de la 0004: texto corto, sin enum.
Unidad = Annotated[str, _texto(20), AfterValidator(_sin_caracteres_de_control)]
Ordinal = Annotated[int, Field(ge=1, le=MAXIMO_UBICACION, strict=True)]
Capacidad = Annotated[int, Field(ge=1, le=MAXIMO_CAPACIDAD, strict=True)]
Cantidad = Annotated[
    Decimal,
    Field(
        max_digits=12,
        decimal_places=3,
        ge=-MAXIMO_CANTIDAD,
        le=MAXIMO_CANTIDAD,
        allow_inf_nan=False,
    ),
]


class _Entrada(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Advertencia(BaseModel):
    """Aviso que no bloquea la operacion (#29; el alta de tanda lo reusa, #27).

    La operacion se hizo igual: el frontend lo muestra y sigue.
    """

    code: str
    message: str
    details: dict[str, Any] = {}


# --- raza ---


class RazaCrear(_Entrada):
    nombre: Nombre
    descripcion: Descripcion | None = None


class RazaActualizar(_Entrada):
    nombre: Nombre | None = None
    # null borra la descripcion.
    descripcion: Descripcion | None = None
    # true reactiva una raza dada de baja.
    activo: bool | None = None


class RazaRespuesta(BaseModel):
    id: UUID
    nombre: str
    descripcion: str | None
    activo: bool


# --- madre (madre reina) ---


class MadreCrear(_Entrada):
    identificacion: Identificacion


class MadreActualizar(_Entrada):
    identificacion: Identificacion | None = None
    activo: bool | None = None


class MadreRespuesta(BaseModel):
    id: UUID
    identificacion: str
    activo: bool


# --- parque de fecundacion ---


class ParqueCrear(_Entrada):
    nombre: Nombre
    # Texto simple: los parques pueden estar en distintos predios.
    ubicacion: Ubicacion
    # Cuantos nucleos activos entran en el parque.
    capacidad_max: Capacidad


class ParqueActualizar(_Entrada):
    nombre: Nombre | None = None
    ubicacion: Ubicacion | None = None
    capacidad_max: Capacidad | None = None
    activo: bool | None = None


class ParqueRespuesta(BaseModel):
    id: UUID
    nombre: str
    ubicacion: str
    capacidad_max: int
    # Se calcula al leer, nunca se guarda.
    nucleos_activos: int
    activo: bool


# --- nucleo ---


class NucleoCrear(_Entrada):
    parque_id: UUID
    fila: Ordinal
    posicion: Ordinal


class NucleoActualizar(_Entrada):
    """El parque no se cambia: para mover un nucleo, se da de baja y se crea."""

    fila: Ordinal | None = None
    posicion: Ordinal | None = None
    activo: bool | None = None


class NucleoRespuesta(BaseModel):
    id: UUID
    parque_id: UUID
    fila: int
    posicion: int
    activo: bool


class NucleoGuardado(NucleoRespuesta):
    """Respuesta del alta y de la edicion: suma las advertencias (#29)."""

    advertencias: list[Advertencia] = []


# --- banco (reinas enjauladas listas para despacho; dominio del 16/10) ---

# Confirmada por la clienta el 16/10; igual que el DEFAULT de la migracion 0009.
CAPACIDAD_BANCO_POR_DEFECTO = 276


class BancoCrear(_Entrada):
    identificador: Identificacion
    # En reinas (no en nucleos, como el parque).
    capacidad_max: Capacidad = CAPACIDAD_BANCO_POR_DEFECTO


class BancoActualizar(_Entrada):
    identificador: Identificacion | None = None
    capacidad_max: Capacidad | None = None
    activo: bool | None = None


class BancoRespuesta(BaseModel):
    id: UUID
    identificador: str
    capacidad_max: int
    # Celdas enjauladas en el banco: se cuenta al leer, nunca se guarda.
    reinas_en_banco: int
    activo: bool


# --- material ---


class MaterialCrear(_Entrada):
    # La cupula suelta, como se compra (docs/dominio.md).
    nombre: Nombre
    unidad: Unidad


class MaterialActualizar(_Entrada):
    nombre: Nombre | None = None
    unidad: Unidad | None = None
    activo: bool | None = None


class MaterialRespuesta(BaseModel):
    id: UUID
    nombre: str
    unidad: str
    # SUM de los movimientos, calculado al leer; puede ser negativo.
    stock: Decimal
    activo: bool


# --- movimiento de material ---


class MovimientoCrear(_Entrada):
    """Compra o ajuste cargado a mano.

    El consumo no se carga aca: lo registra el alta de tanda (#27), con la
    tanda que lo genero.
    """

    tipo: Literal["compra", "ajuste"]
    # Con signo: la compra es positiva; el ajuste, positivo o negativo.
    cantidad: Cantidad
    motivo: Descripcion | None = None
    # Dia del movimiento; puede ser anterior a hoy (se cargan tarde).
    fecha: date | None = None

    @model_validator(mode="after")
    def validar_segun_tipo(self) -> Self:
        if self.cantidad == 0:
            raise ValueError("La cantidad no puede ser cero.")
        if self.tipo == "compra" and self.cantidad < 0:
            raise ValueError("Una compra suma stock: la cantidad va en positivo.")
        if self.tipo == "ajuste" and self.motivo is None:
            raise ValueError("Un ajuste necesita un motivo.")
        # hoy() con la hora de Argentina: el servidor esta en UTC.
        if self.fecha is not None and self.fecha > hoy():
            raise ValueError("La fecha del movimiento no puede ser futura.")
        return self


class MovimientoRespuesta(BaseModel):
    id: UUID
    material_id: UUID
    tipo: str
    cantidad: Decimal
    tanda_id: UUID | None
    motivo: str | None
    fecha: date


class MovimientoGuardado(MovimientoRespuesta):
    """Respuesta del alta: el stock que queda y, si es negativo, un aviso."""

    stock: Decimal
    advertencias: list[Advertencia] = []


# --- item vendible ---


def _alicuota_valida(valor: Decimal) -> Decimal:
    """Solo valores del enum Alicuota de importes.py (igual que el CHECK)."""
    try:
        return Alicuota(valor).value
    except ValueError:
        permitidas = ", ".join(str(a.value) for a in Alicuota)
        raise ValueError(f"La alicuota tiene que ser una de: {permitidas}.") from None


AlicuotaIva = Annotated[Decimal, AfterValidator(_alicuota_valida)]


class ItemVendibleCrear(_Entrada):
    nombre: Nombre
    # 21% por defecto: confirmado por la clienta el 11/10 (docs/dominio.md).
    alicuota: AlicuotaIva = Alicuota.IVA_21.value


class ItemVendibleActualizar(_Entrada):
    nombre: Nombre | None = None
    alicuota: AlicuotaIva | None = None
    activo: bool | None = None


class ItemVendibleRespuesta(BaseModel):
    id: UUID
    nombre: str
    alicuota: Decimal
    activo: bool

    @field_validator("alicuota")
    @classmethod
    def sin_ceros_de_mas(cls, valor: Decimal) -> Decimal:
        # La base guarda numeric(5, 3): 0.210 sale como 0.21.
        return Alicuota(valor).value


# --- tanda (#27) ---

# Entero estricto: "2" o 2.5 son 422, igual que fila y posicion del nucleo.
Cuadros = Annotated[int, Field(ge=CUADROS_MINIMOS, le=CUADROS_MAXIMOS, strict=True)]


class TandaCrear(_Entrada):
    """Alta de tanda: cuadros de madera, nunca un numero libre de cupulas.

    La tanda se identifica por su raza (dominio del 16/10): la madre es
    opcional, porque las madres quedan fuera de alcance.

    El domingo se valida en el repositorio, no aca: tiene su propio codigo
    de error (fecha_no_laborable) y aca saldria como "validacion".
    """

    raza_id: UUID
    # Material de cupulas del que se descuenta el consumo.
    material_id: UUID
    fecha_traslarve: date
    cuadros: Cuadros
    # Opcional desde el 16/10 (tanda.madre_id NULL).
    madre_id: UUID | None = None
    # Ubicacion de los primeros 10 dias en la continuadora: las dos o
    # ninguna (una tanda retroactiva puede no saberlas), como el CHECK
    # ck_tanda_continuadora de la 0009.
    fila_continuadora: Ordinal | None = None
    posicion_continuadora: Ordinal | None = None
    # Carga tardia de una tanda ya hecha: admite domingo.
    retroactivo: bool = False

    @model_validator(mode="after")
    def validar_tanda(self) -> Self:
        # hoy() con la hora de Argentina: el servidor esta en UTC.
        hoy_ar = hoy()
        # El consumo toma esta fecha, y un movimiento no puede ser futuro.
        if self.fecha_traslarve > hoy_ar:
            raise ValueError("La fecha de traslarve no puede ser futura.")
        # Si no, marcar retroactivo saltearia la regla del domingo de hoy.
        if self.retroactivo and self.fecha_traslarve == hoy_ar:
            raise ValueError("Un registro retroactivo lleva una fecha pasada.")
        if (self.fila_continuadora is None) != (self.posicion_continuadora is None):
            raise ValueError(
                "La fila y la posicion de la continuadora van las dos o ninguna."
            )
        return self


class TandaRespuesta(BaseModel):
    id: UUID
    raza_id: UUID
    madre_id: UUID | None
    fecha_traslarve: date
    retroactivo: bool
    fila_continuadora: int | None
    posicion_continuadora: int | None
    # COUNT(*) de sus celdas, calculado al leer: nunca una columna.
    celdas: int
    # Las mismas celdas agrupadas por estado (solo los estados que tienen
    # alguna), tambien calculado al leer. Ej.: {"trasladada": 135}.
    celdas_por_estado: dict[str, int]


class TandaCreada(TandaRespuesta):
    """Respuesta del alta: la tanda, su consumo y el stock que queda.

    Si el stock queda negativo, la tanda se crea igual y viene el aviso
    stock_negativo (docs/dominio.md: no se bloquea).
    """

    consumo: MovimientoRespuesta
    stock: Decimal
    advertencias: list[Advertencia] = []


# --- operaciones por lote sobre la tanda (#28) ---

# Tope de una operacion: alcanza de sobra para 3 cuadros (405 celdas) y evita
# pedidos absurdos.
MAXIMO_CELDAS_POR_OPERACION = 10_000

CantidadCeldas = Annotated[
    int, Field(ge=1, le=MAXIMO_CELDAS_POR_OPERACION, strict=True)
]


class _OperacionConFecha(_Entrada):
    # Dia calendario: el domingo no aplica (docs/dominio.md, 23/10).
    fecha: date

    @field_validator("fecha")
    @classmethod
    def no_futura(cls, fecha: date) -> date:
        # hoy() con la hora de Argentina: el servidor esta en UTC.
        if fecha > hoy():
            raise ValueError("La fecha no puede ser futura.")
        return fecha


class OperacionDescartar(_OperacionConFecha):
    """Descartar celdas que todavia no se introdujeron (quedan sin nucleo)."""

    accion: Literal["descartar"]
    cantidad: CantidadCeldas


class OperacionIntroducir(_OperacionConFecha):
    """Introducir en un parque: el sistema elige los nucleos, nunca el usuario."""

    accion: Literal["introducir"]
    cantidad: CantidadCeldas
    parque_id: UUID


class OperacionCosechar(_OperacionConFecha):
    """Cosechar la tanda: una sola vez, en un dia.

    cantidad son las reinas cosechadas; el resto de las celdas del parque
    pasa solo a descartada. Puede ser 0 (se perdio todo).
    """

    accion: Literal["cosechar"]
    cantidad: Annotated[int, Field(ge=0, le=MAXIMO_CELDAS_POR_OPERACION, strict=True)]


class OperacionPasarABanco(_Entrada):
    """Pasar reinas enjauladas que todavia no estan en un banco."""

    accion: Literal["pasar_a_banco"]
    cantidad: CantidadCeldas
    banco_id: UUID


# El campo accion elige el tipo: un cuerpo con accion desconocida es 422.
OperacionCrear = Annotated[
    OperacionDescartar | OperacionIntroducir | OperacionCosechar | OperacionPasarABanco,
    Field(discriminator="accion"),
]


class RepartoNucleo(BaseModel):
    """Cuantas celdas fueron a un nucleo en esta introduccion."""

    nucleo_id: UUID
    fila: int
    posicion: int
    celdas: int


class OperacionRealizada(BaseModel):
    accion: str
    cantidad: int
    # La tanda despues de la operacion, con sus celdas por estado.
    tanda: TandaRespuesta
    # Solo al introducir: el reparto, en orden de fila y posicion.
    reparto: list[RepartoNucleo] | None = None
    # Solo al cosechar: las celdas del parque que no se cosecharon.
    descartadas_en_cosecha: int | None = None
