# ADR 0010 — JWKS de Cognito como archivo versionado, no leído en runtime

- **Estado:** aceptada
- **Fecha:** 2026-10-07
- **Decisores:** equipo Reinas Malka
- **Issues:** #21 (validación de JWT)
- **Complementa:** [ADR 0002](0002-red-privada-sin-nat.md) (red sin salida)

## Contexto

La validación de los JWT de Cognito (#21) necesita el JWKS del user pool. La
Lambda corre en una VPC **sin salida a internet** (ADR 0002: sin NAT y sin
endpoint de interface para `cognito-idp`): leer el JWKS en runtime desde la
URL de AWS es imposible por diseño, y agregar un VPC endpoint solo para esto
cuesta ~7,30 USD/mes por algo que no cambia.

Las claves de un user pool **no rotan**: existen mientras el pool exista.
Fijar su valor no tiene costo de frescura.

## Decisión

- El JWKS se guarda como **archivo versionado en el repo** y viaja **copiado
  dentro de la imagen** de la Lambda. La validación (#21) lo lee de ahí;
  jamás lo descarga en runtime.
- El output `cognito_jwks_url` de Terraform (`infra/cognito.tf`) **no se
  consume en runtime ni como variable de entorno**: existe para que un humano
  **regenere el archivo a mano** (descargar la URL, commit, PR) si el pool
  llegara a cambiar de claves. El próximo que vea ese output: esta es su
  única razón de ser.

## Consecuencias

- Cero dependencia de red y cero costo para validar tokens.
- Si el pool cambiara sus claves (recrearlo, no rotación), la validación
  falla hasta regenerar el archivo por PR — visible y auditable.
- No confundir: el requisito "JWKS cacheado fuera del handler" del #21 se
  cumple de sobra (ya está en disco en la imagen).

## Alternativas consideradas

**Leer la URL en runtime (env var con `cognito_jwks_url`).** Descartada: no
hay salida a internet desde la VPC y un endpoint dedicado cuesta dinero que
no aporta nada.

**Endpoint de interface para `cognito-idp`.** Descartada por costo frente a
un archivo que no cambia.
