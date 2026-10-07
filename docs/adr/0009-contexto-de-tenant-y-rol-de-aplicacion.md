# ADR 0009 — Contexto de tenant con SET LOCAL y rol de aplicación sin privilegios

- **Estado:** aceptada
- **Fecha:** 2026-10-06
- **Decisores:** equipo Reinas Malka
- **Issues:** #19 (completa ADR 0003)

## Contexto

El ADR 0003 decidió aislar a los inquilinos con Row Level Security. Pero RLS no se activa solo: si la aplicación se conecta con el dueño de las tablas o con un superusuario, las políticas se saltean **en silencio**. Y si el tenant se fija con `SET` (sin `LOCAL`), queda pegado a la conexión del pool y el request siguiente puede ver datos de otro criadero.

## Decisión

1. **Rol propio para la API**: `malka_app` (migración 0002), sin `SUPERUSER`, sin `BYPASSRLS` y sin ser dueño de ninguna tabla. Las migraciones siguen usando el dueño.
2. **Permisos explícitos y mínimos por tabla**, en la misma migración que crea la tabla. No se usa `ALTER DEFAULT PRIVILEGES`: le daría permisos también a tablas futuras sin RLS (como `tenant`) y no se vería en el review.
3. **Tenant por transacción**: toda consulta pasa por `app/db.py::sesion_de_tenant`, que ejecuta `set_config('app.tenant_id', :tenant, true)` (equivalente a `SET LOCAL`, pero con parámetro ligado). Al cerrar la transacción el valor desaparece.
4. **Los servicios no filtran por tenant a mano**: no se escribe `WHERE tenant_id = ...`. `tenant_id` toma su valor por defecto de la sesión y `WITH CHECK` rechaza escrituras en otro tenant. `tests/test_convencion_tenant.py` lo hace cumplir.
5. **Credencial fuera del repo y del tfstate**: Terraform crea el secreto `malka-suite-dev/db/app` vacío. La Lambda de migraciones genera la clave, la guarda en el secreto y la fija en PostgreSQL como hash SCRAM. A diferencia de la clave del dueño (riesgo asentado en el ADR 0003), no queda en claro en el state. La API solo puede leer este secreto, nunca el del dueño.

## Consecuencias

**A favor**
- Un `WHERE` olvidado ya no filtra datos de otro criadero: sin tenant, cualquier consulta devuelve cero filas.
- Un error de la aplicación no puede saltear RLS, porque el rol no tiene con qué.

**En contra**
- **Checklist para toda tabla nueva con `tenant_id`**: `ENABLE` + `FORCE ROW LEVEL SECURITY`, política `aislamiento_por_tenant`, `GRANT` mínimo a `malka_app` y `DEFAULT` del tenant. `test_toda_tabla_con_tenant_id_tiene_rls_y_permisos` falla si falta algo.
- Toda tabla de negocio lleva `tenant_id`, aunque sea hija de otra (por ejemplo, `documento_embarque`): sin la columna no hay política posible.
- Para rotar la clave hay que vaciar el secreto y volver a invocar la Lambda de migraciones.

## Alternativas consideradas

**`SET` de sesión.** Más simple, pero el tenant sobrevive a la transacción y se filtra entre requests por el pool de conexiones. Descartada.

**Conectarse con el dueño y filtrar en el código.** Es lo que el ADR 0003 quiso evitar: un `WHERE` olvidado expone datos de otro cliente. Descartada.

**Generar la clave con `random_password` de Terraform.** Es lo que se hace con el dueño, pero deja la clave en claro en el tfstate. Descartada.