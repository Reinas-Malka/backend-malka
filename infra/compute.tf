# Funcion Lambda que atiende la API.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_api" {
  name               = "${local.name}-rol-lambda-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = {
    Name = "${local.name}-rol-lambda-api"
  }
}

# Permite escribir logs en CloudWatch.
resource "aws_iam_role_policy_attachment" "lambda_basica" {
  role       = aws_iam_role.lambda_api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Permite crear las interfaces de red para correr dentro de la VPC.
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda_api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Permite enviar las trazas de X-Ray.
resource "aws_iam_role_policy_attachment" "lambda_xray" {
  role       = aws_iam_role.lambda_api.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# Se declara explicitamente para fijar la retencion. Si no, Lambda lo crea solo
# y guarda los logs para siempre.
resource "aws_cloudwatch_log_group" "lambda_api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 14

  tags = {
    Name = "${local.name}-api-logs"
  }
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.lambda_api.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
  memory_size   = 1024
  timeout       = 30
  architectures = ["x86_64"]

  # Va dentro de la VPC porque mas adelante tiene que hablar con RDS.
  vpc_config {
    subnet_ids         = aws_subnet.privada[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      APP_ENVIRONMENT = var.environment
      APP_VERSION     = var.image_tag
    }
  }

  tracing_config {
    mode = "Active"
  }

  tags = {
    Name = "${local.name}-api"
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_api,
    aws_iam_role_policy_attachment.lambda_basica,
    aws_iam_role_policy_attachment.lambda_vpc,
  ]
}
