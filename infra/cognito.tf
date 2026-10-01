# Cognito: user pool, app client y grupos de roles.
# El backend no emite tokens: solo valida los que emite Cognito.

locals {
  cognito_nombre = "${var.project}-${var.environment}"

  # Se derivan de los origenes ya permitidos para CORS.
  # Asume que la SPA atiende el callback en /auth/callback.
  cognito_callback_urls = [for o in var.origenes_permitidos : "${o}/auth/callback"]
  cognito_logout_urls   = [for o in var.origenes_permitidos : "${o}/"]

  cognito_grupos = {
    admin      = "Administracion del tenant y configuracion"
    produccion = "Capataz: tandas, etapas y bancos"
    ventas     = "Pedidos, reservas y documentos"
    lectura    = "Solo consulta"
  }
}

# El dominio del login hospedado tiene que ser unico en toda la region.
resource "random_id" "cognito_dominio" {
  byte_length = 3
}

resource "aws_cognito_user_pool" "principal" {
  name = local.cognito_nombre

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"

  # Los usuarios los da de alta un admin: no hay auto-registro en un SaaS B2B.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # custom:tenant_id es inmutable: una vez asignado, el usuario no puede
  # cambiarse de tenant.
  schema {
    name                     = "tenant_id"
    attribute_data_type      = "String"
    mutable                  = false
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }
}

resource "aws_cognito_user_pool_domain" "principal" {
  domain       = "${local.cognito_nombre}-${random_id.cognito_dominio.hex}"
  user_pool_id = aws_cognito_user_pool.principal.id
}

resource "aws_cognito_user_pool_client" "spa" {
  name         = "${local.cognito_nombre}-spa"
  user_pool_id = aws_cognito_user_pool.principal.id

  generate_secret = false # SPA: cliente publico, no puede guardar un secret

  explicit_auth_flows = concat(
    ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    var.cognito_password_auth_habilitado ? ["ALLOW_USER_PASSWORD_AUTH"] : []
  )

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = local.cognito_callback_urls
  logout_urls                          = local.cognito_logout_urls

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  # El cliente puede LEER el tenant_id pero no escribirlo.
  read_attributes  = ["email", "email_verified", "custom:tenant_id"]
  write_attributes = ["email"]

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_group" "roles" {
  for_each     = local.cognito_grupos
  name         = each.key
  description  = each.value
  user_pool_id = aws_cognito_user_pool.principal.id
}

# ---------------------------------------------------------------------------
# Variable y outputs de Cognito
# ---------------------------------------------------------------------------

variable "cognito_password_auth_habilitado" {
  description = "Habilita USER_PASSWORD_AUTH para sacar tokens por CLI en tests y demos. Apagar en produccion"
  type        = bool
  default     = true
}

output "cognito_user_pool_id" {
  description = "Identificador del user pool de Cognito (publico, va al frontend)"
  value       = aws_cognito_user_pool.principal.id
}

output "cognito_client_id" {
  description = "Identificador del app client de la SPA (publico, va al frontend)"
  value       = aws_cognito_user_pool_client.spa.id
}

output "cognito_issuer" {
  description = "Valor esperado del claim iss al validar los JWT"
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.principal.id}"
}

output "cognito_jwks_url" {
  description = "URL de las claves publicas para verificar la firma de los JWT"
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.principal.id}/.well-known/jwks.json"
}

output "cognito_login_url" {
  description = "Dominio del login hospedado de Cognito"
  value       = "https://${aws_cognito_user_pool_domain.principal.domain}.auth.${var.region}.amazoncognito.com"
}