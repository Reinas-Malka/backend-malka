# Cognito: usuarios de prueba y tokens

Definido en `infra/cognito.tf`. El backend no emite tokens: solo valida los de Cognito.

## Grupos (roles)

`admin`, `produccion`, `ventas`, `lectura`. El token los trae en el claim `cognito:groups`.

## Tenant

Cada usuario tiene `custom:tenant_id` (inmutable, lo asigna un admin al crearlo).
El backend usa ese valor para `SET LOCAL app.tenant_id`.

## Datos publicos (van al frontend y al backend)

```bash
terraform -chdir=infra output cognito_user_pool_id
terraform -chdir=infra output cognito_client_id
terraform -chdir=infra output cognito_issuer
terraform -chdir=infra output cognito_jwks_url
terraform -chdir=infra output cognito_login_url
```

## Usuarios de prueba

| Usuario | Grupo | Tenant |
|---|---|---|
| admin@tenant-a.malka.test | admin | A (1111...) |
| produccion@tenant-a.malka.test | produccion | A |
| ventas@tenant-a.malka.test | ventas | A |
| lectura@tenant-a.malka.test | lectura | A |
| admin@tenant-b.malka.test | admin | B (2222...) |

La contrasena NO se sube al repo: se pasa por variable de entorno.

```bash
export POOL_ID=$(terraform -chdir=infra output -raw cognito_user_pool_id)
export CLIENT_ID=$(terraform -chdir=infra output -raw cognito_client_id)
export TEST_PASSWORD='<12+ caracteres, mayuscula, minuscula y numero>'

./infra/scripts/seed_cognito_users.sh seed
./infra/scripts/seed_cognito_users.sh token admin@tenant-a.malka.test
```

## Importante para validar los tokens en el backend

Usar el **ID token**: es el que trae `aud` y `custom:tenant_id`.
El access token de Cognito no tiene `aud` (tiene `client_id`) ni `custom:tenant_id`.
Por eso el frontend debe mandar el ID token en `Authorization: Bearer`.

## Antes de usar en produccion

Poner `cognito_password_auth_habilitado = false`.
