variable "project" {
  description = "Nombre del proyecto, se usa como prefijo de los recursos"
  type        = string
  default     = "malka-suite"
}

variable "environment" {
  description = "Ambiente de despliegue"
  type        = string
  default     = "dev"
}

variable "region" {
  description = "Region de AWS"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "Rango de direcciones de la VPC"
  type        = string
  default     = "10.20.0.0/16"
}

variable "habilitar_endpoints_interfaz" {
  description = "Crea los VPC endpoints de tipo interfaz para SQS y Bedrock. Cuestan alrededor de 7 USD por mes cada uno, por eso arrancan apagados hasta que exista el worker."
  type        = bool
  default     = false
}
