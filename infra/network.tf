# ---------------------------------------------------------------------------
# Red privada de Malka Suite
#
# No hay NAT Gateway a proposito: cuesta alrededor de 64 USD por mes y no lo
# necesitamos. Las Lambdas no salen a internet, llegan a los servicios de AWS
# por VPC endpoints.
# ---------------------------------------------------------------------------

resource "aws_vpc" "principal" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${local.name}-vpc"
  }
}

# Dos subredes privadas en dos zonas distintas. RDS exige al menos dos zonas
# y Lambda reparte sus interfaces de red entre ambas.
resource "aws_subnet" "privada" {
  count = 2

  vpc_id            = aws_vpc.principal.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = data.aws_availability_zones.disponibles.names[count.index]

  tags = {
    Name = "${local.name}-privada-${count.index + 1}"
  }
}

# Tabla de ruteo sin salida a internet. Solo trafico interno de la VPC.
resource "aws_route_table" "privada" {
  vpc_id = aws_vpc.principal.id

  tags = {
    Name = "${local.name}-rt-privada"
  }
}

resource "aws_route_table_association" "privada" {
  count = length(aws_subnet.privada)

  subnet_id      = aws_subnet.privada[count.index].id
  route_table_id = aws_route_table.privada.id
}

# ---------------------------------------------------------------------------
# Grupos de seguridad
# ---------------------------------------------------------------------------

resource "aws_security_group" "lambda" {
  name        = "${local.name}-sg-lambda"
  description = "Trafico de las funciones Lambda"
  vpc_id      = aws_vpc.principal.id

  tags = {
    Name = "${local.name}-sg-lambda"
  }
}

resource "aws_vpc_security_group_egress_rule" "lambda_salida" {
  security_group_id = aws_security_group.lambda.id
  description       = "Salida hacia la base y los endpoints"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-sg-rds"
  description = "Base de datos PostgreSQL"
  vpc_id      = aws_vpc.principal.id

  tags = {
    Name = "${local.name}-sg-rds"
  }
}

# La base solo acepta conexiones de las Lambdas. No se abre a ninguna IP.
resource "aws_vpc_security_group_ingress_rule" "rds_desde_lambda" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL unicamente desde las Lambdas"
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.lambda.id
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-sg-endpoints"
  description = "VPC endpoints de tipo interfaz"
  vpc_id      = aws_vpc.principal.id

  tags = {
    Name = "${local.name}-sg-endpoints"
  }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_desde_lambda" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS desde las Lambdas"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.lambda.id
}

# ---------------------------------------------------------------------------
# VPC endpoints
# ---------------------------------------------------------------------------

# El de S3 es de tipo gateway: no cuesta nada y se resuelve por tabla de ruteo.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.principal.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.privada.id]

  tags = {
    Name = "${local.name}-vpce-s3"
  }
}

# Los de interfaz si cuestan (unos 7 USD por mes cada uno), asi que quedan
# apagados hasta que exista el worker que usa SQS y Bedrock.
locals {
  servicios_endpoint_interfaz = var.habilitar_endpoints_interfaz ? toset(["sqs", "bedrock-runtime"]) : toset([])
}

resource "aws_vpc_endpoint" "interfaz" {
  for_each = local.servicios_endpoint_interfaz

  vpc_id              = aws_vpc.principal.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.privada[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name}-vpce-${each.key}"
  }
}
