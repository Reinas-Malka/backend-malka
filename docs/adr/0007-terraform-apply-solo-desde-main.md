# ADR 0007 — Terraform apply solo desde main, con código pusheado

- **Estado:** aceptada
- **Fecha:** 2026-10-04
- **Decisores:** equipo Reinas Malka
- **Contexto:** incidente de Cognito del 01/10/2026

## Contexto

El 01/10 un `terraform plan` desde `main` propuso **destruir el user pool de
Cognito y 7 recursos asociados**: existían en el state pero ninguna rama
pusheada los definía, porque se habían aplicado desde una rama local que el
resto del equipo no veía. Se detectó antes de aplicar; se resolvió mergeando
esa rama (PR #65) y el plan volvió a `No changes` (evidencia en
`docs/evidencia/2026-10-04-plan-no-changes.txt`).

El riesgo de fondo: aplicar desde una rama local crea state que `main` no
sabe explicar. El próximo `plan` desde `main` propone destruirlo, y quien lo
corra sin saber el contexto pierde recursos reales.

## Decisión

`terraform apply` se ejecuta **solo desde `main`** y solo con el código ya
pusheado (o sea, después de que el PR correspondiente se mergeó). Antes de
cada apply: `git pull` y `terraform plan` para confirmar que el diff es el
esperado. Ningún recurso se crea desde ramas locales o sin pushear.

## Consecuencias

- El state siempre es explicable desde `main`; un `plan` limpio es la
  evidencia (útil para la defensa del 30/11).
- Si un recurso existe en el state sin código en `main`, el tratamiento es
  alinear el código al state y pushearlo — nunca recrear el recurso (en
  Cognito recrear pierde usuarios: `custom:tenant_id` es inmutable).
- Cambios urgentes en infra siguen el mismo camino: PR chico, review, merge,
  apply.
