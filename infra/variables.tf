variable "project" {
  description = "Nombre del proyecto, se usa como prefijo de todos los recursos"
  type        = string
  default     = "malka-suite"
}

variable "environment" {
  description = "Ambiente de despliegue"
  type        = string
  default     = "dev"
}

variable "region" {
  description = "Region de AWS donde se crea todo"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "Rango de direcciones de la VPC"
  type        = string
  default     = "10.20.0.0/16"
}

variable "habilitar_endpoint_sqs" {
  description = "VPC endpoint de interface de SQS: lo usa la API para enviar mensajes (~7,30 USD/mes; se cobra por AZ)"
  type        = bool
  default     = true
}

variable "habilitar_endpoint_bedrock" {
  description = "VPC endpoint de interface de bedrock-runtime (~7,30 USD/mes). Apagado hasta que el worker haga llamadas reales a Bedrock (#40)"
  type        = bool
  default     = false
}

variable "presupuesto_mensual_usd" {
  description = "Tope del presupuesto mensual de AWS para la alarma de AWS Budgets"
  type        = number
  default     = 15
}

variable "email_alertas" {
  description = "Email que recibe las alertas de presupuesto y (a futuro) de las alarmas de DLQ. Vacio = no se crea el presupuesto"
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "Etiqueta de la imagen del backend publicada en ECR"
  type        = string
  default     = "latest"
}

# Origenes que pueden llamar a la API desde el navegador.
variable "origenes_permitidos" {
  description = "Lista de origenes habilitados para CORS"
  type        = list(string)
  default     = ["http://localhost:5173", "http://127.0.0.1:5173"]
}
