###############################################################################
# Rol de aplicacion de la base 
#
# La API se conecta con malka_app (migracion 0002): sin BYPASSRLS y sin ser
# duenio de las tablas. Terraform crea el secreto VACIO: la clave la genera
# la Lambda de migraciones y la guarda aca, asi no queda en claro en el
# tfstate (el riesgo que ADR 0003 asienta para la clave del duenio).
###############################################################################

resource "aws_secretsmanager_secret" "db_app" {
  name                    = "${local.name}/db/app"
  description             = "Credenciales del rol de aplicacion (sin privilegios) de Malka Suite"
  recovery_window_in_days = 0

  tags = {
    Name = "${local.name}-db-app"
  }
}

# La API solo lee este secreto (se usa en rds.tf).
data "aws_iam_policy_document" "lambda_secreto_app" {
  statement {
    sid       = "LeerCredencialDelRolDeAplicacion"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_app.arn]
  }
}

# Las migraciones generan la clave la primera vez y despues la releen.
data "aws_iam_policy_document" "migraciones_secreto_app" {
  statement {
    sid    = "GenerarYLeerCredencialDelRolDeAplicacion"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
    ]
    resources = [aws_secretsmanager_secret.db_app.arn]
  }
}

resource "aws_iam_role_policy" "migraciones_secreto_app" {
  name   = "${local.name}-migraciones-secreto-app"
  role   = aws_iam_role.lambda_migraciones.id
  policy = data.aws_iam_policy_document.migraciones_secreto_app.json
}

output "db_app_secret_nombre" {
  description = "Nombre del secreto con las credenciales del rol de aplicacion"
  value       = aws_secretsmanager_secret.db_app.name
}