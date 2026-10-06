###############################################################################
# Base de datos PostgreSQL y sus credenciales
#
# Este archivo agrupa variables, recursos y salidas de la base de datos para no
# tocar los archivos que ya existen en la carpeta infra.
###############################################################################

###############################################################################
# Variables
###############################################################################

variable "db_nombre" {
  description = "Nombre de la base de datos inicial"
  type        = string
  default     = "malka"
}

variable "db_usuario_owner" {
  description = "Usuario dueno de la base de datos"
  type        = string
  default     = "malka_owner"
}

variable "db_clase_instancia" {
  description = "Clase de instancia de RDS. db.t4g.micro entra en la capa gratuita los primeros doce meses"
  type        = string
  default     = "db.t4g.micro"
}

variable "db_almacenamiento_gb" {
  description = "Almacenamiento inicial en GB"
  type        = number
  default     = 20
}


variable "habilitar_endpoint_secretos" {
  description = "Crea un endpoint de interfaz hacia Secrets Manager en una sola zona. Cuesta alrededor de 7 USD por mes y es lo que permite que la Lambda lea la credencial sin salida a internet"
  type        = bool
  default     = true
}

###############################################################################
# Credencial generada
###############################################################################

resource "random_password" "db_owner" {
  length  = 32
  special = true

  # RDS rechaza barra, arroba, comillas y espacios en la clave maestra.
  override_special = "!#%*()-_=+[]:?"
}

###############################################################################
# Ubicacion en la red
###############################################################################

resource "aws_db_subnet_group" "principal" {
  name       = "${local.name}-subredes-db"
  subnet_ids = aws_subnet.privada[*].id

  tags = {
    Name = "${local.name}-subredes-db"
  }
}

###############################################################################
# Parametros del motor
###############################################################################

resource "aws_db_parameter_group" "principal" {
  name        = "${local.name}-pg16"
  family      = "postgres16"
  description = "Parametros de PostgreSQL 16 para Malka Suite"

  # Obliga a que toda conexion viaje cifrada.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Deja registrada cualquier consulta que tarde mas de medio segundo.
  parameter {
    name         = "log_min_duration_statement"
    value        = "500"
    apply_method = "immediate"
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${local.name}-pg16"
  }
}

###############################################################################
# Instancia
###############################################################################

resource "aws_db_instance" "principal" {
  identifier = "${local.name}-db"

  engine                     = "postgres"
  engine_version             = "16"
  auto_minor_version_upgrade = true
  instance_class             = var.db_clase_instancia

  allocated_storage = var.db_almacenamiento_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_nombre
  username = var.db_usuario_owner
  password = random_password.db_owner.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.principal.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false

  parameter_group_name = aws_db_parameter_group.principal.name

  backup_retention_period = 1
  backup_window           = "07:00-08:00"
  maintenance_window      = "Mon:08:00-Mon:09:00"
  copy_tags_to_snapshot   = true

  enabled_cloudwatch_logs_exports = ["postgresql"]
  performance_insights_enabled    = false
  monitoring_interval             = 0

  # En un entorno de desarrollo conviene poder destruir y recrear sin trabas.
  # Para produccion esto se invierte.
  deletion_protection = false
  skip_final_snapshot = true
  apply_immediately   = true

  tags = {
    Name = "${local.name}-db"
  }
}

###############################################################################
# Credenciales en Secrets Manager
###############################################################################

resource "aws_secretsmanager_secret" "db_owner" {
  name        = "${local.name}/db/owner"
  description = "Credenciales del usuario dueno de la base de datos de Malka Suite"

  # Sin ventana de recuperacion, para poder recrear el secreto con el mismo
  # nombre si hay que rehacer el entorno.
  recovery_window_in_days = 0

  tags = {
    Name = "${local.name}-db-owner"
  }
}

resource "aws_secretsmanager_secret_version" "db_owner" {
  secret_id = aws_secretsmanager_secret.db_owner.id

  secret_string = jsonencode({
    username = aws_db_instance.principal.username
    password = random_password.db_owner.result
    engine   = "postgres"
    host     = aws_db_instance.principal.address
    port     = aws_db_instance.principal.port
    dbname   = aws_db_instance.principal.db_name
  })
}

###############################################################################
# Permiso de lectura para la Lambda
###############################################################################

data "aws_iam_policy_document" "lambda_secretos" {
  statement {
    sid       = "LeerCredencialDeBaseDeDatos"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_owner.arn]
  }
}

# La API solo puede leer el secreto del rol de aplicacion, nunca el del
# duenio: con el duenio, RLS se saltearia.
resource "aws_iam_role_policy" "lambda_secretos" {
  name   = "${local.name}-lectura-de-secretos"
  role   = aws_iam_role.lambda_api.id
  policy = data.aws_iam_policy_document.lambda_secreto_app.json
}

###############################################################################
# Salida hacia Secrets Manager sin internet
#
# Las subredes son privadas y no hay NAT Gateway, asi que la Lambda no puede
# resolver la API de Secrets Manager por internet. Este endpoint de interfaz
# resuelve ese llamado dentro de la VPC. Se crea en una sola subred para pagar
# una interfaz en lugar de dos; el trafico desde la otra zona llega igual.
###############################################################################

resource "aws_vpc_endpoint" "secretos" {
  count = var.habilitar_endpoint_secretos ? 1 : 0

  vpc_id              = aws_vpc.principal.id
  service_name        = "com.amazonaws.${var.region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.privada[0].id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name}-endpoint-secretos"
  }
}

###############################################################################
# Salidas
###############################################################################

output "db_endpoint" {
  description = "Direccion de la instancia, alcanzable solo desde dentro de la VPC"
  value       = aws_db_instance.principal.address
}

output "db_puerto" {
  description = "Puerto de la base de datos"
  value       = aws_db_instance.principal.port
}

output "db_nombre" {
  description = "Nombre de la base de datos inicial"
  value       = aws_db_instance.principal.db_name
}

output "db_secret_nombre" {
  description = "Nombre del secreto con las credenciales"
  value       = aws_secretsmanager_secret.db_owner.name
}

output "db_secret_arn" {
  description = "ARN del secreto con las credenciales"
  value       = aws_secretsmanager_secret.db_owner.arn
}
