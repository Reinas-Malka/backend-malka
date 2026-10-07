###############################################################################
# Presupuesto mensual con alerta (issue de costos, auditoria del 01/10)
#
# AWS Budgets no tiene costo. Avisa por email al superar el 80% del gasto
# real y al pronosticar pasar el 100% del mes. email_alertas es obligatoria:
# sin ella terraform plan falla en vez de proponer destruir el presupuesto.
###############################################################################

resource "aws_budgets_budget" "mensual" {
  name              = "${local.name}-mensual"
  budget_type       = "COST"
  limit_amount      = var.presupuesto_mensual_usd
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2026-10-01_00:00"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold_type             = "PERCENTAGE"
    threshold                  = 80
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.email_alertas]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold_type             = "PERCENTAGE"
    threshold                  = 100
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.email_alertas]
  }

  tags = {
    Name = "${local.name}-presupuesto-mensual"
  }
}
