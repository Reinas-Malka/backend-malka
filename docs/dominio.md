# Dominio de Malka Suite — reglas confirmadas por la clienta

> Fuente de verdad del negocio. Confirmado por la clienta el 2, 4, 7, 11,
> 16 y 23/10/2026.
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
  `tipo_cambio` es NULL cuando la moneda es ARS. Confirmado el 11/10: **no
  hay ventas nacionales en moneda extranjera** y no se usan otras monedas.
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

### Modalidades de envío (confirmado 11/10)

- **Directo** (Latinoamérica): EZE → aeropuerto del país de destino; lo
  recibe el cliente. `embarque.destino_pais` = país del cliente.
- **Consolidado** (Europa, operatoria propia de la cabaña): EZE → aeropuerto
  hub en Europa; lo retira alguien de la empresa y lo redistribuye en
  sub-envíos a aeropuertos donde retiran los apicultores, o se retira en el
  local. `embarque.destino_pais` = país del hub.
- En Europa **el director vende directamente a cada apicultor** (pasa 3 meses
  allá): cada apicultor es un cliente de exportación y **cada pedido es una
  exportación**, con `destino_pais` = país del apicultor, no el del hub.
- **Los sub-envíos y puntos de retiro NO se registran en el sistema**: la
  redistribución ocurre por fuera. Si algún día se modelan, tiene que ser
  una capacidad **genérica y opcional** que cualquier tenant pueda ignorar;
  **nunca lógica condicionada a un tenant** (los servicios no filtran ni
  ramifican por tenant).
- **Incoterm: opcional**, lista cerrada de los **11 de Incoterms 2020**
  (EXW, FCA, CPT, CIP, DAP, DPU, DDP, FAS, FOB, CFR, CIF). Tiene sentido en
  los envíos directos; en los consolidados queda vacío. **No es requisito
  para que un pedido sea de exportación** → contradice el ADR 0006, que pide
  incoterm y destino: reportado para enmendar el ADR.

## Pedidos (confirmado 11/10)

- La cabaña **vende solo reinas**. Hay **3 razas**, pero son **datos** de la
  tabla `raza` de cada tenant, no un enum ni un CHECK: otro criadero puede
  tener otras.
- Cada línea del pedido lleva **ítem vendible y raza, ambos obligatorios**.
  El catálogo no conoce la raza: la combinación ítem × raza se define en la
  línea. No hay lista de precios: el precio se carga en la línea.
- La línea **copia `alicuota` y `precio_unitario`** al crearse: un cambio
  posterior del catálogo no recalcula pedidos existentes.
- Moneda del pedido según el tipo de cliente: nacional → ARS; exportación →
  USD o EUR. El pedido **no guarda tipo de cambio** (lo fija ARCA al
  facturar, después del pedido).
- Exportación: cliente de tipo exportación + **`destino_pais` obligatorio**
  (ISO 3166-1 alfa-2, distinto de `AR`). Incoterm opcional (ver arriba).
