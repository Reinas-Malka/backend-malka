###############################################################################
# Deploy desde GitHub Actions con OIDC (#10)
#
# GitHub no guarda ninguna clave de AWS. En cada corrida, el workflow pide a
# GitHub un token OIDC firmado y se lo presenta a STS, que devuelve credenciales
# temporales (una hora como maximo) SOLO si el token viene de este repositorio
# y de la rama main. Ver docs/adr/0005-deploy-con-oidc.md.
#
# Este archivo se aplica a mano, una vez: el pipeline no puede crear el rol que
# necesita para autenticarse.
###############################################################################

locals {
  github_repositorio = "Reinas-Malka/backend-malka"
  github_rama_deploy = "main"

  # Lambdas que el pipeline actualiza. Migraciones (#53) se referencia por
  # nombre porque todavia no existe; el permiso queda listo para cuando entre.
  lambda_migraciones_nombre = "${local.name}-migraciones"
}

data "aws_caller_identity" "cuenta_oidc" {}

###############################################################################
# Proveedor de identidad
#
# Solo puede existir uno por URL en la cuenta. Si ya se creo a mano, importarlo:
#   terraform import aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<cuenta>:oidc-provider/token.actions.githubusercontent.com
###############################################################################

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS ya valida los tokens de GitHub contra su propia lista de autoridades y
  # no usa estas huellas, pero el provider de Terraform las pide en algunas
  # versiones. Son las publicadas por GitHub.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]

  tags = {
    Name = "${local.name}-github-oidc"
  }
}

###############################################################################
# Rol que asume el workflow
###############################################################################

data "aws_iam_policy_document" "github_confianza" {
  statement {
    sid     = "SoloMainDeEsteRepositorio"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # La condicion que da la seguridad: el emisor es el mismo para TODOS los
    # repositorios de GitHub; sin esto, cualquiera podria asumir el rol.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${local.github_repositorio}:ref:refs/heads/${local.github_rama_deploy}"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name                 = "${local.name}-github-actions"
  description          = "Rol del workflow de deploy de GitHub Actions (OIDC, solo main)"
  assume_role_policy   = data.aws_iam_policy_document.github_confianza.json
  max_session_duration = 3600

  tags = {
    Name = "${local.name}-github-actions"
  }
}

###############################################################################
# Permisos: publicar la imagen y actualizar las Lambdas. Nada mas.
#
# Sin permisos sobre IAM, VPC, RDS ni Secrets Manager: el pipeline despliega
# codigo, no infraestructura (terraform apply sigue siendo manual).
###############################################################################

locals {
  arn_lambda_base = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.cuenta_oidc.account_id}:function"

  lambdas_desplegables = [
    aws_lambda_function.api.arn,
    aws_lambda_function.worker.arn,
    "${local.arn_lambda_base}:${local.lambda_migraciones_nombre}",
  ]
}

data "aws_iam_policy_document" "github_deploy" {
  # Este permiso no admite un recurso especifico: devuelve el token de login.
  statement {
    sid       = "LoginEcr"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PublicarImagen"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.backend.arn]
  }

  statement {
    sid = "ActualizarLambdas"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
    ]
    resources = local.lambdas_desplegables
  }

  # Solo la de migraciones se invoca desde el pipeline.
  statement {
    sid       = "InvocarMigraciones"
    actions   = ["lambda:InvokeFunction"]
    resources = ["${local.arn_lambda_base}:${local.lambda_migraciones_nombre}"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "${local.name}-github-deploy"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

###############################################################################
# Salida: va al secret AWS_ROLE_ARN del repositorio
###############################################################################

output "github_actions_role_arn" {
  description = "ARN del rol que asume el workflow de deploy; va en el secret AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions.arn
}
