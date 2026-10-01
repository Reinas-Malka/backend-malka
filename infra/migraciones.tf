###############################################################################
# Lambda de migraciones
#
# Usa la misma imagen que la API con otro handler (app/migrar.py). Corre dentro
# de la VPC para llegar a RDS, que es privada, y lee la credencial del duenio
# de la base por el VPC endpoint de Secrets Manager que ya existe, asi que no
# suma costo fijo. Se invoca a mano con `aws lambda invoke` y desde el deploy
# (#10), antes de actualizar la API.
###############################################################################

# Rol propio y no el de la API: las migraciones usan la credencial del duenio
# de las tablas, y la API va a usar un rol sin privilegios (#19).
resource "aws_iam_role" "lambda_migraciones" {
  name               = "${local.name}-rol-lambda-migraciones"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = {
    Name = "${local.name}-rol-lambda-migraciones"
  }
}

# Permite escribir logs en CloudWatch.
resource "aws_iam_role_policy_attachment" "migraciones_basica" {
  role       = aws_iam_role.lambda_migraciones.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Permite crear las interfaces de red para correr dentro de la VPC.
resource "aws_iam_role_policy_attachment" "migraciones_vpc" {
  role       = aws_iam_role.lambda_migraciones.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Permite enviar las trazas de X-Ray.
resource "aws_iam_role_policy_attachment" "migraciones_xray" {
  role       = aws_iam_role.lambda_migraciones.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# Solo puede leer el secreto del duenio de la base (definido en rds.tf).
resource "aws_iam_role_policy" "migraciones_secretos" {
  name   = "${local.name}-migraciones-lectura-de-secretos"
  role   = aws_iam_role.lambda_migraciones.id
  policy = data.aws_iam_policy_document.lambda_secretos.json
}

# Se declara explicitamente para fijar la retencion.
resource "aws_cloudwatch_log_group" "lambda_migraciones" {
  name              = "/aws/lambda/${local.name}-migraciones"
  retention_in_days = 14

  tags = {
    Name = "${local.name}-migraciones-logs"
  }
}

# Una migracion puede tardar mas que una request: timeout de 5 minutos
# (el maximo de Lambda es 15).
resource "aws_lambda_function" "migraciones" {
  function_name = "${local.name}-migraciones"
  role          = aws_iam_role.lambda_migraciones.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
  memory_size   = 512
  timeout       = 300
  architectures = ["x86_64"]

  # Sin reserved_concurrent_executions a proposito: la cuota de la cuenta no
  # permite reservar (ver el worker en sqs.tf). Se invoca a mano o desde el
  # deploy, una ejecucion por vez.

  # Misma imagen, otro punto de entrada.
  image_config {
    command = ["app.migrar.handler"]
  }

  vpc_config {
    subnet_ids         = aws_subnet.privada[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      APP_ENVIRONMENT = var.environment
      APP_VERSION     = var.image_tag
      DB_SECRET_NAME  = aws_secretsmanager_secret.db_owner.name
    }
  }

  tracing_config {
    mode = "Active"
  }

  tags = {
    Name = "${local.name}-migraciones"
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_migraciones,
    aws_iam_role_policy_attachment.migraciones_basica,
    aws_iam_role_policy_attachment.migraciones_vpc,
  ]
}

output "lambda_migraciones_nombre" {
  description = "Nombre de la funcion Lambda que aplica las migraciones"
  value       = aws_lambda_function.migraciones.function_name
}