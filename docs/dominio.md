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

- Único formato: **Factura E** (exportación), electrónica, generada en el
  portal de ARCA con clave fiscal. **El número lo asigna ARCA** al generar el
  comprobante.
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
nacimiento · nac+4 a 5 madurez sexual · nac+10 fecundada **o extravíada** ·
I0+16 enjaulado, lista para despacho (≈ D26).

Estados de la celda/reina individual (no de la tanda):

    trasladada → introducida → nacida → madura → (fecundada | extraviada)
               → enjaulada → despachada

"Extraviada" es una **pérdida con causa propia** (no volvió del vuelo
nupcial). No modelarla como `fecundada = false`.

**KPIs:**

- Principal: % de fecundación = fecundadas / celdas introducidas al núcleo.
- Secundario: fecundadas / nacidas (separa falla de nacimiento de falla de
  fecundación).
- % de extravío = extraviadas / introducidas.
- El % de aceptación post traslarve **no** es el indicador relevante;
  `celdas_operculadas` queda opcional y sin reportes encima.

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

    comprobante:         tenant_id, punto_venta, numero, fecha_emision,
                         moneda, importe, tipo_cambio, archivo_s3_key, cargado_por
                         UNIQUE (tenant_id, punto_venta, numero)

    embarque:            tenant_id, destino_pais, fecha_vuelo, awb, estado
    documento_embarque:  embarque_id, tipo (enum de 6), numero,
                         archivo_s3_key, fecha
                         → completitud = los 6 tipos presentes

    tanda:               fecha_traslarve, madre_id, celdas_trasladadas
    celda:               tanda_id, nucleo_id, fecha_introduccion, estado,
                         fecha_nacimiento, fecha_fecundacion, fecha_enjaulado
                         → los % se calculan a nivel celda y se agregan por
                           tanda y por madre sin duplicar lógica

### Notas al modelo (decisiones del 05/10)

- **`celdas_trasladadas` no es columna**: es `COUNT(*)` de celdas de la tanda.
  Una fila de `celda` por cada cúpula traslarvada, estado inicial
  `trasladada` (decenas por tanda). Persistir el conteo es el mismo error
  que `porcentaje_fecundacion`.
- `celda.nucleo_id` es **NULLABLE**: la celda recién traslarvada no tiene
  núcleo; se introduce recién alrededor del día 10.
- **Jerarquía banco → núcleo** (viene del brief original, no de las
  confirmaciones): `nucleo.banco_id NOT NULL`; `banco.capacidad_max` se
  mide en núcleos por banco.
- La **alícuota de IVA vive en el catálogo de ítems vendibles** (enum de
  `importes.py`), no en el cliente. Valor para reinas: pendiente de la
  clienta (21% o 10,5%); configurable, sin valor asumido.
