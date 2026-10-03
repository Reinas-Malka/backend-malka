# ADR 0006 — Cálculo de importes con Decimal, redondeo HALF_UP e IVA por alícuota

- **Estado:** aceptada
- **Fecha:** 2026-10-02
- **Decisores:** equipo Reinas Malka
- **Issues:** #41 (lo usan #34 y #42)

## Contexto

La regla del brief es que **la IA redacta y el código calcula**. El borrador con Bedrock (#40) rechaza cualquier salida del modelo que incluya `total`, `iva` o `tipo_cambio`, así que esos valores los tiene que producir el sistema, de forma exacta, reproducible y auditable.

Hay tres trampas al calcular plata en Python:

- `float` no representa bien los decimales (`0.1 + 0.2 = 0.30000000000000004`), y esos errores terminan en centavos que no cierran.
- El redondeo por defecto de `Decimal` es `ROUND_HALF_EVEN` (redondeo bancario): 0,525 pasa a 0,52, que no es lo que espera quien controla una factura a mano.
- Redondear el IVA de cada línea o el de la suma de una alícuota da resultados distintos. Con tres líneas de $0,12 al 21%, da 0,09 por línea y 0,08 por alícuota.

## Decisión

Un módulo de dominio, `app/services/documentos/importes.py`, sin dependencias de base de datos, FastAPI ni AWS.

- **Siempre `Decimal`.** `Linea` rechaza un precio que llegue como `float`.
- **Redondeo `ROUND_HALF_UP` a centavos**, aplicado en un único helper, `redondear()`. Todo importe devuelto tiene exactamente dos decimales.
- **El neto de cada línea** (cantidad × precio unitario) se redondea a centavos.
- **El IVA se calcula una vez por alícuota**, sobre la suma de los netos de esa alícuota, como en el cuadro de IVA de una factura.
- **Alícuotas admitidas:** 0%, 2,5%, 5%, 10,5%, 21% y 27%, como un enum. Cualquier otra se rechaza.
- **Exportación exenta de IVA.** Con `exportacion=True`, todas las líneas se calculan al 0%, sin importar la alícuota que traigan.
- **Moneda extranjera:** el tipo de cambio es obligatorio y mayor que cero, y `total_en_pesos = redondear(total × tipo_cambio)`. En pesos no se informa tipo de cambio. De dónde sale la cotización lo decide quien llama.
- **Validaciones** con una excepción propia, `ImporteInvalido` (subclase de `ValueError`). El endpoint que use el módulo la traduce a `ValidacionError` (422). Se rechazan: un documento sin líneas, una cantidad no entera o menor que 1, un precio negativo, no finito o que no sea `Decimal`, y una alícuota fuera del enum. El precio cero es válido (muestra sin cargo, bonificación).
- **Serialización:** `Importes.como_dict()` pasa cada `Decimal` a texto (`"4481.45"`) para guardarlo en el `snapshot_json` del #42 sin volver a introducir floats.

### Quién decide qué

- **Si un pedido es de exportación lo decide el #34**, no este módulo. Exportación significa que la mercadería sale del país con su trámite aduanero. Un cliente extranjero que retira acá, o una venta nacional facturada en dólares, **no** es exportación y lleva IVA. El #34 debería permitir `exportacion=True` solo con un cliente de tipo exportación y con `incoterm` y `destino` cargados.
- **Cómo se muestra lo decide el #42.** En una factura E corresponde "Exento" o directamente no mostrar cuadro de IVA, aunque internamente esas líneas se agrupen en `IVA_0`.

### Fuera de alcance

- Los derechos de exportación y los reintegros los liquida la aduana y no van en la factura E.
- El recupero del IVA de las compras del exportador es contabilidad de la empresa.
- El número de comprobante y el CAE se asignan al aprobar (#42).

## Consecuencias

**A favor**

- Cálculo exacto al centavo y reproducible: el mismo pedido da siempre el mismo resultado.
- Cambiar el criterio de redondeo es cambiar una línea.
- Se prueba sin nada levantado: 31 tests con casos calculados a mano y 100% de cobertura del módulo.
- En exportación no se puede cobrar IVA por olvido de quien arma las líneas.
- El snapshot es legible por personas y se reconstruye exacto con `Decimal(...)`.

**En contra**

- Si alguien marca `exportacion=True` por error en una venta nacional, la factura sale sin IVA. Lo mitigan la validación en el #34, el campo `exportacion` guardado en el snapshot y la aprobación humana de cada documento (#42).
- Exento y 0% se tratan igual (`IVA_0`). No pueden ser dos miembros del enum con el mismo valor, porque en Python el segundo se vuelve alias del primero. Si la factura tiene que distinguirlos, hay que modelar ese concepto aparte.
- Como el IVA se redondea por alícuota, si el PDF mostrara un IVA por línea, la suma de esas líneas podría no coincidir con el cuadro por algunos centavos.
- **Pendiente de confirmar con quien lleve la contabilidad de Malka:** la alícuota de las reinas en venta nacional, el tratamiento de exportación y la fuente y fecha de la cotización.

## Alternativas consideradas

**`float`.** Más simple, pero introduce errores de representación. Descartada: es la regla número uno del issue.

**`ROUND_HALF_EVEN` (el default de Python).** Reduce el sesgo estadístico en grandes volúmenes, pero no coincide con la cuenta que hace una persona a mano (0,525 da 0,52). Descartada.

**IVA por línea.** Cada línea queda autocontenida, pero los redondeos se acumulan y el total se aparta del cuadro de IVA de la factura. Descartada.

**Rechazar las líneas de exportación que no vengan al 0%.** Más estricta, pero obliga a cada llamador a reescribir las alícuotas y reparte la regla en varios lugares. Se eligió forzar el 0% y concentrar en el #34 la decisión de qué es exportación.

**Usar `ValidacionError` de `app/errores.py`.** Ese módulo importa FastAPI, lo que metería infraestructura en el dominio. Descartada.

**Que el modelo de IA calcule los importes.** Descartada por la regla del brief: el modelo puede equivocarse con los números y una factura no admite errores.