- Estados: `borrador → reservado → entregado | cancelado`. El alta crea
  siempre en `borrador`; las transiciones las valida el servicio (#36).

### Disponibilidad, reservas y despacho (confirmado 15 y 16/10)

- Un pedido **no sale de un banco fijo**: al despachar, los apicultores eligen
  de uno o varios bancos según lo que pida el pedido.
- La **disponibilidad para vender es por raza** y se calcula desde las celdas,
  nunca se guarda: `disponible = celdas enjauladas de la raza − reservas
  activas de la raza` (ADR 0014).
- La **reserva va por raza, sin banco**: se decide antes de saber de qué
  bancos van a salir las reinas.
- **Despachar** = elegir celdas enjauladas de uno o varios bancos y pasarlas a
  `despachada`; la reserva de esas líneas deja de estar activa. **Cancelar** el
  pedido libera sus reservas.
- La **existencia por banco** (cuántas reinas hay en cada banco) es solo
  ubicación física. **No existe un "disponible por banco"**: la reserva no
  tiene banco.

## Ciclo de cría — dos ejes temporales

Modelarlo con una sola fecha ancla es el error a evitar.

**Eje A — cría** (ancla: fecha de traslarve, D0): D0 traslarve (a la
**iniciadora**) · D1 a la **continuadora** · ~D10 introducción de la celda al
núcleo del **parque** (antes de nacer) · D11 nacimiento.

**Eje B — fecundación** (ancla: introducción de la celda, I0): I0+1
nacimiento · nac+4 a 5 madurez sexual · nac+10 fecundada **o extraviada** ·
I0+16 enjaulado, lista para despacho o paso a un **banco** (≈ D26).

Estados de la celda/reina individual (no de la tanda):

    trasladada → (descartada)
    trasladada → introducida → nacida → madura → (fecundada | extraviada)
               → enjaulada → despachada

### Lo que se registra en el campo (confirmado 23/10)

El productor no carga cada paso biológico: **carga tres operaciones por
tanda**, todas por lote (ver "Celda, granularidad y experiencia de usuario"):

1. **Introducir:** elige el parque, la cantidad y la fecha; **no elige
   núcleos**. El sistema reparte las celdas en forma pareja entre los núcleos
   activos del parque, empezando por los que tienen menos celdas ocupadas
   (las que todavía no se cosecharon) y en orden de fila y posición, y
   devuelve cómo quedó el reparto. Sin núcleos activos no hay introducción.
   - Un **núcleo es un baby nuc**. Sus dos compartimentos no se registran.
   - **No hay tope de celdas por núcleo**: el reparto es parejo y sin límite.
2. **Cosechar:** la cosecha de una tanda se hace **en un día** y se cuenta
   **por tanda** (no por baby nuc). Se carga cuántas reinas se cosecharon y la
   fecha. Esas celdas pasan a `enjaulada`, que es lo que suma stock para
   vender (ADR 0014). **Las celdas de la tanda que estaban en el parque y no se
   cosecharon pasan solas a `descartada`**, con la fecha de la cosecha: nadie
   cuenta las pérdidas, salen de la resta (120 introducidas y 95 cosechadas =
   95 enjauladas + 25 descartadas). Una tanda se cosecha una sola vez.
3. **Pasar a banco** las enjauladas, con el control de capacidad del banco.

Antes de introducir también se puede **descartar** lo que no se va a usar.

- **Nacimiento, madurez y fecundación no se cargan**: con la cosecha alcanza.
  Los estados `nacida`, `madura`, `fecundada` y `extraviada` siguen en el
  modelo, pero en fase 1 no hay operaciones que los usen.
- **No hace falta saber de qué núcleo salió cada reina**: la cosecha elige qué
  celdas de la tanda pasan a enjaulada. El núcleo de la celda sirve para
  saber la ocupación del parque, no para trazar cada reina.

`descartada` es el estado terminal de **pérdida**, con dos orígenes que se
distinguen sin un estado nuevo:

- **Sin núcleo:** la cúpula nunca se introdujo (no operculó, se perdió
  antes). Su motivo es **cerrar la fila**, NO medir aceptación: sin reportes
  encima, igual que `celdas_operculadas`.
- **Con núcleo:** se introdujo y no llegó a reina (el resto de la cosecha).

Las dos guardan `fecha_descarte`. `extraviada` (la reina nació, salió al vuelo
nupcial y no volvió) queda en el modelo, pero en fase 1 esas pérdidas se
registran como descarte con núcleo.

**KPIs** (redefinidos el 23/10). El denominador sigue siendo **celdas
introducidas**:

- **% de cosecha** = cosechadas (enjauladas, más las que ya se despacharon) /
  celdas introducidas al núcleo. Es el % de fecundación del dominio: una reina
  cosechada es una reina fecundada.
- **% de pérdida en el parque** = descartadas con núcleo / introducidas.
- El indicador secundario (fecundadas / nacidas) y el % de extravío **salen de
  fase 1**: nacimiento y extravío no se cargan.
- El % de aceptación post traslarve **no** es el indicador relevante;
  `celdas_operculadas` queda opcional y sin reportes encima.
- Los % se calculan a nivel celda y se agregan **por tanda y por raza**. No hay
  KPIs por madre: las madres quedan fuera de alcance (16/10).

### Fechas de las tareas humanas

- **El traslarve no se hace los domingos**: el alta de una tanda con
  `fecha_traslarve` en domingo se rechaza, salvo un registro **retroactivo**
  (la carga tardía de una tanda ya hecha). Viene del brief original y lo usa el
  alta de tanda (#27). **Pendiente de confirmar con la clienta.**
- Las fechas biológicas son días calendario: el domingo no aplica.

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

    madre:               identificacion, activo — OPCIONAL (16/10): fuera de
                         alcance, sin trazabilidad por colmena madre
                         UNIQUE (tenant_id, identificacion)
    tanda:               fecha_traslarve, raza_id NOT NULL, madre_id (NULL),
                         retroactivo,
                         fila_continuadora, posicion_continuadora (NULL;
                         las dos o ninguna)
                         → celdas_trasladadas NO es columna: COUNT(*) de celda
    celda:               tanda_id NOT NULL, nucleo_id (NULL hasta
                         introducirla), banco_id (NULL hasta pasar a banco),
                         estado, fecha_introduccion, fecha_nacimiento,
                         fecha_fecundacion, fecha_enjaulado,
                         fecha_descarte (solo en descartada),
                         fecha_extravio (solo en extraviada)
                         → los % se calculan a nivel celda y se agregan por
                           tanda y por raza sin duplicar lógica

    parque:              nombre, ubicacion (texto simple, sin tabla predio),
                         capacidad_max (en núcleos), activo
    nucleo:              parque_id NOT NULL, fila, posicion, activo
                         UNIQUE (tenant_id, parque_id, fila, posicion)
    banco:               identificador, capacidad_max (en reinas,
                         DEFAULT 276), activo
                         UNIQUE (tenant_id, identificador)
                         → existencia = COUNT(*) de celdas enjauladas con
                           ese banco_id, nunca una columna

    material:            nombre, unidad (texto corto, sin enum cerrado), activo
    movimiento_material: material_id, tipo (compra|consumo|ajuste),
                         cantidad (con signo), tanda_id (solo en consumo),
                         motivo, fecha
                         → stock = SUM(cantidad), nunca una columna
    item_vendible:       nombre, alicuota (enum de importes.py; default 21%),
                         activo

    pedido:              cliente_id, estado (borrador|reservado|entregado|
                         cancelado), moneda (ARS|USD|EUR), destino_pais
                         (NULL en nacional), incoterm (opcional, 11 de 2020),
                         fecha_entrega_solicitada
                         → exportación ⇔ cliente de tipo exportación
    pedido_item:         pedido_id, numero_linea (orden en el pedido),
                         item_vendible_id NOT NULL, raza_id NOT NULL,
                         cantidad, precio_unitario,
                         alicuota (copiada del ítem al crear)
                         → neto, IVA y total se calculan (ADR 0008), no se
                           guardan

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
  - **Banco**: donde se almacenan reinas **ya fecundadas y enjauladas** — un
    momento posterior del ciclo, NO un contenedor de núcleos. **Reincorporado
    a la fase 1 el 16/10** para controlar el inventario listo para despacho,
    con una `capacidad_max` de 276 reinas por defecto.
- **Madres fuera de alcance (16/10)**: no hay calendario previo al traslarve
  ni trazabilidad por colmena madre. La tanda se identifica por su
  `raza_id`. La tabla `madre` queda y `tanda.madre_id` es opcional.
- **Trazabilidad espacial transitoria en la tanda (16/10)**: la ubicación de
  los primeros 10 días va en la propia tanda (`fila_continuadora`,
  `posicion_continuadora`), sin tablas para iniciadoras ni continuadoras.
- **Ubicación en la celda (16/10, ajustado el 23/10)**: desde el día 10 la
  celda guarda en qué `nucleo_id` del parque se introdujo, y al enjaularse en
  qué `banco_id` se guardó. El núcleo sirve para la **ocupación del parque**,
  no para trazar de qué baby nuc salió cada reina: la cosecha es por tanda y
  esa trazabilidad no hace falta (23/10). El banco sí es la ubicación real de
  cada reina enjaulada.
- La **alícuota de IVA vive en el catálogo de ítems vendibles** (enum de
  `importes.py`), no en el cliente. Valor para reinas: **21%, confirmado
  el 11/10** (el 04/10 había respondido "creo"); sigue configurable por ítem.
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

## Celda, granularidad y experiencia de usuario (confirmado 16/10)

- **Una fila = una celda, sin excepción.** Es lo único que permite el quiebre
  espacial desde el día 10 (las celdas de una tanda se separan en núcleos y
  bancos distintos) y la trazabilidad del producto final (reinas de una misma
  tanda con destinos distintos).
- **Rendimiento:** decenas de miles de filas por temporada son triviales para
  PostgreSQL.
- **Operaciones por lote (obligatorias):** la granularidad queda en el backend.
  El usuario **nunca** opera celda por celda: trabaja con grupos y cantidades
  (por ejemplo, "descartar 20 celdas de la tanda X") y el sistema actualiza
  los estados y las fechas de cada celda. Los endpoints y las pantallas de
  transiciones tienen que ser por lote.
