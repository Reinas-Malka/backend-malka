# ADR 0012 — Excepción temporal: claves IAM de largo plazo para los applies

- **Estado:** aceptada (excepción con caducidad)
- **Fecha:** 2026-10-09
- **Decisores:** Felipe Andreau (dueño de la cuenta)
- **Caducidad:** 30/11/2026 (defensa final) — rotar o eliminar después

## Contexto

La doctrina del proyecto es **solo OIDC** para el CI (ADR 0005): ningún
secreto de largo plazo en el flujo de deploy. Pero `terraform apply` es
manual (ADR 0007) y corre desde las terminales locales, que necesitan
credenciales. La auditoría del 09/10 encontró dos claves activas:

- `felipe-admin-supremo` — creada el 20/09/2026 (uso: `AWS_PROFILE=malka`,
  los applies; hoy una sola persona los corre)
- `pia-porzio` — creada el 01/10/2026

## Decisión

**Se aceptan como excepción temporal, con caducidad el día de la defensa.**
Después del 30/11/2026 se rotan o eliminan, y los applies migran a
credenciales temporales (SSO o assumes del rol de deploy con MFA).

Esto NO es una puerta abierta: cualquier clave nueva de largo plazo sigue
siendo hallazgo de auditoría. La excepción cubre exactamente estas dos.

## Consecuencias

- Los applies no dependen de un flujo por construir antes de la defensa.
- La superficie es conocida y acotada (dos claves, ambas en usuarios del
  equipo, repo público sin exposición).
- Al vencer, este ADR se marca superseded por el que documente el reemplazo.
