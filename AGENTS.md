# AGENTS.md — reglas para agentes que trabajan este repo

Backend de Malka Suite (TP Cloud, UTN FRLP). FastAPI + Lambda + Terraform.
**El negocio está en [`docs/dominio.md`](docs/dominio.md)**: leelo antes de
tocar modelos, migraciones o cálculos. Si ese documento contradice código o
ADRs, gana el documento y el conflicto se reporta como issue.

## Reglas duras

1. **Ningún cambio llega a `main` sin PR** con CI verde y revisión de otra
   persona. El autor no auto-aprueba.
2. **`terraform apply` solo desde `main`** con el código ya pusheado (ADR 0007).
   Nunca desde ramas locales ni sin pushear.
3. **Cero secretos en el repo** (público): sin claves AWS, sin contraseñas, sin
   emails personales en archivos versionados. Las variables sensibles van por
   entorno (`TF_VAR_*`); `*.tfvars` está ignorado.
4. **`custom:tenant_id` es inmutable**: recrear el user pool de Cognito pierde
   los usuarios. Ante drift de Cognito, alinear el código al state, jamás
   recrear.
5. **RLS no se degrada ni se posterga**: los tests deben incluir el caso de
   fuga entre tenants.
6. La IA redacta borradores; **nunca** genera números fiscales ni los aprueba
   (fail-closed, `docs/dominio.md` regla general).
7. Logs sin cuerpos de mensajes ni PII: JSON con request_id.

## Convenciones

- Commits en español, formato conventional (`feat:`, `fix:`, `docs:`,
  `infra:`…). Issues con Contexto/Tareas/DoD.
- `ruff format` + `ruff check` antes de pushear (el CI de calidad los exige).
- Diagramas: el oficial es `docs/diagramas/arquitectura.png`;
  `generar_*.py` regenera la base.
