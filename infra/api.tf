# API Gateway HTTP: la puerta de entrada publica del backend.
# Se usa HTTP API y no REST API porque cuesta la tercera parte y alcanza para
# lo que necesitamos (proxy a Lambda y validacion de JWT mas adelante).

resource "aws_apigatewayv2_api" "principal" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"
  description   = "API publica de Malka Suite"

  # API Gateway responde el preflight; no agregar CORSMiddleware en FastAPI.
  cors_configuration {
    allow_origins  = local.origenes_reales
    allow_methods  = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    allow_headers  = ["content-type", "authorization"]
    expose_headers = ["x-request-id"]
    max_age        = 3600
  }

  tags = {
    Name = "${local.name}-api"
  }
}

resource "aws_apigatewayv2_integration" "lambda_api" {
  api_id                 = aws_apigatewayv2_api.principal.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

# Prueba de vida. Queda sin autenticacion a proposito: la usan el monitoreo y
# el smoke test del pipeline.
resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.principal.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_api.id}"
}

resource "aws_apigatewayv2_route" "health_ready" {
  api_id    = aws_apigatewayv2_api.principal.id
  route_key = "GET /health/ready"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_api.id}"
}

# Catch-all de la version de la API: toda la logica de ruteo la decide
# FastAPI dentro de la Lambda; el gateway solo enruta /health explícitos
# (documentados arriba) y el resto del prefijo versionado. Sin esta ruta,
# cualquier endpoint nuevo (p. ej. /api/v1/clientes, #87) da 404 del gateway
# aunque el codigo y el deploy esten perfectos.
resource "aws_apigatewayv2_route" "api_v1" {
  api_id    = aws_apigatewayv2_api.principal.id
  route_key = "ANY /api/v1/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_api.id}"
}

# El auto-preflight del cors_configuration solo responde donde existe una
# ruta explicita para OPTIONS (ANY no lo cubre: verificado en vivo, 405).
# Con CORS configurado el gateway responde 204 solo, sin invocar la Lambda.
resource "aws_apigatewayv2_route" "api_v1_options" {
  api_id    = aws_apigatewayv2_api.principal.id
  route_key = "OPTIONS /api/v1/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.principal.id
  name        = "$default"
  auto_deploy = true

  # Techo de trafico para que un error o un abuso no dispare la factura.
  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 20
  }

  tags = {
    Name = "${local.name}-api-stage"
  }
}

# Sin esto API Gateway recibe un 403 al intentar invocar la funcion.
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowInvokeFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.principal.execution_arn}/*/*"
}
