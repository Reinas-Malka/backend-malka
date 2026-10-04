# ADR 0008 — Alcance del cálculo de importes: pedido interno vs comprobante fiscal

- **Estado:** aceptada
- **Amends:** [0006 — Cálculo de importes](0006-calculo-de-importes.md) (parcial: no la sustituye)
- **Fecha:** 2026-10-04
- **Decisores:** equipo Reinas Malka (regla confirmada por la clienta)

## Contexto

El ADR 0006 estableció el cálculo de importes del sistema con `Decimal` y
redondeo `HALF_UP`. Después, la clienta confirmó (2 y 4/10, `docs/dominio.md`)
cómo funciona la emisión real de comprobantes. Dos citas, verbatim:

> ADR 0006, "Fuera de alcance": **"El número de comprobante y el CAE se
> asignan al aprobar (#42)."**

> `docs/dominio.md`, Facturación: **"El número lo asigna ARCA al generar el
> comprobante."**

Son incompatibles: el sistema no asigna ningún número fiscal. La salida no es
derogar el 0006 entero sino acotar su alcance: son **dos entidades**.

## Decisión

**`pedido` (interno).** El sistema CALCULA netos, IVA por alícuota y totales
(ADR 0006 vigente sin cambios). Sobre eso la IA redacta borradores
(`prompts/borrador_v1.txt` se mantiene) y #40 sigue rechazando salidas del
modelo con `total`/`iva`/`tipo_cambio`.

**`comprobante` (externo — Factura A/B/E de ARCA; la letra la asigna ARCA
según la condición fiscal del cliente).** `punto_venta`, `numero`,
`moneda`, `importe`, `tipo_cambio` y CAE son **datos de entrada transcriptos
del comprobante e inmutables**. Nunca calculados, generados ni recalculados.
Flujo: se emite en ARCA → se sube el PDF → el sistema registra la metadata.

**Se deroga del 0006, textualmente:** "El número de comprobante y el CAE se
asignan al aprobar (#42)". El número lo asigna ARCA al emitir, por fuera del
sistema; el CAE, si figura, se transcribe. En #42 la aprobación humana
registra y congela lo transcripto; no numera.

**Sobre `total_en_pesos`:** vale como valor derivado de consulta
(`redondear(total × tipo_cambio)`), pero `tipo_cambio` es un input
transcripto — lo fija ARCA al confeccionar la factura — y la moneda del
comprobante puede ser **USD o EUR**: no se asume ARS como única moneda
destino ni USD como única extranjera.

## Consecuencias

- No existe ninguna secuencia de numeración propia; la unicidad es
  `UNIQUE (tenant_id, punto_venta, numero)` (modelo de `docs/dominio.md`).
- `importes.py` no cambia: calcula pedidos, no comprobantes.
- #42 (aprobación) transcribe y congela; su numeración propia queda fuera.
- Los issues de modelos (#26/#27/#28) se abordan recién con este ADR merged.

## Alternativas consideradas

**Derogar el 0006 completo.** Descartada: el cálculo del pedido interno sigue
siendo correcto y necesario ("la IA redacta y el código calcula").

**Tratar el comprobante como un pedido congelado.** Descartada: mezcla una
entidad calculada con una transcripta y reintroduce la numeración propia por
la puerta de atrás.
