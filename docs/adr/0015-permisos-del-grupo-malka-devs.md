# ADR 0015 — Permisos del grupo malka-devs: en Terraform y con mínimo privilegio

- **Estado:** propuesta (requiere la aprobación de CODEOWNERS de `infra/`)
- **Fecha:** 2026-11-02
- **Decisores:** Felipe Andreau (dueño de la cuenta), Pilar Wagner (codeowner de infra)
- **Complementa:** ADR 0012 (claves de largo plazo) y ADR 0007 (applies manuales desde main)
- **Issue:** #103

## Contexto

La auditoría de fugas del 15/10 encontró que el grupo `malka-devs`, creado a
mano el 01/10, tenía además de `ReadOnlyAccess` tres políticas inline que
ningún ADR documentaba y que no estaban en Terraform:

| Política | Permiso | Riesgo |
|---|---|---|
| `tfstate-escritura` | `s3:PutObject` y `s3:DeleteObject` sobre `dev/*` del bucket del state | Un integrante podía pisar o borrar el state |
| `cognito-escritura` | `cognito-idp:*` y `cognito-identity:*` sobre `*` | Crear o borrar pools, resetear passwords de cualquier usuario |
| `auto-gestion` | IAM sobre el propio usuario | Bajo: es lo esperado |

Uso real según IAM (*last accessed*, consultado el 02/11):

- El grupo tiene un solo integrante (`pia-porzio`).
- `cognito-idp`: escrituras solo el 01/10 (creación inicial del pool y de los
  usuarios de prueba, hoy gestionados por Terraform y por
  `infra/scripts/seed_cognito_users.sh`). Desde el 06/10, solo lecturas, que
  ya cubre `ReadOnlyAccess`.
- `cognito-identity`: nunca se usó.
- El state se lee con `ReadOnlyAccess`. `terraform plan` con
  `use_lockfile = true` necesita escribir y borrar el lockfile; nada más.

## Decisión

1. **Las políticas pasan a Terraform** (`infra/iam_devs.tf`) con bloques
   `import`: no se recrea el grupo ni se cortan accesos durante el apply.
2. **`tfstate-escritura`** queda limitada a `dev/terraform.tfstate.tflock`.
   Los integrantes pueden correr `terraform plan`; solo el dueño aplica,
   desde `main` (ADR 0007).
3. **`cognito-escritura`** queda limitada al ARN del pool del proyecto y a las
   acciones de `seed_cognito_users.sh` (`AdminCreateUser`,
   `AdminSetUserPassword`, `AdminAddUserToGroup`,
   `AdminUpdateUserAttributes`, `AdminGetUser`). Se quita `cognito-identity`.
4. **`auto-gestion`** se mantiene igual.

## Consecuencias

- Un recurso de IAM creado a mano vuelve a ser hallazgo: el grupo ya no lo es.
- Ningún integrante fuera del dueño puede alterar el state ni el pool.
- El plan esperado del apply es `5 to import, 0 to add, 2 to change, 0 to destroy`.
- Al vencer el ADR 0012 (30/11/2026), si los accesos migran a SSO, este grupo
  se revisa junto con las claves.
