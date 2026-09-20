locals {
  name = "${var.project}-${var.environment}"
}

# Zonas de disponibilidad utilizables de la region, para repartir las subredes.
data "aws_availability_zones" "disponibles" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
