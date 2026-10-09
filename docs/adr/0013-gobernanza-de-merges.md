# ADR 0013 — Gobernanza de merges: CI obligatorio, aprobación solo para infra

- **Estado:** aceptada
- **Fecha:** 2026-10-09
- **Decisores:** Felipe Andreau (dueño)
- **Reemplaza:** la regla "el autor no auto-aprueba" del AGENTS.md original

## Contexto

Somos cinco. La regla de "una aprobación para todo merge" demostró costar
**días de cola**: PRs CI-verdes esperando a que alguien entre a GitHub, con
el equipo trabajando en paralelo y el dueño fusionando por `--admin` con el
ruleset apagado — cinco desactivaciones de la protección en una tarde, en el
historial del repo. Eso es peor que el problema que la regla quería evitar:
el CI con checks obligatorios (calidad, imagen, infra, tests + los guards de
Alembic y JWKS) es lo que de verdad protege `main`, y nunca estuvo opcional.

## Decisión

1. **Checks de CI obligatorios en ambos repos** para tocar `main`. Sin verde, no hay merge.
2. **Sin requisito de aprobación general**: el autor puede mergear su PR si el CI está verde.
3. **Excepción — `infra/`**: todo cambio de infraestructura exige aprobación de un dueño de infra vía CODEOWNERS (`/infra/` → Felipe, Pilar). La infra se paga en dinero real y se aplica con manos; ahí sí hace falta un segundo par de ojos.
4. Los guards del CI (una sola cabeza de Alembic, JWKS offline) siguen siendo la primera línea.

## Consecuencias

- La cola de review desaparece para código; queda solo donde el riesgo es real.
- El historial no acumula más desactivaciones de protección.
- Si el equipo crece o entra alguien externo, se revisa esta decisión primero.
