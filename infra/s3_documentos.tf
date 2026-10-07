###############################################################################
# Bucket de documentos e ingesta (issue #38)
#
# El backend nunca recibe el binario por el request: la API firma URLs
# prefirmadas y el cliente sube o baja directo contra S3. Por eso el bucket
# no se restringe por VPC endpoint: el navegador del cliente llega por
# internet. La Lambda, en cambio, alcanza la API de S3 por el endpoint de
# gateway que ya existe en la VPC, que no tiene costo fijo.
###############################################################################

data "aws_caller_identity" "cuenta_documentos" {}

resource "aws_s3_bucket" "documentos" {
  bucket = "${local.name}-documentos-${data.aws_caller_identity.cuenta_documentos.account_id}"

  tags = {
    Name = "${local.name}-documentos"
  }
}

# Sin ACLs: el dueno del bucket es el unico dueno de los objetos.
resource "aws_s3_bucket_ownership_controls" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Nada publico, ni por ACL ni por politica.
resource "aws_s3_bucket_public_access_block" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Los documentos aprobados son evidencia contable: versionado activo para no
# perder un PDF por una sobreescritura.
resource "aws_s3_bucket_versioning" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

    bucket_key_enabled = true
  }
}

# Retencion confirmada por la clienta (#70, docs/dominio.md): cada plazo es
# una linea. Ademas de estas reglas, se limpian las subidas multiparte que
# quedaron a medias.
resource "aws_s3_bucket_lifecycle_configuration" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  rule {
    id     = "abortar-subidas-incompletas"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Comprobantes: 5 anos por prescripcion fiscal (Ley 11.683 art. 56),
  # con pasaje a GLACIER_IR a los 90 dias para abaratar el almacenamiento.
  rule {
    id     = "documentos-comprobantes"
    status = "Enabled"

    filter {
      prefix = "documentos/"
    }

    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }

    expiration {
      days = 1825
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # Ingesta: carpeta temporal de subida, 30 dias.
  rule {
    id     = "ingesta-temporal"
    status = "Enabled"

    filter {
      prefix = "ingesta/"
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # Evidencia fotografica de cria (prefijo de fase 1): 365 dias.
  rule {
    id     = "evidencia-de-cria"
    status = "Enabled"

    filter {
      prefix = "evidencia/"
    }

    expiration {
      days = 365
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# El PUT de la URL prefirmada lo hace el navegador, asi que el bucket necesita
# CORS con los mismos origenes que la API.
resource "aws_s3_bucket_cors_configuration" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  cors_rule {
    allowed_methods = ["GET", "PUT", "HEAD"]
    allowed_origins = local.origenes_reales
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 300
  }
}

data "aws_iam_policy_document" "documentos_bucket" {
  statement {
    sid    = "DenegarTraficoSinTLS"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.documentos.arn,
      "${aws_s3_bucket.documentos.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "documentos" {
  bucket = aws_s3_bucket.documentos.id
  policy = data.aws_iam_policy_document.documentos_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.documentos]
}

###############################################################################
# Permisos de la Lambda de API
#
# Una URL prefirmada hereda los permisos de quien la firma, asi que el rol
# necesita el permiso aunque el objeto lo mueva el navegador. Acotado a los
# dos prefijos de negocio: nada de s3:* sobre el bucket entero.
###############################################################################

data "aws_iam_policy_document" "lambda_documentos" {
  statement {
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload",
    ]

    resources = [
      "${aws_s3_bucket.documentos.arn}/documentos/*",
      "${aws_s3_bucket.documentos.arn}/ingesta/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_documentos" {
  name   = "${local.name}-documentos-en-s3"
  role   = aws_iam_role.lambda_api.id
  policy = data.aws_iam_policy_document.lambda_documentos.json
}

output "bucket_documentos" {
  description = "Nombre del bucket de documentos e ingesta"
  value       = aws_s3_bucket.documentos.bucket
}
