# Dominio de Malka Suite — reglas confirmadas por la clienta

> Fuente de verdad del negocio. Confirmado por la clienta el 2 y 4/10/2026.
> Si algo acá contradice código o ADRs existentes, **gana este documento** y la
> contradicción se reporta como issue: no se "corrige" de forma silenciosa.

## Regla general

El sistema **no emite comprobantes fiscales ni aduaneros**. Los emiten ARCA,
SENASA, el despachante y la línea aérea, por fuera del sistema. Malka Suite es
el **registro y el repositorio documental** de esos comprobantes.

Todo número, moneda y tipo de cambio es un **dato de entrada transcripto del
comprobante**, nunca un valor calculado ni generado por el sistema. Y nunca se
recalcula retroactivamente.

## Facturación

La cabaña vende **en Argentina y al exterior**. Tres tipos de comprobante,
según la condición fiscal del CLIENTE:

- **Factura A** → cliente responsable inscripto (IVA discriminado)
- **Factura B** → monotributo, exento o consumidor final (IVA incluido)
- **Factura E** → exportación (electrónica, portal de ARCA con clave fiscal)

- **La letra la asigna ARCA al emitir, igual que el número**: se
  TRANSCRIBE, no se elige. Nunca un valor calculado ni generado.
- La condición del cliente sirve para (a) validar que la letra transcripta
  es coherente y (b) calcular el pedido interno (ADR 0008). Derivación
  esperada: exterior → E, responsable inscripto → A, resto → B.
- Moneda: **ARS** en ventas nacionales; **USD o EUR** en exportación.
  `tipo_cambio` es NULL cuando la moneda es ARS.
- Facturan dos personas de la cabaña bajo **un solo CUIT** → no hay tabla de
  emisores; el CUIT es atributo del tenant.
- `punto_venta` y `numero` se transcriben del comprobante.
- No hay cliente WSFE, ni mock de AFIP, ni `afip_estado`: fuera de alcance.
- Para auditoría interna se guarda `cargado_por` (usuario), que **no** es el
  emisor fiscal.
- Flujo real: emiten en ARCA → se sube el PDF a S3 `documentos/` → el sistema
  registra la metadata.

## Exportación

Seis documentos por envío, cinco emisores distintos:

| Documento | Lo emite |
|---|---|
| Factura E | la cabaña, vía ARCA |
| Permiso de embarque de exportación | despachante / SIM |
| Guía aérea (AWB) | línea aérea |
| Lista de empaque | la cabaña |
| DTe (documento de tránsito electrónico) | SENASA |
| Shipper's certificate | la cabaña, p/ aerolínea |

- Moneda: **USD o EUR** según el caso. Campo `moneda` (ISO 4217); no
  hardcodear USD.
- Tipo de cambio: lo fija ARCA y queda determinado al confeccionar la factura.
  Se transcribe junto con el número y queda inmutable.
- La entidad central es el **embarque**: el valor del sistema es responder
  "¿tengo los 6 documentos de este envío?". Esa es la demo del 30/11.

## Ciclo de cría — dos ejes temporales

Modelarlo con una sola fecha ancla es el error a evitar.

**Eje A — cría** (ancla: fecha de traslarve, D0): D0 traslarve · ~D10
introducción de la celda al núcleo (antes de nacer) · D11 nacimiento.

**Eje B — fecundación** (ancla: introducción de la celda, I0): I0+1
nacimiento · nac+4 a 5 madurez sexual · nac+10 fecundada **o extraviada** ·
I0+16 enjaulado, lista para despacho (≈ D26).

Estados de la celda/reina individual (no de la tanda):

    trasladada → (descartada)
    trasladada → introducida → nacida → madura → (fecundada | extraviada)
               → enjaulada → despachada

Dos estados terminales de **pérdida**, con causas distintas:

- `descartada`: la cúpula nunca se introdujo al núcleo (no operculó, se
  perdió antes). Su motivo es **cerrar la fila**, NO medir aceptación: sin
  reportes encima, igual que `celdas_operculadas`.
- `extraviada`: la reina nació, salió al vuelo nupcial y no volvió. No
  modelarla como `fecundada = false`.

Los KPIs no cambian: el denominador sigue siendo **celdas introducidas**.

**KPIs:**

- Principal: % de fecundación = fecundadas / celdas introducidas al núcleo.
- Secundario: fecundadas / nacidas (separa falla de nacimiento de falla de
  fecundación).
- % de extravío = extraviadas / introducidas.
- El % de aceptación post traslarve **no** es el indicador relevante;
  `celdas_operculadas` queda opcional y sin reportes encima.

## Materiales y consumo (confirmado 07/10)

- **El stock no se guarda**: se calcula sumando movimientos. Tres tipos con
  signo — `compra` (positiva), `consumo` (negativa), `ajuste` (cualquiera) —
  con un CHECK de coherencia por tipo.
- **El stock negativo se permite y no lleva constraint**. Motivo técnico:
  es una suma calculada y validarla obligaría a lockear la tabla. Motivo
  real: los movimientos se cargan tarde, y bloquear el consumo impediría
  registrar la tanda — que es el dato que importa. Se resuelve con una
  **advertencia en la respuesta del alta**.
- La **unidad vive en `material`**, como texto corto, sin enum cerrado.
- **El material es la cúpula suelta** (que es como se compra). La
  multiplicación por cúpulas por cuadro vive en el **servicio de alta**,
  jamás en la tabla.
