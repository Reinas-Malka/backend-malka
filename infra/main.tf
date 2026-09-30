locals {
  name = "${var.project}-${var.environment}"

  # Modelo de Bedrock verificado en el #39. El perfil us. enruta a los
  # modelos de fundacion de EE.UU., por eso el worker necesita los dos ARN.
  arns_modelo_bedrock = [
    "arn:aws:bedrock:${var.region}:::inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:${var.region}:::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
  ]
}

# Zonas de disponibilidad utilizables de la region, para repartir las subredes.
data "aws_availability_zones" "disponibles" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
