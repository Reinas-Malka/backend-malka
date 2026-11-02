locals {
  name = "${var.project}-${var.environment}"

  # Modelo de Bedrock verificado en el #39. El worker invoca el perfil de
  # inferencia us. (con la cuenta en el ARN) y el perfil reparte el pedido
  # entre los modelos de fundacion de las regiones de EE.UU.: el permiso
  # tiene que cubrir el modelo en las tres. Los modelos de fundacion no
  # llevan cuenta (arn:aws:bedrock:<region>::foundation-model/...).
  # Antes los dos ARN tenian un ":" de mas y no matcheaban nada (02/11).
  modelo_bedrock          = "anthropic.claude-haiku-4-5-20251001-v1:0"
  regiones_perfil_bedrock = ["us-east-1", "us-east-2", "us-west-2"]
  arns_modelo_bedrock = concat(
    ["arn:aws:bedrock:${var.region}:${data.aws_caller_identity.actual.account_id}:inference-profile/us.${local.modelo_bedrock}"],
    [for r in local.regiones_perfil_bedrock : "arn:aws:bedrock:${r}::foundation-model/${local.modelo_bedrock}"],
  )
}

# Zonas de disponibilidad utilizables de la region, para repartir las subredes.
data "aws_availability_zones" "disponibles" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