- El alta de tanda recibe **cantidad de cuadros de madera** (1, 2 o 3) y el
  sistema multiplica: cada cuadro tiene listones y cada listón 15 cúpulas —
  **`CUPULAS_POR_CUADRO = 135` como constante nombrada en un solo lugar**.
  Nunca un número libre de cúpulas.
- **Ambigüedad abierta (no resolver)**: no está claro si un cuadro son 135
  cúpulas o si las 135 son entre los tres cuadros. Repreguntado a la
  clienta; mientras tanto la constante es el único lugar a tocar.
- El descuento de material es **automático al crear la tanda**, sin paso de
  confirmación: movimiento de consumo directo en el alta (#27).

## Retención de documentos en S3

| Prefijo | Transición | Expiración | Motivo |
|---|---|---|---|
| `documentos/` | GLACIER_IR a los 90 días | 1825 días (5 años) | prescripción fiscal, Ley 11.683 art. 56 |
| versiones no actuales | — | 30 días | |
| `ingesta/` | — | 30 días | temporal |
| `evidencia/` (fotos de cría) | — | 365 días | |

Implementado como lifecycle rules por prefijo: un cambio de plazo es una línea.

## Multitenant

- Real a efectos del TP: **no degradar RLS ni postergarlo**.
- Semilla: 3 tenants (la cabaña propia + 2 criaderos socios de demo).
- Todo query pasa por el tenant del JWT (`custom:tenant_id`, **inmutable**).
- Los tests **deben** incluir el caso de fuga entre tenants.

## Aprobación de borradores

- Siempre dueño o admin del tenant. Nunca auto-aprobación de la IA.
- Bitácora obligatoria: quién aprobó, cuándo, y el hash o versión del borrador
  aprobado (cubierto por #42; no duplicar la tabla).

## Idempotencia

- TTL de la idempotency key: **24 horas**.
- El frontend reintenta con la **misma** key; dentro de la ventana se devuelve
  la respuesta original guardada; pasadas las 24 h la key se considera nueva.

## Modelo de datos que se desprende

    tenant:              nombre, cuit
    emisor:              NO EXISTE (un solo CUIT)

    comprobante:         tenant_id, tipo (A|B|E), cliente_id, punto_venta,
                         numero, fecha_emision, moneda, importe,
                         tipo_cambio (NULL si ARS), archivo_s3_key, cargado_por
                         UNIQUE (tenant_id, tipo, punto_venta, numero)

    embarque:            tenant_id, destino_pais, fecha_vuelo, awb, estado
    documento_embarque:  embarque_id, tipo (enum de 6), numero,
                         archivo_s3_key, fecha
                         → completitud = los 6 tipos presentes

    tanda:               fecha_traslarve, madre_id, celdas_trasladadas
    celda:               tanda_id, nucleo_id, fecha_introduccion, estado,
                         fecha_nacimiento, fecha_fecundacion, fecha_enjaulado
                         → los % se calculan a nivel celda y se agregan por
                           tanda y por madre sin duplicar lógica

    parque:              ubicacion (texto simple, sin tabla predio)
    nucleo:              parque_id NOT NULL
    banco:               FUERA DE FASE 1 (almacena reinas ya fecundadas)

    material:            unidad (texto corto, sin enum cerrado)
    movimiento_material: material_id, tipo (compra|consumo|ajuste),
                         cantidad_con_signo, motivo
                         → stock = SUM(cantidades), nunca una columna

### Notas al modelo (decisiones del 04/10, corregidas el 07/10)

- **`celdas_trasladadas` no es columna**: es `COUNT(*)` de celdas de la tanda.
  Una fila de `celda` por cada cúpula traslarvada, estado inicial
  `trasladada` (decenas por tanda). Persistir el conteo es el mismo error
  que `porcentaje_fecundacion`.
- `celda.nucleo_id` es **NULLABLE**: la celda recién traslarvada no tiene
  núcleo; se introduce recién alrededor del día 10.
- **Parque y banco son DOS entidades distintas — no confundirlas** (corregido
  el 07/10 tras confirmación de la clienta; el error se quiso repetir):
  - **Parque de fecundación**: donde se lleva la celda, se mete en un
    **núcleo** y se espera la fecundación. `nucleo.parque_id NOT NULL` — el
    núcleo cuelga del parque. El parque lleva su **ubicación como texto
    simple** (pueden estar en distintos predios; sin tabla `predio`).
  - **Banco**: donde se almacenan reinas **ya fecundadas y cosechadas** — un
    momento posterior del ciclo, NO un contenedor de núcleos. **Fuera del
    alcance de fase 1.**
- La **alícuota de IVA vive en el catálogo de ítems vendibles** (enum de
  `importes.py`), no en el cliente. Valor para reinas: **default 21%,
  NO VERIFICADO** (la clienta respondió "creo"; confirmar con una factura
  vieja) y sigue configurable por ítem.
- **`punto_venta` y `numero` son DOS enteros separados**, nunca el string
  impreso concatenado: `0001-00000045` es la REPRESENTACIÓN IMPRESA. El
  padding (4-5 y 8 dígitos) se arma al mostrar. Guardar el string
  concatenado vuelve el UNIQUE redundante y la transcripción inconsistente
  según quién cargue.
- **El UNIQUE espeja la clave natural de ARCA** — (CUIT emisor, tipo, punto
  de venta, número): la terna con la que se pide el CAE. El CUIT es
  atributo del tenant. Un sistema que transcribe no puede ser más estricto
  que el sistema que emite; el UNIQUE con `tipo` es seguro tanto con un
  punto de venta para todas las letras como con puntos separados por tipo.
