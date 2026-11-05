# Grupo malka-devs: acceso de lectura de los integrantes que no aplican
# Terraform (#103, ADR 0015). Se creo a mano el 01/10; los bloques import
# lo traen al state sin recrearlo. Los applies siguen siendo solo desde main
# y con la cuenta del duenio (ADR 0007).

locals {
  bucket_tfstate = "malka-suite-tfstate-${data.aws_caller_identity.actual.account_id}"
}

import {
  to = aws_iam_group.devs
  id = "malka-devs"
}

import {
  to = aws_iam_group_policy_attachment.devs_lectura
  id = "malka-devs/arn:aws:iam::aws:policy/ReadOnlyAccess"
}

import {
  to = aws_iam_group_policy.devs_auto_gestion
  id = "malka-devs:auto-gestion"
}

import {
  to = aws_iam_group_policy.devs_tfstate
  id = "malka-devs:tfstate-escritura"
}

import {
  to = aws_iam_group_policy.devs_cognito
  id = "malka-devs:cognito-escritura"
}

resource "aws_iam_group" "devs" {
  name = "malka-devs"
}

resource "aws_iam_group_policy_attachment" "devs_lectura" {
  group      = aws_iam_group.devs.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# Cada uno gestiona solo su password, sus claves y su MFA.
resource "aws_iam_group_policy" "devs_auto_gestion" {
  name  = "auto-gestion"
  group = aws_iam_group.devs.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "iam:GetUser",
        "iam:ChangePassword",
        "iam:ListAccessKeys",
        "iam:CreateAccessKey",
        "iam:DeleteAccessKey",
        "iam:ListMFADevices",
        "iam:CreateVirtualMFADevice",
        "iam:EnableMFADevice",
        "iam:ResyncMFADevice",
      ]
      Resource = [
        "arn:aws:iam::${data.aws_caller_identity.actual.account_id}:user/$${aws:username}",
        "arn:aws:iam::${data.aws_caller_identity.actual.account_id}:mfa/$${aws:username}",
      ]
    }]
  })
}

# Antes: Put/Delete sobre dev/* (podian pisar el state). Ahora solo el
# lockfile, que `terraform plan` escribe con use_lockfile = true; el state
# se lee con ReadOnlyAccess y no se escribe.
resource "aws_iam_group_policy" "devs_tfstate" {
  name  = "tfstate-escritura"
  group = aws_iam_group.devs.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:DeleteObject"]
      Resource = "arn:aws:s3:::${local.bucket_tfstate}/dev/terraform.tfstate.tflock"
    }]
  })
}

# Antes: cognito-idp:* y cognito-identity:* sobre *. Ahora solo lo que usa
# infra/scripts/seed_cognito_users.sh (usuarios de prueba) y solo en el pool
# del proyecto. El pool y sus grupos los gestiona Terraform.
resource "aws_iam_group_policy" "devs_cognito" {
  name  = "cognito-escritura"
  group = aws_iam_group.devs.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:AdminUpdateUserAttributes",
        "cognito-idp:AdminGetUser",
      ]
      Resource = aws_cognito_user_pool.principal.arn
    }]
  })
}
