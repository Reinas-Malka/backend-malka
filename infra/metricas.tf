###############################################################################
# Metricas, alarmas y dashboard (#44)
#
# Las metricas de negocio las escribe la app en Embedded Metric Format
# (app/metricas.py): una linea JSON en el log que CloudWatch convierte en
# metrica, sin permisos extra para las Lambdas. Namespace MalkaSuite, sin
# dimensiones. Cada linea lleva el request_id: de una metrica se llega al log
# de la operacion con la consulta "seguir-request-id" de abajo.
#
# Todas las alarmas avisan al topic de SNS, con la misma direccion que el
# presupuesto (email_alertas). La suscripcion por email hay que confirmarla
# una vez desde el correo que manda AWS.
###############################################################################

locals {
  namespace_app = "MalkaSuite"

  # Dimensiones de las metricas de API Gateway (HTTP API, sin metricas
  # detalladas por ruta: no tienen costo extra).
  dimensiones_api = { ApiId = aws_apigatewayv2_api.principal.id }
}

resource "aws_sns_topic" "alarmas" {
  name = "${local.name}-alarmas"

  tags = {
    Name = "${local.name}-alarmas"
  }
}

resource "aws_sns_topic_subscription" "alarmas_email" {
  topic_arn = aws_sns_topic.alarmas.arn
  protocol  = "email"
  endpoint  = var.email_alertas
}

###############################################################################
# Alarmas
###############################################################################

# 5xx sobre el 1% de los requests en 5 minutos. Con poco trafico un solo 5xx
# ya pasa el 1%: es a proposito, en dev cada 5xx se mira.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx-sobre-1-por-ciento"
  alarm_description   = "Mas del 1% de los requests de la API respondieron 5xx en 5 minutos"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
  ok_actions          = [aws_sns_topic.alarmas.arn]

  metric_query {
    id          = "porcentaje"
    expression  = "IF(requests > 0, 100 * errores / requests, 0)"
    label       = "Porcentaje de 5xx"
    return_data = true
  }

  metric_query {
    id = "errores"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "5xx"
      dimensions  = local.dimensiones_api
      stat        = "Sum"
      period      = 300
    }
  }

  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      dimensions  = local.dimensiones_api
      stat        = "Sum"
      period      = 300
    }
  }

  tags = {
    Name = "${local.name}-api-5xx-sobre-1-por-ciento"
  }
}

# p95 de latencia de la API. Tres periodos seguidos para no saltar por un
# arranque en frio aislado.
resource "aws_cloudwatch_metric_alarm" "api_latencia_p95" {
  alarm_name          = "${local.name}-api-latencia-p95"
  alarm_description   = "El p95 de latencia de la API paso ${var.latencia_p95_maxima_ms} ms durante 15 minutos"
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  dimensions          = local.dimensiones_api
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  threshold           = var.latencia_p95_maxima_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
  ok_actions          = [aws_sns_topic.alarmas.arn]

  tags = {
    Name = "${local.name}-api-latencia-p95"
  }
}

# Errores de conexion con RDS (base caida, red o credenciales), contados por
# la API, el chequeo de /health/ready y el worker. Un lock ocupado no cuenta.
resource "aws_cloudwatch_metric_alarm" "errores_conexion_rds" {
  alarm_name          = "${local.name}-errores-conexion-rds"
  alarm_description   = "La API o el worker no pudieron conectarse a RDS en los ultimos 5 minutos"
  namespace           = local.namespace_app
  metric_name         = "errores_conexion_rds"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarmas.arn]
  ok_actions          = [aws_sns_topic.alarmas.arn]

  tags = {
    Name = "${local.name}-errores-conexion-rds"
  }
}

# Las alarmas de DLQ estan en sqs.tf, junto a sus colas, y avisan al mismo
# topic.

###############################################################################
# Dashboard del flujo de documentos
###############################################################################

resource "aws_cloudwatch_dashboard" "documentos" {
  dashboard_name = "${local.name}-documentos"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Documentos: generados y rechazados por validacion"
          region  = var.region
          view    = "timeSeries"
          stacked = false
          stat    = "Sum"
          period  = 300
          metrics = [
            [local.namespace_app, "documentos_generados", { label = "Generados" }],
            [local.namespace_app, "documentos_rechazados_por_validacion", { label = "Rechazados por validacion" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Colas de documentos: pendientes y DLQ"
          region = var.region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 60
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.documentos.name, { label = "Pendientes" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.documentos_dlq.name, { label = "En la DLQ" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Bedrock: latencia"
          region = var.region
          view   = "timeSeries"
          period = 300
          metrics = [
            [local.namespace_app, "bedrock_latency_ms", { stat = "p95", label = "p95" }],
            [local.namespace_app, "bedrock_latency_ms", { stat = "Average", label = "Promedio" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Bedrock: tokens (entrada + salida)"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            [local.namespace_app, "bedrock_tokens", { label = "Tokens" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Rechazos de negocio"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            [local.namespace_app, "reservas_rechazadas_por_stock", { label = "Reservas sin stock" }],
            [local.namespace_app, "transiciones_invalidas", { label = "Transiciones invalidas" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Salud: 5xx de la API y conexion a RDS"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/ApiGateway", "5xx", "ApiId", aws_apigatewayv2_api.principal.id, { label = "5xx de la API" }],
            [local.namespace_app, "errores_conexion_rds", { label = "Errores de conexion a RDS" }],
          ]
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 18
        width  = 24
        height = 3
        properties = {
          title = "Alarmas"
          alarms = [
            aws_cloudwatch_metric_alarm.api_5xx.arn,
            aws_cloudwatch_metric_alarm.api_latencia_p95.arn,
            aws_cloudwatch_metric_alarm.errores_conexion_rds.arn,
            aws_cloudwatch_metric_alarm.dlq_documentos.arn,
            aws_cloudwatch_metric_alarm.dlq_ingesta.arn,
          ]
        }
      },
    ]
  })
}

###############################################################################
# Seguir una operacion por request_id
###############################################################################

# En CloudWatch Logs Insights, "seguir-request-id": se reemplaza el id y
# devuelve, en orden, la linea de acceso de la API, los logs del worker y
# las metricas EMF de esa operacion.
resource "aws_cloudwatch_query_definition" "seguir_request_id" {
  name = "${local.name}/seguir-request-id"

  log_group_names = [
    aws_cloudwatch_log_group.lambda_api.name,
    aws_cloudwatch_log_group.lambda_worker.name,
  ]

  query_string = <<-EOT
    fields @timestamp, @log, mensaje, route, status, latency_ms, @message
    | filter request_id = "REEMPLAZAR" or @message like "request_id=REEMPLAZAR"
    | sort @timestamp asc
  EOT
}
