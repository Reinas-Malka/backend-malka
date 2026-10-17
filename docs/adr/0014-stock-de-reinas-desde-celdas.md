# ADR 0014 — El stock de reinas se calcula desde las celdas, sin libro de movimientos

- **Estado:** aceptada
- **Fecha:** 2026-10-16
- **Decisores:** Felipe Andreau (dueño), con la confirmación de la clienta del 15 y 16/10
- **Reemplaza:** el libro `movimiento_reinas` que planteaban los issues #33, #35 y #36

## Contexto

Los issues de ventas (#33, #35, #36) se escribieron el 26/09 con un libro de
movimientos de reinas (`ingreso`, `reserva`, `liberacion`, `egreso`), con el
banco como dueño del stock (`banco_id` en los índices y en el lock de
reservas). Desde entonces cambiaron dos cosas:

1. **15/10:** la clienta aclaró que un pedido no sale de un banco fijo: al
   despachar se eligen reinas de uno o varios bancos. La reserva, entonces, no
   puede tener banco.
2. **16/10:** el dominio actualizado reincorpora el banco a la fase 1 como
   **ubicación de cada celda** (`celda.banco_id`) y exige una fila por celda,
   con operaciones por lote.

Con una fila por reina y su estado (`enjaulada`, `despachada`, …), el stock
físico ya está en la tabla `celda`. Un libro paralelo sería una **segunda
fuente de verdad** que habría que mantener sincronizada con el estado de las
celdas: cada despacho tendría que escribir las dos cosas, y un error en
cualquiera de las dos desalinea el disponible.

Además, la fórmula del libro (`ingresos − reservas + liberaciones − egresos`)
descontaba dos veces al entregar si el egreso no liberaba la reserva.

## Decisión

1. **No hay tabla `movimiento_reinas`.** El stock físico de una raza es
   `COUNT(*)` de celdas `enjaulada` de tandas de esa raza.
2. **Las reservas van por raza, sin banco**, en una tabla propia ligada a la
   línea del pedido (#35). Una reserva está activa hasta que el pedido se
   despacha o se cancela.
3. **Disponible por raza** = celdas enjauladas de la raza − reservas activas
   de la raza. Se calcula siempre: nunca es una columna.
4. **Despachar** (#36) = elegir celdas enjauladas de uno o varios bancos y
   pasarlas a `despachada`, por lote. La reserva de esas líneas deja de estar
   activa en la misma transacción. **Cancelar** libera las reservas.
5. **Existencia por banco** = celdas enjauladas con ese `banco_id`. Es solo
   ubicación física; no hay "disponible por banco".
6. **Contra la sobreventa** (#35): la reserva toma un lock por
   `(tenant, raza)` (`pg_advisory_xact_lock`), recalcula el disponible dentro
   de la transacción y responde 409 si no alcanza.

## Alternativa descartada

**Libro `movimiento_reinas` por raza, con el banco solo como ubicación.**
Cambiaba menos los issues de Santi, pero dejaba dos registros del mismo hecho
(la celda despachada y el egreso) que tenían que coincidir siempre.

## Consecuencias

- **#33** se reescribe: `GET /disponibilidad` se calcula desde celdas y
  reservas; no crea tabla de movimientos.
- **#35** crea la tabla de reservas y el lock por `(tenant, raza)`, no por
  `(banco_id, raza_id)`.
- **#36** despacha eligiendo celdas por banco y cierra las reservas en la
  misma transacción.
- **#28** (transiciones por lote) es la única puerta de entrada al stock: el
  paso a `enjaulada` es lo que suma una reina disponible.
- Frontend #12, #13 y #14 se alinean a este modelo.
- Si algún día hace falta auditar cada cambio de estado, se agrega una tabla
  de eventos **derivada** de las transiciones, no un segundo stock.
