# ADR 0003 — PostgreSQL en RDS con credenciales gestionadas y TLS obligatorio

- **Estado:** aceptada
- **Fecha:** 2026-09-21
- **Decisores:** equipo Reinas Malka
- **Issues:** #5

## Contexto

Malka Suite maneja datos relacionales con integridad referencial clara (tandas de cría, madres, razas, bancos, clientes, pedidos y documentos comerciales) y es multi-inquilino: varios clientes comparten la aplicación y **ninguno debe poder ver los datos de otro**. También necesitamos consultas con joins y agregaciones para los tableros.

Un TP que guarda contraseñas en el repositorio o en variables de entorno en texto plano pierde puntos con razones de sobra, así que el manejo del secreto era parte del problema desde el principio.

## Decisión

**RDS PostgreSQL 16** en `db.t4g.micro`, sin acceso público, en las subredes privadas.

- La contraseña la genera **`random_password` de Terraform** y se guarda en **Secrets Manager** (`malka-suite-dev/db/owner`). No existe en el repositorio ni en el código de la aplicación: la Lambda la lee en tiempo de ejecución con permisos IAM acotados a ese ARN.
- El parameter group propio (`malka-suite-dev-pg16`) fija **`rds.force_ssl = 1`**: la base rechaza conexiones sin TLS.
- El aislamiento entre inquilinos se implementará con **Row Level Security** de PostgreSQL, con políticas por `tenant_id`. La decisión es hacerlo en la base y no solo en la aplicación: si el filtro vive únicamente en el código, un `WHERE` olvidado filtra datos de otro cliente.

### Restricciones del plan gratuito

- `backup_retention_period = 1`. Con 7 días, la creación falla con `FreeTierRestrictionError`.
- Sin `max_allocated_storage`: el autoscaling de almacenamiento no está permitido, así que el tamaño es fijo.

## Consecuencias

**A favor**

- Backups automáticos, parches y monitoreo a cargo de AWS.
- El secreto es rotable sin tocar código ni redeployar.
- RLS da una garantía de aislamiento que sobrevive a errores de la aplicación.
- PostgreSQL nos deja la puerta abierta a `pgvector` si el módulo de IA del Checkpoint 2 necesita búsqueda semántica.

**En contra**

- **Una sola AZ y un día de retención**: no es una configuración productiva. Es una limitación consciente del plan gratuito y hay que decirlo en la defensa.
- Leer el secreto agrega latencia a los cold starts; conviene cachearlo fuera del handler.
- RLS exige disciplina: cada sesión debe fijar el `tenant_id` y las políticas hay que testearlas, porque un error silencioso no da error sino datos de menos.
- La instancia corre 24/7 aunque no haya tráfico, a diferencia del resto de la arquitectura.

## Alternativas consideradas

**Aurora Serverless v2.** Escala a demanda y sería el match ideal con Lambda, pero su capacidad mínima facturable la vuelve más cara que `db.t4g.micro` para un uso tan bajo. Descartada por costo.

**DynamoDB.** Encaja naturalmente con Lambda y tiene capa gratuita generosa, pero nuestro modelo es relacional y los reportes con joins serían mucho más trabajosos. Además no ofrece nada equivalente a RLS. Descartada por el modelo de datos.

**Contraseña en variables de entorno de la Lambda.** Más simple y sin el costo del VPC endpoint, pero queda visible para cualquiera con acceso de lectura a la consola y no es rotable. Descartada por seguridad.

**Una base por inquilino.** El aislamiento más fuerte posible, pero multiplica el costo y las migraciones. Descartada por costo y por operación.
