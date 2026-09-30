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
  description = "Crea los VPC endpoints de tipo interfaz (SQS y bedrock-runtime). Tienen costo por hora (~14,60 USD/mes entre los dos); se prenden con el worker del #37, que corre en la VPC y no tiene NAT"
  type        = bool
  default     = true
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
