#!/usr/bin/env bash
# Crea usuarios de prueba en Cognito: uno por rol en un tenant A y un admin en un tenant B.
# Sirve para las demos y para probar el aislamiento entre tenants.
#
# Requisitos: AWS CLI con credenciales que permitan administrar el user pool.
# Ejecutar desde la raiz del repositorio.
#
# Uso:
#   export POOL_ID=$(terraform -chdir=infra output -raw cognito_user_pool_id)
#   export CLIENT_ID=$(terraform -chdir=infra output -raw cognito_client_id)
#   export TEST_PASSWORD='<contrasena propia: 12+ caracteres, mayuscula, minuscula y numero>'
#   ./infra/scripts/seed_cognito_users.sh seed                              # crea los usuarios (una sola vez)
#   ./infra/scripts/seed_cognito_users.sh token admin@tenant-a.malka.test   # imprime el ID token
#
# TEST_PASSWORD se lee del entorno: no se escribe en el repo ni pasa por Terraform,
# asi no queda en el state.
# El ID token es una credencial temporal: no compartirlo ni pegarlo en issues o PR.
set -euo pipefail

: "${POOL_ID:?falta POOL_ID}"

TENANT_A="${TENANT_A:-11111111-1111-4111-8111-111111111111}"
TENANT_B="${TENANT_B:-22222222-2222-4222-8222-222222222222}"

create_user() { # email tenant grupo
  local email="$1" tenant="$2" group="$3"
  aws cognito-idp admin-create-user \
    --user-pool-id "$POOL_ID" --username "$email" \
    --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
                      Name=custom:tenant_id,Value="$tenant" \
    --message-action SUPPRESS >/dev/null
  aws cognito-idp admin-set-user-password \
    --user-pool-id "$POOL_ID" --username "$email" \
    --password "${TEST_PASSWORD:?falta TEST_PASSWORD}" --permanent
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id "$POOL_ID" --username "$email" --group-name "$group"
  echo "ok: $email ($group, tenant $tenant)"
}

case "${1:-}" in
  seed)
    create_user admin@tenant-a.malka.test      "$TENANT_A" admin
    create_user produccion@tenant-a.malka.test "$TENANT_A" produccion
    create_user ventas@tenant-a.malka.test     "$TENANT_A" ventas
    create_user lectura@tenant-a.malka.test    "$TENANT_A" lectura
    create_user admin@tenant-b.malka.test      "$TENANT_B" admin
    ;;
  token) # imprime el ID token (lleva aud y custom:tenant_id)
    : "${CLIENT_ID:?falta CLIENT_ID}"
    aws cognito-idp initiate-auth --client-id "$CLIENT_ID" \
      --auth-flow USER_PASSWORD_AUTH \
      --auth-parameters USERNAME="${2:?email}",PASSWORD="${TEST_PASSWORD:?falta TEST_PASSWORD}" \
      --query 'AuthenticationResult.IdToken' --output text
    ;;
  *) echo "uso: $0 seed | token <email>"; exit 1 ;;
esac
