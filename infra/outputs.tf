output "vpc_id" {
  description = "Identificador de la VPC"
  value       = aws_vpc.principal.id
}

output "subredes_privadas" {
  description = "Subredes privadas donde corren las Lambdas y la base"
  value       = aws_subnet.privada[*].id
}

output "sg_lambda_id" {
  description = "Grupo de seguridad de las funciones Lambda"
  value       = aws_security_group.lambda.id
}

output "sg_rds_id" {
  description = "Grupo de seguridad de la base de datos"
  value       = aws_security_group.rds.id
}

output "sg_endpoints_id" {
  description = "Grupo de seguridad de los VPC endpoints de interfaz"
  value       = aws_security_group.endpoints.id
}

output "ecr_repository_url" {
  description = "Repositorio de imagenes del backend"
  value       = aws_ecr_repository.backend.repository_url
}

output "api_base_url" {
  description = "URL base publica de la API"
  value       = aws_apigatewayv2_api.principal.api_endpoint
}

output "health_url" {
  description = "URL de la prueba de vida, la que se usa en el smoke test"
  value       = "${aws_apigatewayv2_api.principal.api_endpoint}/health"
}

output "lambda_api_nombre" {
  description = "Nombre de la funcion Lambda que atiende la API"
  value       = aws_lambda_function.api.function_name
}
