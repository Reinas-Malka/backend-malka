# Decisiones asistidas por IA

Registro de los puntos del proyecto donde usamos IA, qué generó y qué corregimos.

## Alcance y criterio de este registro

- **Período:** desde el 19/09/2026 (última entrada anterior) hasta el 02/11/2026,
  en los dos repositorios (`backend-malka` y `frontend-malka`). Los números sin
  prefijo son PRs del backend; los que llevan **F#** son del frontend.
- **Fuentes:** solo lo que se puede verificar en los PRs, sus reviews y
  comentarios, los commits, `docs/adr/` y `docs/dominio.md`. No se reconstruye
  nada de memoria.
- **Atribución:** los PRs de FelipeAndreau se hicieron con un asistente de IA de
  código. Para el resto del equipo no hay evidencia escrita de uso de IA en sus
  PRs: no se les atribuye y quedan listados al final para que cada autor lo
  complete.
- El login del frontend (F#32) tiene además su propia entrada en el
  `AI-DECISIONS.md` del frontend, con las alternativas descartadas.

### Herramientas

| Herramienta | Rol | Dónde |
|---|---|---|
| **Claude Code** (asistente de IA de código) | Escribe código, tests, Terraform y documentación; corre los gates locales; hace revisiones con subagentes (`react-reviewer`, `security-reviewer`) y skills (auditoría de fugas, `diseno-frontend`) | PRs de FelipeAndreau; las revisiones con subagentes figuran en #88, #112, F#27, F#30, F#32 y F#36 |
| **Bedrock, Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | Parte del producto: redacta el borrador del documento comercial de un pedido | Sección "La IA dentro del producto" |

### Qué frena lo que genera la IA

- **CI obligatorio** en los dos repos y **aprobación de un code owner** para
  `infra/` (ADR 0013). Los PRs de infraestructura de este período los aprobaron
  personas del equipo (por ejemplo #61, #121, #136, #142 y #143, @pilarwagnerr;
  #76 y #85, @Piaporzio).
- **`terraform apply` solo desde `main`** (ADR 0007): la IA propone el plan; el
  apply lo corre una persona después del merge.
- **`docs/dominio.md` gana** ante cualquier contradicción con código o ADRs, y la
  contradicción se reporta como issue (AGENTS.md).
- La IA **no escribe contraseñas**: todo lo que necesita iniciar sesión con un
  usuario real lo prueba una persona (F#32).

---

## 1. Definición del stack y la arquitectura cloud

**Problema abordado**
Elegir lenguaje de backend y servicios de AWS para una solución SaaS multi-tenant
de gestión de cría de reinas, con presupuesto acotado y plazo de un cuatrimestre.

**Prompt y herramienta**
Asistente de IA conversacional. Se describió el dominio, el equipo y la restricción
de costos, y se pidió una recomendación de stack con alternativas justificadas.

**Código o arquitectura generada**
Propuesta serverless: API en FastAPI sobre AWS Lambda detrás de API Gateway,
PostgreSQL en RDS con Row Level Security por tenant, S3 para documentos,
SQS para el trabajo asíncrono y Bedrock para la redacción de borradores.

**Validación y corrección humana**
Se descartó Go, que era la idea inicial del equipo, por curva de aprendizaje frente
al plazo disponible. Se verificó el costo mensual estimado (17 a 20 USD) contra la
calculadora de AWS y se reemplazó NAT Gateway por VPC endpoints, que baja unos
64 USD por mes. Pendiente de validar en la implementación.

---

## 2. Infraestructura base: RDS, S3, SQS y costos — #13, #15, #16, #49, #50, #61, #71, #76, #80, #84 (21/09 a 07/10)

**Problema abordado**
Levantar la red privada, la base, el bucket de documentos y el circuito
asincrónico con colas, sin NAT y dentro del presupuesto.

**Prompt y herramienta**
Claude Code, con cada issue de infraestructura como consigna (#5, #37, #38, #39)
y el ADR 0002 (red privada sin NAT) como restricción.

**Código o arquitectura generada**
Terraform de la VPC (#13), CORS en API Gateway (#15), RDS PostgreSQL 16 con
`rds.force_ssl` y contraseña en Secrets Manager (#16), bucket privado con TLS
obligatorio y URLs prefirmadas (#49), colas con DLQ, worker y alarmas (#50),
presupuesto mensual (#61) y retención por prefijo (#71).

**Validación y corrección humana**
- **Plan gratuito de AWS (#16):** con `backup_retention_period = 7` el apply
  falló con `FreeTierRestrictionError`; quedó en 1 día y sin autoescalado de
  almacenamiento.
- **Concurrencia reservada (#50):** `reserved_concurrent_executions` es inválido
  en una cuenta con cuota total de 10 (`PutFunctionConcurrency` 400); se sacó en
  el commit `f99b849`. El DoD se verificó en vivo: un mensaje `veneno` falló tres
  veces, llegó a la DLQ y la alarma pasó a ALARM.
- **Costo por AZ (#61):** los endpoints de interfaz cobran por zona; se dejaron
  en una sola AZ y se apagó el de Bedrock hasta que hubiera llamadas reales
  (~36,50 → ~14,60 USD/mes). Antes del apply, una auditoría con subagente no
  encontró hallazgos bloqueantes (comentario del #61). Lo aprobó @pilarwagnerr.
- **Plan que proponía destruir (#76):** con `count = var.email_alertas != "" ? 1 : 0`,
  un `terraform plan` sin la variable proponía **destruir el presupuesto** en vez
  de fallar. Se volvió obligatoria la variable y se sacó el `count`.
- **Decisión revertida por el equipo (#84):** el 04/10 se había decidido no
  versionar nunca la dirección de alertas; el 07/10 el equipo la revirtió porque
  la variable obligatoria bloqueaba los planes de lectura de todos. El PR deja la
  justificación escrita y cómo volver atrás.
- **Tope del presupuesto (#80):** con 15 USD y un costo fijo de ~14,60 la alerta
  saltaba con cualquier pico; pasó a 25 USD.
- **Retención (#49 → #71):** la IA dejó un TODO explícito en lugar de inventar
  plazos; se completó cuando la clienta los confirmó (#70).

---

## 3. Deploy por OIDC, ADRs y guardias de CI — #57, #67, #75, #77, #78, #89, #92, #107 (01/10 a 15/10)

**Problema abordado**
Deployar sin claves de largo plazo y fijar reglas para que ni las personas ni la
IA rompan la cadena de migraciones o la infraestructura.

**Prompt y herramienta**
Claude Code, sobre los issues #10 y #21 y los incidentes del 01/10.

**Código o arquitectura generada**
Trust policy OIDC (#57), `AGENTS.md` con las reglas duras y ADR 0007 (#67),
guardias de CI de Alembic y JWKS (#77), ADRs 0010, 0012 y 0013 (#78, #89, #92).

**Validación y corrección humana**
- **OIDC (#57):** el deploy fallaba con `Not authorized to perform
  sts:AssumeRoleWithWebIdentity` aunque la trust policy parecía correcta. Se
  capturaron los claims reales con un workflow de debug en una rama efímera:
  GitHub había cambiado el formato del `sub` para incluir los IDs estables de
  organización y repositorio. Se ajustó la condición sin aflojarla
  (`StringEquals`, solo este repo y solo `main`).
- **Guardia de JWKS con falso positivo (#77):** la primera versión buscaba
  `cognito-idp` en todo `app/` y rebotaba el `iss` legítimo de Cognito. La v2
  restringe la búsqueda de clientes HTTP a `app/auth.py`; los dos casos se
  probaron a mano.
- **ADR 0010 (#78):** la decisión de versionar el JWKS en la imagen la tomó el
  equipo, corrigiendo un comentario previo del issue #21 que proponía lo contrario.
- **ADR 0007 (#67):** nace del incidente de Cognito del 01/10: un `terraform plan`
  desde `main` propuso destruir el user pool, que se había aplicado desde una rama
  local. Se detectó antes de aplicar. En el mismo PR se marcó que el ADR 0006 contradecía el dominio
  (el sistema no numera comprobantes): se resolvió con el ADR 0008, que enmienda
  y no reemplaza al 0006 (comentario del 04/10).
- **ADR 0012 (#89):** las dos claves IAM de largo plazo quedan como excepción con
  caducidad el 30/11. La decisión es del dueño de la cuenta, no de la IA.
- **Número de ADR desactualizado (#75 → #107):** `AGENTS.md` decía que el último
  ADR era el 0010 cuando en `main` ya estaba el 0013. En vez de cambiar un número
  por otro, se puso el comando que lo muestra.

---

## 4. Diagramas y drift de la documentación — #59, #60 (01/10)

**Problema abordado**
La auditoría pidió un diagrama de arquitectura con CI/CD, IAM/OIDC y el flujo
completo, y encontró desajustes entre los documentos y la infra real.

**Prompt y herramienta**
Claude Code, con el resultado de la auditoría como consigna.

**Código o arquitectura generada**
Diagrama oficial, pipeline regenerable con la librería `diagrams` y correcciones
en ADRs y README.

**Validación y corrección humana**
Cada etiqueta del diagrama se cruzó contra `infra/*.tf` (throttle 20/50, SQS 14
días y 360 s, DLQ a los 3 fallos). El drift corregido: CIDR real `10.20.0.0/16`
(los ADRs decían `10.0.x`), costo del NAT, link roto del tablero y un data source
muerto en `oidc.tf`. `terraform plan` dio **No changes**.

---

## 5. API Gateway: rutas de `/api/v1` y preflight CORS — #88, #90, #91 (09/10)

**Problema abordado**
El primer endpoint de negocio (`/api/v1/clientes`, #87) respondía 404 desde
internet aunque los tests pasaban.

**Prompt y herramienta**
Claude Code, a partir de la revisión completa del proyecto (skills y tres
subagentes) y del diagnóstico de @santigonzalezdangelo del 08/10.

**Código o arquitectura generada**
Una ruta catch-all (#88), una ruta OPTIONS explícita (#90) y, al final, rutas por
método (#91).

**Validación y corrección humana**
- **El diagnóstico fue humano:** la ruta faltante la propuso Santi el 08/10, y el
  #88 lo dice en su primera línea. El 404 lo devolvía el gateway antes de llegar
  a la Lambda, algo invisible para `TestClient`.
- **El primer arreglo de la IA estaba mal (#90):** tras el apply, `OPTIONS
  /api/v1/clientes` daba 405 y se agregó una ruta OPTIONS explícita. Tampoco
  servía: `ANY` incluye OPTIONS y una ruta OPTIONS también se manda a la Lambda,
  donde FastAPI responde 405.
- **Arreglo correcto (#91):** que OPTIONS no coincida con ninguna ruta, para que
  el `cors_configuration` del gateway responda el preflight sin invocar nada.
  Rutas GET, POST, PUT, PATCH y DELETE por `for_each`. Se mantuvo la decisión del
  #15: CORS solo en el gateway, sin `CORSMiddleware`, para no duplicar cabeceras.

---

## 6. Semilla de criaderos demo — #93, #96 (09 y 10/10)

**Problema abordado**
El primer POST de negocio en AWS falló por la FK `cliente_tenant_id_fkey`: los
usuarios de Cognito traían `custom:tenant_id`, pero la tabla `tenant` estaba vacía.

**Prompt y herramienta**
Claude Code, con la traza de CloudWatch (con `request_id`) como insumo.

**Código o arquitectura generada**
Migración `0006` idempotente (`ON CONFLICT DO NOTHING`) con downgrade.

**Validación y corrección humana**
- @Piaporzio observó que la semilla usaba el mismo UUID que el `TENANT` de los
  tests; cualquier test futuro que lo creara iba a fallar por clave duplicada. Se
  cambió en #96, con el crédito en el título.
- La auditoría del 15/10 encontró que la semilla corre en **cualquier** entorno,
  producción incluida: issue #104, **abierto**.

---

## 7. Revisión del 15/10 y auditoría de fugas; correcciones al PR #101

**Problema abordado**
Revisar el estado del proyecto a mitad del cuatrimestre: fugas entre criaderos,
coherencia entre el dominio, los ADRs y el código, y calidad del frontend.

**Prompt y herramienta**
Claude Code: auditoría de fugas con Postgres real, revisión de coherencia entre
documentos, subagente `react-reviewer` sobre F#30 y subagente `security-reviewer`
sobre F#32.

**Código o arquitectura generada**
Issues con los hallazgos y las correcciones, aplicadas en PRs propios o en
commits encima del PR revisado.

**Validación y corrección humana**

*Hallazgos que pasaron a issues (abiertos el 15/10).* Backend: #102 (faltaba el
endpoint de URL prefirmada), #103 (permisos inline del grupo `malka-devs` sin
ADR: escritura del tfstate y `cognito-idp:*` sobre `*`; **abierto**), #104
(semilla en cualquier entorno; **abierto**), #105 (el ADR 0006 exigía incoterm y
el dominio lo define opcional), #106 (este registro) y el PR #107 (número de ADR
desactualizado en `AGENTS.md`). Frontend: #26 (contrato y datos simulados), #28
(componentes `ui/`, tres estados y targets de 40 px; **abierto**) y #29 (unificar
`src/types` y `src/tipos`; **abierto**).

*Correcciones al PR #101 (pedidos, de @santigonzalezdangelo).* Se aplicaron en
dos commits encima de su rama, sin force-push (`8de32ae` y `096153b`), y se
explicaron en un comentario del PR:
1. El PR **borraba de `docs/dominio.md`** la regla "neto, IVA y total se
   calculan, no se guardan", confirmada por la clienta. Se restauró: cambiarla
   requiere un issue y una consulta a la clienta.
2. `cantidad = 3000000000` pasaba Pydantic y hacía fallar el `Integer` de
   Postgres con un **500** en vez de un 422. Se agregó `le=MAX_CANTIDAD`, con un
   test que falló antes del cambio.
3. `" eur"` daba 422 porque el validador `before` corría antes de
   `str_strip_whitespace`. Ahora recorta.
4. Índice `(tenant_id, creado_en DESC, id DESC)` para el listado, agregado a la
   `0008`, que todavía no estaba aplicada en ningún entorno.
5. Tests de los CHECK insertando directo en la base, de la línea repetida y del
   409 por la misma `Idempotency-Key` con otro cuerpo.

Resultado: 704 tests y 97% de cobertura (pedidos al 100%). La auditoría de fugas
confirmó RLS ENABLE + FORCE + política y GRANT mínimo en `pedido` y `pedido_item`.

*Frontend.*
- **F#27:** `cliente.ts` devolvía datos simulados **en las dos ramas**: con el
  flag apagado desaparecía el banner pero seguían los datos de ejemplo. Ahora
  falla cerrado. También se corrigieron `'exterior'` → `'cliente_exterior'`, ids
  de celdas repetidos (`slice(0,4)`) y `'Nueva Zelanda'` → `NZ`. Los 11 tests de
  contrato se escribieron antes del arreglo.
- **F#30 (`react-reviewer`):** sin CRITICAL; cuatro HIGH corregidos en `46f9af2`
  con un test por caso (página que no se recortaba al buscar, error de refresco
  que escondía la tabla, catálogo que no cargaba todas sus páginas, estado de
  baja que no se reseteaba). Quedaron pendientes anotados, como el foco al primer
  campo inválido.
- **F#32 (`security-reviewer`):** el ID token viajaba como `id_token_hint` en la
  URL del logout (M1); al salir no se revocaba el refresh token, válido 30 días
  más (M2); `destinoSeguro` dejaba pasar `/\evil.com` (L1); un corte de red en la
  renovación cerraba la sesión (L2). Corregido en `d5fdc09` y `23d2907`. Lo
  aprobó y lo mergeó @Piaporzio, dueña del tema según el #6.

---

## 8. Dominio del 16/10: bancos, continuadora y alineación de documentos — #110, #112, #114, #116, #126 (16/10 a 23/10)

**Problema abordado**
La clienta cambió el modelo el 16/10 (banco en fase 1, tanda por raza,
disponibilidad calculada desde las celdas) y el 23/10 (operaciones del campo y
KPIs).

**Prompt y herramienta**
Claude Code, con los documentos de la clienta y `docs/dominio.md` como consigna.

**Código o arquitectura generada**
Fusión del dominio y ADR 0014 (#110), migración `0009` y API de bancos (#112),
enmiendas a ADRs y prompt (#114), regla de cliente con pedidos (#116) y dominio
del 23/10 (#126).

**Validación y corrección humana**
- **Fusionar, no reemplazar (#110):** el documento del 16/10 era un parche de 48
  líneas contra 274. Reemplazarlo habría borrado facturación, exportación,
  materiales y retención; se fusionó conservando todo lo que no repetía.
- **CHECK que dejaba pasar NULL (#112):** el CHECK de la continuadora aceptaba
  `(1, NULL)` porque `NULL > 0` da NULL y un CHECK en NULL pasa. Lo encontraron
  los tests; se corrigió con `IS NOT NULL` explícito.
- **Contradicciones de la revisión del 16/10 (#114):** ADR 0006 (incoterm
  obligatorio, ventas nacionales en dólares), ADR 0008 (UNIQUE del comprobante
  sin `tipo`), el prompt `borrador_v1` ("comprobante ya calculado por el
  sistema"), README, y `auth.py`, que interpolaba el `kid` (lo controla el
  cliente) en el mensaje de log; ahora va como campo estructurado.
- **Bug confirmado con un test (#116):** un `PATCH` pasaba un cliente nacional con
  pedidos en ARS a exportación y respondía 200. Ahora es 409 `cliente_con_pedidos`.
- **Lo no confirmado queda marcado (#126):** la regla del domingo sale del brief
  y quedó "pendiente de confirmar" con la clienta, sin tomarla como hecho.

---

## 9. Exportación y documentos del embarque — #117, #118, #120, #121, #139, #145 (17/10 a 02/11)

**Problema abordado**
La demo del 30/11 responde "¿tengo los 6 documentos de este envío?"; para eso
hacían falta el embarque, la subida de PDFs y la descarga.

**Prompt y herramienta**
Claude Code, con los issues #109 y #102 y la sección "Exportación" del dominio.

**Código o arquitectura generada**
Migraciones `0010` y `0011`, endpoints de embarques, completitud, subida y
descarga por URL prefirmada, y la variable del bucket en la Lambda.

**Validación y corrección humana**
- **Choque de nombres en el contrato (#118):** dos enums `TipoDocumento` hicieron
  que el OpenAPI generara schemas con el nombre del módulo y que `ClienteRespuesta`
  cambiara de tipo en el front. Se renombró a `TipoDocumentoEmbarque` y se sumó
  `test_openapi.py`, que falla si vuelve a pasar.
- **Infra separada (#120 → #121):** la variable `BUCKET_DOCUMENTOS` fue en un PR
  aparte para pasar por CODEOWNERS (lo aprobó @pilarwagnerr). Mientras tanto el
  endpoint respondía 503 `subida_no_configurada` sin dejar registros a medias.
- **Completitud con claves inventadas (#139):** la revisión del 29/10 (hallazgo
  ALTO) encontró que los 6 documentos se podían "cumplir" con keys que no
  existían, porque solo se validaba el prefijo del criadero. Ahora el archivo
  tiene que estar registrado: 422 `archivo_inexistente`. Quedan para otro PR la FK
  en la base y comprobar con `HeadObject` que el archivo llegó a S3.
- **Pendientes declarados, no inventados (#117):** los nombres de los estados del
  embarque y cómo corregir un documento mal cargado quedaron como preguntas a la
  clienta.

---

## 10. RLS y aislamiento entre criaderos en los PRs propios — #112, #117, #120, #134, #135, #141, #142, #146 y #147 (abierto)

**Problema abordado**
Cada tabla nueva tiene que respetar el aislamiento por criadero; RLS no se
degrada ni se posterga (AGENTS.md, regla 5).

**Prompt y herramienta**
Claude Code, siguiendo la convención del ADR 0009 y de los PRs de @Piaporzio
(#74 y #94). Esos dos PRs no se atribuyen a la IA en este registro.

**Código o arquitectura generada**
En cada tabla nueva (`banco`, `embarque`, `documento_embarque`, `documento`,
`comprobante`, `llamada_ia`, `borrador`): ENABLE + FORCE + política, GRANT
mínimo (sin DELETE, y sin UPDATE en lo transcripto), FKs compuestas con
`tenant_id` y la tabla agregada a `test_aislamiento_tenant.py`.

**Validación y corrección humana**
- **Keys de S3 con el prefijo del criadero (#117, #120):** CHECK que exige
  `documentos/<tenant_id>/` y prohíbe `..`, para que nadie registre el PDF de
  otro criadero; conocer la key ajena no alcanza para bajarla.
- **El worker con el rol de aplicación (#142):** el worker del borrador lee la
  base con el secreto de `malka_app`, como la API, y no con el dueño, para que RLS
  siga aplicando.
- **El nombre del criadero no se forzó (#146):** el prompt v3 necesitaba el
  nombre del criadero, pero `malka_app` no puede leer `tenant` (no tiene RLS ni
  GRANT, y `test_rol_aplicacion.py` lo exige). El PR **no abrió la tabla**: dejó
  la decisión para aparte. El #147 (abierto, en revisión) propone una función
  `SECURITY DEFINER` con `search_path` fijo que devuelve solo el nombre del
  criadero de la sesión, sin el CUIT.

---

## 11. Idempotencia en altas y reintentos — #117, #120, #134, #141; F#30, F#53, F#55, F#56

**Problema abordado**
Un reintento del navegador (doble clic, timeout, red cortada) no puede crear dos
veces el mismo recurso ni llamar dos veces al modelo.

**Prompt y herramienta**
Claude Code, reutilizando el mecanismo de `Idempotency-Key` del #95
(@mmmelissa099, no atribuido a la IA en este registro).

**Código o arquitectura generada**
`Idempotency-Key` en los POST de embarques, documentos, comprobantes y
borradores; en el frontend, una clave fija por apertura de modal que se repite en
los reintentos.

**Validación y corrección humana**
- **El borrador encola una sola vez (#141):** con la misma clave, el mensaje a
  SQS sale solo la primera vez, y un mensaje repetido no vuelve a llamar al modelo.
- **Simulador del front sin idempotencia (F#30):** lo marcó el `react-reviewer`
  como MEDIUM; se corrigió para que la demo se comporte igual que el backend.
- **Doble clic (F#53):** una sola petición, con la clave repetida hasta que sale bien.
- **Revisiones humanas que completaron el patrón:** en la review del #95 se
  destacó que la cabecera tenía que estar en el CORS del gateway para funcionar
  desde el navegador; en la del #123 se marcó que `POST /reservas` no aceptaba
  `Idempotency-Key`, y @santigonzalezdangelo lo sumó en #127 y #140.

---

## 12. Locks, concurrencia y timeouts — #112, #116, #138 (17/10 a 31/10)

**Problema abordado**
Operaciones simultáneas sobre las mismas celdas (reservar, despachar, pasar a
banco) y locks trabados que ocupaban la única conexión de la Lambda.

**Prompt y herramienta**
Claude Code, con los hallazgos MEDIOS de la revisión del 29/10.

**Código o arquitectura generada**
`bloquear` del banco (`FOR UPDATE` y conteo en otra consulta, #112), `FOR UPDATE`
del cliente que frena un alta de pedido simultánea (#116), y en #138 el mismo
lock de raza que la reserva y el despacho más `lock_timeout` y `statement_timeout`.

**Validación y corrección humana**
- **Bug reproducido antes de arreglarlo (#138):** una entrega tomaba 5 celdas;
  *pasar a banco* esperaba el lock y después les ponía el banco igual, porque el
  `UPDATE ... WHERE id IN (subconsulta)` no volvía a mirar el estado.
  `test_concurrencia_despacho.py` falla sin el arreglo. El cambio toca código de
  @pilarwagnerr (#128) sin cambiar la lógica ni los códigos de error.
- **Topes con `SET LOCAL` (#138):** 5 s de lock y 20 s por sentencia, sin que
  queden pegados en la conexión del pool (hay un test). Antes un lock trabado
  esperaba hasta el corte de 29 s del gateway. `55P03` y `40P01` responden 409
  `operacion_en_curso`.
- **Conflicto de merge en `app/errores.py`:** #135 (límite de la IA) y #138
  tocaban el mismo archivo. Antes de mergear, el commit `866d20f` movió
  `SQLSTATE_OCUPADA` junto a los imports para que los dos cambios no se pisaran.
- **Observaciones de las reviews:** en #119 se pidió que la reserva calcule el
  disponible después de tomar el lock por raza; en #122 se marcó que
  `date.today()` usa la hora UTC de la Lambda, y @pilarwagnerr lo resolvió en #125.

---

## 13. Totales del pedido en su moneda — #132 (29/10)

**Problema abordado**
Un pedido en USD o EUR no podía calcular sus totales porque `importes.py` exigía
un tipo de cambio que el pedido no guarda (lo fija ARCA al facturar).

**Prompt y herramienta**
Claude Code, con el issue #113 y la enmienda del 16/10 del ADR 0008.

**Código o arquitectura generada**
`calcular_totales_pedido`, que comparte `_cuadro_de_iva` con el cálculo del
comprobante; neto, IVA y total calculados en cada lectura, nunca guardados.

**Validación y corrección humana**
El cálculo del documento sigue exigiendo tipo de cambio fuera de ARS (hay un
test). Los valores de punta a punta se fijaron en los tests
(`350005.00 / 73501.05 / 423506.05` en ARS). Lo mergeó @mmmelissa099.

---

## 14. Comprobantes transcriptos y QR de ARCA — #134, #145; F#55, F#57, F#58 (31/10 a 02/11)

**Problema abordado**
Registrar las facturas A, B y E que ya emitió ARCA, sin que el sistema numere ni
calcule nada fiscal, y ahorrar la transcripción a mano.

**Prompt y herramienta**
Claude Code, con los issues #108 (backend) y #37 (frontend) y la sección
"Facturación" del dominio.

**Código o arquitectura generada**
Migración `0014_comprobante` inmutable (GRANT SELECT, INSERT), endpoints con
validación de letra; en el frontend, listado, alta, detalle y lectura del QR del
PDF con pdf.js y jsQR.

**Validación y corrección humana**
- **La letra se valida en el servicio (#134):** un CHECK no puede leer la tabla
  `cliente`; el issue se corrigió el 17/10 y la regla quedó en el servicio
  (`letra_incoherente`). `punto_venta` y `numero` son enteros estrictos: `"0001"`
  se rechaza.
- **pdf.js contra la CSP de producción (F#57):** la CSP de CloudFront tiene
  `script-src 'self'`. Se sirvió el worker desde el mismo origen y se apagó
  WebAssembly (`useWasm: false`). La opción `isEvalSupported` **no existe** en
  pdf.js 6 (TypeScript no la acepta); en su lugar, un test verifica que ni
  `pdf.mjs` ni el worker usen `eval(` o `new Function(`. Se probó con un build
  servido con la misma CSP y un PDF con QR de ARCA: sin violaciones en la consola.
- **Abrir descargaba el PDF (F#57 → #145 → F#58):** la URL se firmaba con
  `Content-Disposition: attachment`. Se agregó `en_linea=true` en el backend y el
  botón lo usa.
- **El QR completa, la persona confirma:** todo queda editable, con el aviso
  "Revisalos antes de guardar"; sin QR legible se vuelve a la carga a mano.

---

## 15. Indicadores de producción (KPIs) — F#59 (02/11)

**Problema abordado**
Mostrar % de cosecha y % de pérdida por tanda y por raza sobre el endpoint del
#144 (@pilarwagnerr, no atribuido a la IA en este registro).

**Prompt y herramienta**
Claude Code, con la definición de KPIs del dominio del 23/10.

**Código o arquitectura generada**
Pantalla `/produccion/kpis`, los % en el detalle de tanda, filtros en la URL y un
simulador que cuenta desde las mismas celdas que la simulación de tandas.

**Validación y corrección humana**
- El simulador replica la regla de `repositorio_kpis.py`: por raza se suman
  celdas y después se divide, no se promedian los %; hay un test para eso.
- Un período al revés se avisa en el campo y no se pide (evita el 422).
- La comparación del contrato contra lo que usan las pantallas descartó, con el
  motivo escrito, dos endpoints que no agregaban nada en la UI.
- La review del #144 (backend) se hizo contra el dominio: denominador =
  introducidas, `null` sin introducidas en vez de dividir por cero.

---

## 16. Seguridad de la infraestructura: CloudFront, CSP y roles OIDC — #85, #136; F#21, F#47 (07/10 a 01/11)

**Problema abordado**
Publicar el frontend sin bucket público y, después, cerrar los dos hallazgos
ALTOS de infraestructura de la revisión de seguridad del 29/10.

**Prompt y herramienta**
Claude Code, con el ADR 0011 y la revisión de seguridad del 29/10.

**Código o arquitectura generada**
S3 privado con CloudFront y OAC (#85), cabeceras de seguridad y un rol de deploy
por repo (#136), y los workflows del frontend (F#21, F#47).

**Validación y corrección humana**
- **Un agregado que no estaba en la orden (#85):** para que el front usara "el
  mismo rol", la IA extendió la confianza OIDC al repo del frontend y lo marcó en
  el PR como dependencia no dicha. Lo aprobó @Piaporzio.
- **Ese mismo rol compartido se corrigió (#136):** la revisión del 29/10 mostró
  que un paquete npm comprometido en el build del front podía reemplazar las
  Lambdas o invocar las migraciones, que usan la credencial dueña de la base y se
  saltean RLS. Ahora el front tiene su propio rol, limitado a su bucket y a la
  invalidación de su distribución.
- **CSP sin ciclo en Terraform (#136):** la API va como comodín de
  `execute-api` de la región porque su CORS ya depende del dominio de la
  distribución; nombrarla armaba un ciclo.
- **Lo que se probó y lo que no (comentario del 30/10):** se sirvió el build con
  las mismas cabeceras y se recorrieron Tandas, Pedido, Embarque y un modal sin
  violaciones de CSP. El login real y la subida a S3 no se podían probar en local
  y quedaron para verificar en DevTools después del apply.
- **Orden de aplicación:** el PR del frontend que cambia de rol (F#47) se mergeó
  después del apply, para no deployar con un rol inexistente.
- **Afuera a propósito:** `USER_PASSWORD_AUTH` en dev, autorizador JWT en el
  gateway y la contraseña del dueño en el tfstate quedan para otro PR.

---

## 17. Borrador con IA: prompt, validador fiscal, worker y v3 — #58, #135, #137, #141, #142, #143, #146 y #147 (abierto); F#51, F#53, F#60 (30/09 a 02/11)

**Problema abordado**
Redactar con Bedrock el borrador del documento de un pedido sin que el modelo
genere ningún número fiscal, con costo acotado y sin colgar la API.

**Prompt y herramienta**
Claude Code para todo el código; **Claude Haiku 4.5 en Bedrock** como modelo del
producto. Consignas: issues #40, #45 y #39 del backend, #16 del frontend, y la
regla inviolable del dominio.

**Código o arquitectura generada**
Esqueleto mockeado (#58), límites por criadero (#135), validador por valores y
prompt v2 (#137), flujo asincrónico API → SQS → worker → Bedrock (#141), infra
del worker (#142), ARNs corregidos (#143), prompt v3 y contexto (#146); en el
front, la sección del borrador y el documento completo (F#53, F#60).

**Validación y corrección humana**
- **Falso positivo desde el principio (#58):** la palabra "total" se buscaba con
  límite de palabra para no rechazar "totalidad". Primer tramo sin llamadas reales
  (cero consumo). Lo aprobó @santigonzalezdangelo.
- **Agujeros del filtro fiscal (#137):** la revisión del 29/10 (ALTO) reprodujo
  que *"Importe de USD 12.600 a tipo de cambio 1050. Factura 0003-00001234"*
  **pasaba**, igual que un JSON con campos vacíos, mientras que *"La cantidad
  total de reinas es 20"* y *"La entrega cae en octubre"* se rechazaban. El filtro
  pasó a buscar **valores** (montos con moneda, cotizaciones, números de
  comprobante, porcentajes, CAE) y a rechazar cualquier número que no esté en el
  contexto, con esquema estricto (`extra="forbid"`).
- **Fallos que no dejan nada colgado (#141):** si SQS falla, el borrador queda
  `rechazado` (`cola_no_disponible`) y no en `generando` para siempre. El modelo
  se llama fuera de toda transacción.
- **ARNs de Bedrock mal armados (#143):** el primer borrador real en dev terminó
  en `error_modelo`. Con `simulate-principal-policy` sobre el rol del worker, los
  cuatro ARNs reales daban `implicitDeny`: tenían un `:` de más
  (`us-east-1:::`), al perfil de inferencia le faltaba la cuenta, y el perfil
  `us.` reparte entre us-east-1, us-east-2 y us-west-2, así que el modelo de
  fundación tiene que estar permitido en las tres. Con la política nueva,
  `simulate-custom-policy` dio `allowed`. Esa política venía del #50 y los tests
  con el modelo falso no podían detectarlo: apareció con la primera llamada real.
  Lo aprobó @pilarwagnerr.
- **"Núcleos" en vez de "reinas" (#146):** para un pedido de 30 reinas Buckfast
  el modelo escribió "30 núcleos de abejas". El contexto decía
  `- 30 x Buckfast, raza Buckfast` y nada indicaba qué se vende. El v3 dice que el
  criadero vende solo reinas y arma cada línea como "N reinas <raza>, ítem
  <ítem>". El validador no chequea esa palabra, a propósito: es un tema del prompt.
- **Fecha rechazada por el propio validador (#146):** con la fecha solo en ISO,
  "4 de diciembre" se rechazaba porque "04" no es "4". El contexto ahora la manda
  también en palabras.
- **Sin precios en el contexto (#146):** la consigna permitía incluirlos, pero un
  precio copiado por el modelo sería un número fiscal en el texto. Se sacaron y
  los importes se muestran aparte, calculados por el sistema (F#60).
- **`Retry-After` invisible para el navegador (F#53):** el CORS del gateway no la
  expone, así que el front usa los mismos valores que manda el backend (60 s y la
  medianoche de Argentina) y se dejó la sugerencia para el backend.
- **No verificado todavía:** la salida real de Bedrock con el v3 y la latencia en
  el peor caso (issue #148, con la prueba en dev sin tildar).

---

## 18. Frontend: base, sistema de diseño y contrato OpenAPI — F#22, F#23, F#24, F#25, F#30, F#34, F#51 (09/10 a 01/11)

**Problema abordado**
Una base sobre la que el resto del equipo pudiera armar pantallas sin
desalinearse del backend.

**Prompt y herramienta**
Claude Code, con los issues #4, #5 y #10 del frontend y `docs/dominio.md`.

**Código o arquitectura generada**
Alias `@/`, tokens de diseño y componentes base sobre Radix, tipos del dominio y
datos simulados, contrato generado desde el OpenAPI (`openapi-typescript`),
cliente HTTP, rutas y catálogos genéricos.

**Validación y corrección humana**
- **Contrato generado, no escrito a mano (F#30):** `src/api/esquema.d.ts` sale de
  una foto del OpenAPI; si el backend cambia un campo, el build marca el lugar.
  Se regenera en cada cambio del backend (F#34, F#51). Del lado del backend, el
  #118 agregó un test para que los nombres de schemas no rompan esa generación.
- **Bugs que encontraron los tests (F#30):** `<Button comoHijo>` rompía la
  pantalla porque el `Slot` de Radix recibía dos hijos (lo detectó el test de la
  404); la hora en `es-AR` mostraba `02:47` para las 14:47; había un `<main>`
  dentro de otro.
- **Peso medido (F#23):** el sistema de componentes sumó +152 kB (+50 kB gzip),
  medidos y escritos en el PR.
- **Tipos alineados con la API real (F#25):** a pedido de @santigonzalezdangelo,
  verificados contra la respuesta en vivo.
- **El banner de datos simulados (F#24)** no alcanzaba: lo corrigió F#27 (entrada 7).

---

## 19. Frontend: pantallas — F#19, F#34, F#35, F#36, F#41, F#46, F#49, F#56 (07/10 a 02/11)

**Problema abordado**
Pantallas de producción, exportación y documentos para la demo del 30/11.

**Prompt y herramienta**
Claude Code, con los issues del frontend, la auditoría con la skill
`diseno-frontend` (F#36) y las revisiones de diseño, accesibilidad y React del
29/10 (F#46, F#49).

**Código o arquitectura generada**
Subida de documentos, bancos, embarques con el checklist de 6 documentos,
núcleos, movimientos de materiales y detalle de tanda con operaciones por lote.

**Validación y corrección humana**
- **Contraste medido (F#36):** siete pares de colores no pasaban WCAG AA (por
  ejemplo error sobre panel 2,98 → 5,78). Las tablas pasan a tarjetas en el
  teléfono para que las acciones no queden detrás de un scroll horizontal.
- **Ejemplos que contradecían el dominio (F#41):** la guía de estilo mostraba
  "la madre tiene que existir" (las madres son opcionales) y un bloqueo por falta
  de cúpulas que el dominio no tiene (avisa, no bloquea).
- **Revisión de diseño del 29/10 (F#46):** "2/6 faltan" se leía como "faltan 2";
  pasó a "2 de 6". Inputs de 16 px en el teléfono para que iOS no haga zoom. Los
  archivos de tandas que había reformateado Prettier se dejaron como estaban para
  no generar conflictos con el trabajo de otra persona.
- **Revisión de accesibilidad y React del 29/10 (F#49):** el botón de peligro
  daba 3,3:1 y pasó a 4,7:1; el foco vuelve al botón que abrió el modal; un 409 al
  reservar refresca el pedido en la pantalla de @santigonzalezdangelo (el test
  fallaba sin el arreglo).
- **Trabajo sobre issues de otros:** F#56 toma el #9 de @pilarwagnerr y lo dice
  en el PR; F#46 y F#49 avisan qué pantallas ajenas tocan.

---

## 20. Frontend: paginación con cursor — F#52, F#56 (02/11)

**Problema abordado**
Embarques, movimientos de un material y tandas pedían `limite=100` e ignoraban
`siguiente`: pasadas las 100 filas el listado se cortaba sin avisar.

**Prompt y herramienta**
Claude Code, con el patrón de cursor del backend (#100 de @pilarwagnerr y #131 de
@santigonzalezdangelo, no atribuidos a la IA en este registro).

**Código o arquitectura generada**
`useInfiniteQuery` con "Cargar más", simuladores que paginan como el backend y
`CargarMas` como pie común.

**Validación y corrección humana**
Test con 103 filas (el límite más 3); un cursor roto da 422 también en el
simulador. El gate local falló en tres tests no tocados que fallaban igual sobre
`origin/main` con la máquina cargada; el PR lo declara y se mergeó con el CI en verde.

---

## 21. Frontend: CI y estabilidad de los tests — F#20, F#54 (07/10 a 02/11)

**Problema abordado**
El repo del frontend no tenía ningún check, y más adelante los tests empezaron a
fallar por motivos ajenos al código.

**Prompt y herramienta**
Claude Code.

**Código o arquitectura generada**
Workflow de lint, build y tests (F#20) y ajustes de configuración de Vitest (F#54).

**Validación y corrección humana**
- **Un efecto lateral de la propia herramienta (F#54):** Vitest recorría
  `.claude/**`, donde viven los worktrees de los agentes, y corría sus copias de
  los tests, con fallas falsas. Se excluyó la carpeta.
- **Timeouts:** el test del inicio esperaba 1 s a una pantalla con carga diferida
  y fallaba con la máquina cargada; pasó a 5 s, como `rutas.test.tsx`.
- **Sin falsos verdes (F#20):** mientras no existía el script de tests, el job
  pasaba con un `::notice::` visible en vez de fallar en `main` o pasar en silencio.

---

## 22. README del backend — #47 (28/09)

**Problema abordado**
Completar el README para que alguien que llega al repo entienda de qué va el
proyecto, cómo está armado y cómo se opera (documentación del Checkpoint 1).

**Prompt y herramienta**
Claude Code, con el repo y la arquitectura definida como
contexto. 

**Código o arquitectura generada**
Las secciones "De qué va el proyecto", "Observabilidad", "Estructura del
proyecto", las variables de entorno de la API y de Terraform, las salidas útiles
y "Problemas comunes".

**Validación y corrección humana**
La autora revisó cada sección antes de subirla. Desde entonces, cada PR de
producción de este registro actualizó su parte del README en un commit propio
(`docs: …`), para que no se desactualizara.

---

## 23. Alembic, migración inicial y Lambda de migraciones — #54, #56 (30/09)

**Problema abordado**
Versionar el esquema con Alembic, con una migración inicial que se pueda revertir
(issue #18), y aplicar las migraciones en AWS sin abrir la base a internet
(issue #53).

**Prompt y herramienta**
Claude Code, con las issues y la infraestructura que ya existía
(VPC, RDS privada y Secrets Manager) como contexto.

**Código o arquitectura generada**
- #54: Alembic leyendo la URL desde la configuración de la app, la migración
  `0001` con `tenant`, `raza`, `tanda` y `tanda_evento` con RLS, tests de la
  configuración de conexión y la sección del README.
- #56: `app/migrar.py`, un handler que corre `upgrade` o `downgrade` según el
  evento; `infra/migraciones.tf`, una Lambda con **la misma imagen** que la API
  (otro `command`) dentro de la VPC, con las credenciales del secreto de RDS; y
  cómo correrla en el README.

**Validación y corrección humana**
- **Downgrade probado:** el DoD de la #18 pedía bajar y volver a subir la
  migración, no solo aplicarla. Quedó como regla en el README: cada migración se
  escribe a mano con un `downgrade` que funcione, y antes del PR se prueba
  `alembic downgrade -1` y se vuelve a subir.
- **La misma imagen, con un rol propio:** las migraciones que corren en AWS son
  las del commit desplegado y llegan a RDS, que es privada, desde la VPC, sin
  abrir la base. La Lambda tiene su propio rol porque usa la credencial del dueño
  de las tablas, mientras que la API usa un rol sin privilegios (#19). Lee el
  secreto por el VPC endpoint que ya existía, así que no suma costo fijo.
- **El primer esquema no seguía el dominio:** `0001` modelaba la tanda con
  eventos por etapa. Cuando el dominio del 04/10 y 07/10 definió la celda como
  entidad central, se abrió la #73 para reconciliarlo y se reemplazó en #79
  (entrada 24).

---

## 24. Migraciones 0003 y 0004: la celda como entidad central y los materiales — #79, #86 (07/10 a 08/10)

**Problema abordado**
Llevar el esquema al dominio confirmado por la clienta: una fila de celda por
cúpula en lugar del modelo de etapas de la tanda (issues #26 y #73), y los
materiales con sus movimientos y el catálogo de ítems vendibles.

**Prompt y herramienta**
Claude Code, con `docs/dominio.md` como fuente de verdad.

**Código o arquitectura generada**
- #79: la migración `0003` con `celda`, `madre`, `parque` y `nucleo` (RLS,
  `CHECK`s y permisos mínimos del rol `malka_app`), tests de aislamiento por
  tenant y de las reglas del esquema, y el modelo de datos del dominio
  actualizado.
- #86: la migración `0004` con `material`, `movimiento_material` e
  `item_vendible`, y sus tests.

**Validación y corrección humana**
- **El núcleo colgaba del banco:** la primera versión de `0003` seguía el brief
  original (banco → núcleo). La clienta confirmó el 07/10 que el núcleo está en
  un **parque de fecundación** y que el banco es otra cosa (guarda reinas ya
  cosechadas). Se corrigió en el mismo PR (`3d9d860`) y la tabla pasó a llamarse
  `parque`, como en el dominio (`e1c11e3`). El dominio deja escrito que el error
  "se quiso repetir": la distinción se volvió a revisar en cada PR siguiente.
- **Lo que no es columna:** `celdas_trasladadas` es `COUNT(*)` de celdas y el
  stock es la suma de movimientos; ninguno se guarda, como pide el dominio.
- **Permisos sin `DELETE` (#86):** el material se da de baja lógica y un
  movimiento no se edita ni se borra; un error se corrige con un ajuste, así el
  stock siempre tiene historia.
- **La alícuota no se inventó:** los valores salen del enum de `importes.py` (un
  test verifica que la migración y el enum coincidan) y el 21 % por defecto quedó
  marcado como "NO VERIFICADO" hasta que la clienta lo confirmó el 11/10.

---

## 25. CRUD de producción, paginación y capacidad del parque — #100 (13/10)

**Problema abordado**
Altas, lectura, edición y baja de razas, madres, materiales (con movimientos),
parques, núcleos e ítems vendibles (issue #30), con el control de capacidad del
parque de fecundación (issue #29).

**Prompt y herramienta**
Claude Code, con las dos issues, el dominio y los patrones ya
existentes del backend (errores, idempotencia de #95, RLS).

**Código o arquitectura generada**
Paginación por cursor común (`app/paginacion.py`) y escritura común de
repositorios (`app/services/repositorio.py`), schemas, seis repositorios, los
endpoints con `Idempotency-Key`, la advertencia `parque_al_limite`, tests de punta
a punta (incluido el aislamiento entre criaderos) y la sección del README.

**Validación y corrección humana**
- **Una condición de carrera en la capacidad del parque:** la primera versión
  bloqueaba el parque y contaba sus núcleos en la misma consulta. Con `READ
  COMMITTED`, ese conteo no ve los núcleos que otra alta confirma mientras se
  espera el lock, así que dos altas simultáneas podían pasar la capacidad. Se
  separó en dos pasos (primero `FOR UPDATE`, después contar en otra consulta) y se
  agregó `test_concurrencia_parque.py`, que lo demuestra (`bd08f92`). Ese patrón
  es el que después reusaron #122 (material) y #128 (parque y banco).
- La paginación por cursor de este PR es la que tomó después el frontend
  (entrada 20).

---

## 26. Alta de tanda y el "hoy" de Argentina — #122, #125 (19/10 a 23/10)

**Problema abordado**
Dar de alta una tanda por cuadros de madera (1 a 3), con raza obligatoria, madre
opcional y la ubicación en la continuadora, creando una celda por cúpula y
descontando las cúpulas del material en la misma transacción (issue #27). Después,
que las validaciones de fecha usen el día de Argentina y no el de UTC (#124).

**Prompt y herramienta**
Claude Code.

**Código o arquitectura generada**
- #122: `CUPULAS_POR_CUADRO` en un solo lugar, schemas del alta, repositorio con
  la tanda, las celdas en un solo `INSERT … generate_series` y el movimiento de
  consumo, endpoints `POST` y `GET` de tandas, tests sin base y de punta a punta,
  y la sección del README.
- #125: `app/fechas.py` con `hoy()` en `America/Argentina/Buenos_Aires` (y
  `tzdata`, para que `zoneinfo` funcione en Lambda), las validaciones de tandas y
  movimientos contra ese día y tests a las 22 h de Argentina, cuando UTC ya es el
  día siguiente.

**Validación y corrección humana**
- **La regla del domingo ida y vuelta:** la issue actualizada dejó de mencionar el
  domingo y se sacó; Felipe pidió mantenerla solo para el traslarve, con la
  excepción de la carga retroactiva, y se restauró. Se corrigió además un
  comentario que la aplicaba a las fechas biológicas, que son días calendario.
- **`material_id` en el alta:** se confirmó con Felipe antes de mergear, porque el
  dominio no decía de qué material se descuentan las cúpulas.
- **El review de #122 detectó `date.today()`:** corre en UTC, así que entre las
  21 y las 24 h de Argentina "hoy" era mañana. No bloqueó el merge: se abrió la
  issue #124 y se resolvió en #125, que también agregó el test de concurrencia
  que el review había marcado como opcional (dos altas simultáneas sobre el mismo
  material, que ya estaban serializadas por el `FOR UPDATE`).
- Al rebasar sobre un `main` nuevo chocó `app/main.py`: se tomó el de `main` y
  se agregaron a mano las dos líneas de tandas, en vez de aceptar el de la rama.

---

## 27. Operaciones por lote sobre la tanda — #128 (25/10)

**Problema abordado**
Registrar lo que pasa en el campo sin operar celda por celda: descartar antes de
introducir, introducir en un parque, cosechar y pasar a banco (issue #28, dominio
del 23/10).

**Prompt y herramienta**
Claude Code. Antes de escribir código, la IA armó la lista de
preguntas que la issue dejaba abiertas; la autora se las hizo a la clienta y a
Felipe y volvió con las respuestas, que pasaron al dominio (#126).

**Código o arquitectura generada**
Un solo endpoint `POST /api/v1/tandas/{id}/operaciones` con el campo `accion` como
discriminador, el reparto parejo entre núcleos (`repartir()`, primero los menos
ocupados y en orden de fila y posición), el repositorio de operaciones con
bloqueo de la tanda y después del parque o del banco, tests sin base, de punta a
punta y de concurrencia con el banco lleno.

**Validación y corrección humana**
- **Las respuestas de la clienta cambiaron el diseño:** un núcleo es un baby nuc,
  sin tope de celdas; el núcleo se libera al cosechar; la madurez no lleva fecha;
  la cosecha se carga por tanda y lo no cosechado pasa a `descartada` con núcleo,
  sin un estado nuevo; las fechas no pueden ser anteriores al paso previo.
- **Fuera de alcance por decisión del equipo:** mover reinas de un banco a otro y
  corregir operaciones cargadas por error. Quedaron como issues propias.
- **Mismo patrón de concurrencia que #29:** primero `FOR UPDATE` y después contar
  en otra consulta; el test de dos lotes simultáneos sobre un banco con lugar para
  uno lo demuestra.
- Se verificó que la rama mergeaba sin conflictos con la de @santigonzalezdangelo
  (#127), que tocaba el mismo README. Quedó anotada una diferencia conocida: el
  banco inexistente responde 404 acá, porque se reusa `repositorio_bancos` igual
  que con el parque, y 422 en #127, que sigue la convención de pedidos para un id
  del cuerpo que no existe.
- **Un error de la IA que encontró otra persona:** *pasar a banco* podía ponerle
  banco a celdas que un despacho simultáneo acababa de tomar, porque el `UPDATE`
  no volvía a mirar el estado después del lock. Lo reprodujo y arregló Felipe en
  #138 (entrada 12), sin cambiar la lógica ni los códigos de error.

---

## 28. Detalle de tanda: núcleos ocupados y bancos — #130 (25/10)

**Problema abordado**
La #9 del frontend pedía mostrar cuántas cosechadas están en banco, los núcleos
ocupados por la tanda y los bancos donde están, y `GET /api/v1/tandas/{id}` no lo
devolvía (issue #129).

**Prompt y herramienta**
El faltante lo encontró la IA al preparar la #9 del
front; en vez de resolverlo en el frontend, propuso el cambio en el backend.

**Código o arquitectura generada**
`TandaDetalle` (= `TandaRespuesta` más `nucleos` y `bancos`, contados al leer),
dos consultas en el repositorio de tandas, el `GET` del detalle con ese schema y
9 tests de punta a punta. El listado no cambió.

**Validación y corrección humana**
- **Formato:** el mismo `GET` con un schema propio para el
  detalle, no un endpoint aparte, y "núcleos" son solo los que todavía tienen
  celdas en el parque (después de cosechar la lista queda vacía).

---

## 29. KPIs de producción en el backend — #144 (02/11)

**Problema abordado**
% de cosecha y % de pérdida en el parque por tanda y por raza, calculados desde
las celdas con la definición del dominio del 23/10 (issue #31).

**Prompt y herramienta**
Claude Code, con la issue y la sección de KPIs del dominio.

**Código o arquitectura generada**
`GET /api/v1/kpis/produccion` con `agrupar=tanda|raza` y período por fecha de
traslarve; la cuenta del % separada en `kpis.py` y los conteos escritos una sola
vez (`CONTEOS`) para las dos agrupaciones; 8 tests sin base y 15 de punta a punta,
armados con las operaciones de #128.

**Validación y corrección humana**
Decisiones que la issue no fijaba, explicadas en el PR y revisadas contra el
dominio:
- Sin introducidas, los % vienen en `null` y no en 0: un 0 parecería un dato.
- Por raza se suman las celdas y después se divide; no se promedian los % de cada
  tanda (120/95 y 30/30 da 83,3 %, el promedio daría 89,6 %). Hay un test para eso.
- `en_parque` explica por qué, en una tanda en curso, cosecha + pérdida no llega
  a 100.
- Las cosechadas y las perdidas exigen `fecha_introduccion`, como el denominador,
  así ningún % puede pasar de 100.
- El README todavía decía "% de fecundación y de extravío": se corrigió, porque
  el extravío salió de la fase 1.

---

## 30. Frontend: listado y alta de tandas por cuadros — F#42 (27/10)

**Problema abordado**
La pantalla de tandas (issue #8 del frontend): listado con edad y celdas por
estado y alta por cuadros, usable desde el teléfono.

**Prompt y herramienta**
Claude Code, con el contrato OpenAPI de #122 y los patrones del
frontend (`FormularioRecurso`, `DataTable`, swap simulado/real).

**Código o arquitectura generada**
`Tanda`, `TandaCrear` y `TandaCreada` pasan a ser alias del contrato; cliente de
tandas real y simulado con la misma firma (el simulado repite las reglas del
backend: domingo, raza y material activos, 135 celdas por cuadro, stock negativo e
idempotencia); datos simulados con el modelo del 23/10; la pantalla y 18 tests.

**Validación y corrección humana**
- **Un test existente falló solo en Windows:** `rutas.test.tsx` esperaba 1 s a
  cada pantalla con carga diferida; con Tandas como primera pantalla que carga el
  formulario y el modal, en una máquina lenta no alcanzaba. Se subió a 5 s por
  pantalla y 20 s el test, con el motivo en un comentario.
- **No se probó contra el backend real:** los tipos salen del contrato y el
  cliente HTTP es el mismo de las otras pantallas; el PR lo dice.
- Al preparar la #9 apareció que el backend no devolvía los núcleos ni los bancos
  de la tanda: eso dio lugar a #130 (entrada 28).

---

## La IA dentro del producto: el borrador del documento

### Reglas

- **La IA redacta, nunca genera números fiscales** (`docs/dominio.md`, regla
  general; AGENTS.md, regla 6). Número de comprobante, CAE, IVA, total y tipo de
  cambio se transcriben del comprobante de ARCA o los calcula el sistema; nunca
  los escribe el modelo.
- **Falla cerrado:** lo que no pasa la validación no se guarda. La base lo
  refuerza con un CHECK: un borrador listo tiene contenido y uno rechazado tiene
  motivo (migración `0016`).
- **Una persona revisa:** el front muestra siempre que el texto lo redacta una IA
  y que los números fiscales los carga una persona. La aprobación del borrador
  (backend #42, frontend #17) todavía no existe: lo que se edita en pantalla no se
  guarda (F#53).
- **El modelo no descubre datos:** el worker arma el contexto leyendo la base con
  RLS; el mensaje de la cola lleva solo ids (#141).

### Flujo

`POST /api/v1/documentos/borradores` → reserva una llamada del límite → crea el
borrador en `generando` y responde 202 → encola después del commit → el worker lee
el pedido, llama a Bedrock (`converse`, temperatura 0) fuera de toda transacción,
valida y guarda `borrador` o `rechazado` → el front consulta cada 2 s.

### Validador (`app/services/ia/borrador.py`)

- Esquema estricto (`extra="forbid"`) con largos máximos: encabezado 200, cuerpo
  6000, notas 1000 caracteres.
- Rechaza los cinco campos fiscales como clave (`numero_comprobante`, `cae`,
  `total`, `iva`, `tipo_cambio`) y, en el texto, **valores**: montos con moneda,
  tipo de cambio o cotización, número de comprobante impreso, porcentajes e IVA,
  CAE o 14 dígitos seguidos.
- Rechaza cualquier número que no esté en el contexto (`35.000,50` y `35000.50`
  cuentan como el mismo).
- Un reintento con el error devuelto al modelo; después, `rechazado` con
  `salida_invalida`. Si el modelo falla, `error_modelo`; si la cola falla,
  `cola_no_disponible`.

### Límites por criadero (`app/services/ia/limites.py`, #135)

| Tope | Valor por defecto | Al pasarse |
|---|---|---|
| Llamadas por minuto | 10 (`IA_LLAMADAS_POR_MINUTO`) | 429 `limite_ia_por_minuto`, `Retry-After: 60` |
| Llamadas por día de Argentina | 200 (`IA_LLAMADAS_POR_DIA`) | 429 `limite_ia_diario`, hasta la medianoche |
| Tokens de salida por llamada | 3000 (1500 hasta el v3) | El modelo corta la respuesta |

Un advisory lock por criadero evita que dos pedidos simultáneos usen el último
lugar. Una configuración vacía, en 0 o no numérica usa el valor por defecto: la
IA nunca queda sin límite. Con los topes por defecto, el gasto máximo ronda
**2,10 USD por día por criadero** (era ~1,60 antes del v3).

### Métricas

- **Por borrador** (tabla `borrador`): modelo, versión de prompt, tokens de
  entrada y salida, latencia, reintentos y resultado de la validación.
- **Por criadero** (tabla `llamada_ia` y Embedded Metric Format): namespace
  `MalkaSuite/IA`, dimensión `tenant_id`, métricas `Llamadas`, `TokensEntrada` y
  `TokensSalida`, sin llamar a la API de CloudWatch.
- Los logs del worker no llevan el cuerpo de los mensajes (#69).

### Prompts versionados (`app/services/ia/prompts/`)

Cambiar un prompt es cambiar de versión: los anteriores quedan en el repo y cada
borrador guarda con qué versión se generó.

| Versión | PR | Qué cambió y por qué |
|---|---|---|
| `borrador_v1` | #58 (30/09) | Primera versión: JSON con encabezado, cuerpo y notas; prohíbe los cinco campos fiscales y no inventar datos. El 16/10 (#114) se corrigió una frase en el mismo archivo, antes de cualquier llamada real: decía que el comprobante venía "ya calculado por el sistema", y el dominio dice que los números se transcriben de ARCA. |
| `borrador_v2` | #137 (31/10) | El contexto va entre `<datos>` y es dato, nunca instrucción (se sacan las etiquetas `<datos>` que vengan adentro, para que un nombre de cliente no pueda cerrar el bloque). Prohíbe montos, porcentajes, cotizaciones y números de factura. Acompaña el validador por valores. |
| `borrador_v3` | #146 (02/11) | El v2 producía textos cortos y el error de "núcleos". El v3 dice qué vende el criadero (solo reinas), distingue "Nota de venta" de "Proforma de exportación", fija cuatro secciones (PARTES, OBJETO, CONDICIONES DE ENTREGA, CIERRE), suma notas de manejo de reinas vivas y prohíbe años de normas, plazos y temperaturas, que serían números fuera del contexto. El contexto se arma en `contexto.py` (función pura) y no lleva precios. |

### Pendiente

- Probar la salida real del v3 en dev con el pedido Buckfast (#148).
- Aprobación del borrador (backend #42, frontend #17).

---

## Pendiente de completar por cada autor

Estos PRs se mergearon desde el 19/09 y no tienen evidencia escrita de uso de IA
(ni en la descripción ni en las reviews). Cada autor indica si usó IA y, si la
usó, agrega una entrada con el mismo formato (problema, herramienta, qué generó,
qué corrigió y por qué). Si no la usó, alcanza con anotarlo al lado del PR.

### @Piaporzio

- [ ] #65 — feat(infra): user pool, app client y grupos de Cognito (01/10)
- [ ] #74 — feat(core): rol de aplicación sin privilegios y tenant con SET LOCAL (07/10)
- [ ] #82 — feat(core): validación del ID token de Cognito contra el JWKS versionado (07/10)
- [ ] #94 — test(seguridad): aislamiento multi-tenant con RLS (09/10)
- [ ] F#44 — feat(nav): menú y rutas según el rol del usuario (27/10)


### @santigonzalezdangelo

- [ ] #66 — Feat/importes alicuotas (03/10)
- [ ] #87 — Feat/crud clientes (09/10)
- [ ] #101 — feat(ventas): alta y lectura de pedidos con reglas de exportación (15/10). Las correcciones de la revisión del 15/10 ya están en la entrada 7.
- [ ] #119 — feat(ventas): disponibilidad de reinas por raza calculada desde las celdas (17/10)
- [ ] #123 — feat(ventas): reservas transaccionales sin sobreventa (23/10)
- [ ] #127 — feat(ventas): máquina de estados del pedido, entregar y cancelar (27/10)
- [ ] #131 — fix(ventas): listado de clientes paginado por cursor (27/10)
- [ ] #133 — feat(ventas): GET de reservas y despachos de un pedido (29/10)
- [ ] #140 — feat(reservas): Idempotency-Key en POST /reservas (31/10)
- [ ] F#3 — feat(frontend): base del frontend con Vite, React, TypeScript y pantalla inicial (28/09)
- [ ] F#43 — feat(clientes): país por desplegable, 409 cliente_con_pedidos en el tipo y tests del DoD (29/10)
- [ ] F#45 — feat(pedidos): listado, alta, detalle, reservar, entregar entre bancos y trazabilidad (29/10)
- [ ] F#48 — feat(disponibilidad): vista por raza con desglose por banco (31/10)
- [ ] F#50 — feat(reservas): reservar con conflictos de concurrencia y reintento (31/10)

### @mmmelissa099

- [ ] #14 — feat(api): endpoints GET /health y /health/ready sobre Lambda y API Gateway (21/09)
- [ ] #51 — Ci/workflow lint test (30/09)
- [ ] #52 — Feat/core errors logs (30/09)
- [ ] #55 — Ci/deploy OIDC (01/10)
- [ ] #95 — Feat/idempotencia (10/10)
- [ ] #97 — Feat/health ready (11/10)
- [ ] #99 — docs: dominio de pedidos y modalidades de exportación (11/10)
