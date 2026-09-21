output "vpc_id" {
  description = "Identificador de la VPC"
  value       = aws_vpc.principal.id
}

output "subredes_privadas" {
  description = "Subredes privadas donde corren Lambda y RDS"
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
