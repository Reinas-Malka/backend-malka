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
  # Desde 2025 GitHub emite el claim sub del token OIDC con los IDs estables
  # de org y repo ademas de los nombres, para que un renombre no rompa la
  # federacion. Verificado con un workflow de debug (run 36798383213): el
  # sub real es repo:Reinas-Malka@329292836/backend-malka@1375132426:ref:...
  # y la condicion StringEquals sin los IDs nunca matchea (STS responde
  # "Not authorized to perform sts:AssumeRoleWithWebIdentity").
  github_repositorio_sub = "Reinas-Malka@329292836/backend-malka@1375132426"
  github_rama_deploy     = "main"
}


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
    # Dos subs: el backend (imagen + lambdas) y el frontend (deploy del SPA).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${local.github_repositorio_sub}:ref:refs/heads/${local.github_rama_deploy}",
        "repo:Reinas-Malka@329292836/frontend-malka@1377758180:ref:refs/heads/main",
      ]
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
  # Las tres Lambdas que el pipeline actualiza, por referencia al recurso.
  lambdas_desplegables = [
    aws_lambda_function.api.arn,
    aws_lambda_function.worker.arn,
    aws_lambda_function.migraciones.arn,
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
    resources = [aws_lambda_function.migraciones.arn]
  }

  # El deploy del frontend (ADR 0011): publica el build e invalida la
  # cache de CloudFront para no servir el bundle viejo.
  statement {
    sid = "PublicarFrontend"
    actions = [
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.frontend.arn,
      "${aws_s3_bucket.frontend.arn}/*",
    ]
  }

  statement {
    sid       = "InvalidarCloudFront"
    actions   = ["cloudfront:CreateInvalidation"]
    resources = ["*"]
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
