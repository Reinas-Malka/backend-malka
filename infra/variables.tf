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

variable "habilitar_endpoints_interfaz" {
  description = "Crea los VPC endpoints de tipo interfaz. Tienen costo por hora, por eso estan apagados"
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Etiqueta de la imagen del backend publicada en ECR"
  type        = string
  default     = "latest"
}
