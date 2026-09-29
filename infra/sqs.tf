###############################################################################
# Colas SQS del circuito de documentos (issue #37)
#
# Una cola por tipo de trabajo: `documentos` para el borrador asincronico
# con Bedrock (#40) y `ingesta` para el procesamiento de fotos. Cada cola
# tiene su DLQ: un mensaje que falla 3 veces aterriza ahi y dispara la
# alarma, en vez de reintentarse para siempre.
###############################################################################

# La DLQ retiene 14 dias (el maximo de SQS) para dar tiempo a investigar.
resource "aws_sqs_queue" "documentos_dlq" {
  name                      = "${local.name}-documentos-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.name}-documentos-dlq"
  }
}

resource "aws_sqs_queue" "ingesta_dlq" {
  name                      = "${local.name}-ingesta-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = {
    Name = "${local.name}-ingesta-dlq"
  }
}

# El timeout de visibilidad es 6 veces el timeout de la Lambda worker: si una
# ejecucion tarda, el mensaje no vuelve a la cola mientras otra lo procesa.
resource "aws_sqs_queue" "documentos" {
  name                       = "${local.name}-documentos"
  visibility_timeout_seconds = 360
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.documentos_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Name = "${local.name}-documentos"
  }
}

resource "aws_sqs_queue" "ingesta" {
  name                       = "${local.name}-ingesta"
  visibility_timeout_seconds = 360
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingesta_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Name = "${local.name}-ingesta"
  }
}

###############################################################################
# Lambda worker
#
# Misma imagen de ECR que la API, pero con otro handler: el command de
# image_config pisa el CMD del Dockerfile. Corre en la VPC porque #40 leera
# el contexto de RDS y llamara a Bedrock por su VPC endpoint.
###############################################################################

resource "aws_iam_role" "lambda_worker" {
  name               = "${local.name}-rol-lambda-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = {
    Name = "${local.name}-rol-lambda-worker"
  }
}

resource "aws_iam_role_policy_attachment" "worker_basica" {
  role       = aws_iam_role.lambda_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "worker_vpc" {
  role       = aws_iam_role.lambda_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "worker_xray" {
  role       = aws_iam_role.lambda_worker.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# El worker consume: solo recibir, borrar y mirar atributos de las dos colas.
resource "aws_iam_role_policy" "worker_consume_sqs" {
  name = "${local.name}-worker-consume-sqs"
  role = aws_iam_role.lambda_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = [aws_sqs_queue.documentos.arn, aws_sqs_queue.ingesta.arn]
      },
    ]
  })
}

# Invocacion de Bedrock, lo que quedo pendiente del #39: el perfil
# us. enruta a los modelos de fundacion, por eso se permiten los dos ARN.
resource "aws_iam_role_policy" "worker_bedrock" {
  name = "${local.name}-worker-bedrock"
  role = aws_iam_role.lambda_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "bedrock:InvokeModel"
        Resource = local.arns_modelo_bedrock
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda_worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = 14

  tags = {
    Name = "${local.name}-worker-logs"
  }
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.lambda_worker.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
  memory_size   = 512
  timeout       = 60
  architectures = ["x86_64"]

  # Concurrencia acotada: dos ejecuciones como maximo, para no saturar la
  # base ni disparar el consumo de Bedrock cuando entra una hornada de mensajes.
  reserved_concurrent_executions = 2

  image_config {
    command = ["app.worker.handler"]
  }

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
    Name = "${local.name}-worker"
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_worker,
    aws_iam_role_policy_attachment.worker_basica,
    aws_iam_role_policy_attachment.worker_vpc,
  ]
}

# batch_size 1: cada mensaje es una ejecucion, asi un reintento solo repite
# el mensaje que fallo y no todo el lote.
resource "aws_lambda_event_source_mapping" "documentos" {
  event_source_arn                   = aws_sqs_queue.documentos.arn
  function_name                      = aws_lambda_function.worker.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
}

resource "aws_lambda_event_source_mapping" "ingesta" {
  event_source_arn                   = aws_sqs_queue.ingesta.arn
  function_name                      = aws_lambda_function.worker.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
}

###############################################################################
# Alarmas de DLQ
#
# Cualquier mensaje visible en una DLQ significa que fallo 3 veces y hay que
# mirarlo. El destino (SNS/email) se engancha con el modulo de metricas.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "dlq_documentos" {
  alarm_name          = "${local.name}-dlq-documentos-con-mensajes"
  alarm_description   = "Hay mensajes en la DLQ de documentos: fallaron 3 veces y nadie los proceso"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.documentos_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  tags = {
    Name = "${local.name}-dlq-documentos-con-mensajes"
  }
}

resource "aws_cloudwatch_metric_alarm" "dlq_ingesta" {
  alarm_name          = "${local.name}-dlq-ingesta-con-mensajes"
  alarm_description   = "Hay mensajes en la DLQ de ingesta: fallaron 3 veces y nadie los proceso"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.ingesta_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  tags = {
    Name = "${local.name}-dlq-ingesta-con-mensajes"
  }
}

###############################################################################
# Permisos de envio para la API
###############################################################################

# La API envia: solo SendMessage, y solo sobre las dos colas principales
# (nunca directo a una DLQ).
resource "aws_iam_role_policy" "api_envia_sqs" {
  name = "${local.name}-api-envia-sqs"
  role = aws_iam_role.lambda_api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = [aws_sqs_queue.documentos.arn, aws_sqs_queue.ingesta.arn]
      },
    ]
  })
}

output "cola_documentos_url" {
  description = "URL de la cola de documentos"
  value       = aws_sqs_queue.documentos.url
}

output "cola_ingesta_url" {
  description = "URL de la cola de ingesta"
  value       = aws_sqs_queue.ingesta.url
}

output "lambda_worker_nombre" {
  description = "Nombre de la funcion Lambda worker"
  value       = aws_lambda_function.worker.function_name
}
