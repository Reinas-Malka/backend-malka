###############################################################################
# Hosting del frontend (ADR 0011): bucket S3 privado + CloudFront con OAC
#
# Sin dominio propio: se usa el default xxxx.cloudfront.net (decision
# reversible; un alias con ACM despues no obliga a rehacer nada). El bucket
# nunca se abre al publico: lo sirve CloudFront con Origin Access Control.
# Se escribe y se mergea ANTES de aplicar: el apply llega cuando el login
# funcione (#6 del frontend).
###############################################################################

# La unica lista a mantener sincronizada: los origenes de CORS/callbacks
# pasan a incluir el dominio de CloudFront en cuanto exista.
data "aws_caller_identity" "actual" {}

locals {
  origenes_reales = concat(
    var.origenes_permitidos,
    ["https://${aws_cloudfront_distribution.frontend.domain_name}"],
  )

  frontend_bucket_nombre = "${local.name}-frontend-${data.aws_caller_identity.actual.account_id}"
}

resource "aws_s3_bucket" "frontend" {
  bucket = local.frontend_bucket_nombre

  tags = {
    Name = local.frontend_bucket_nombre
  }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# CloudFront es el unico que lee el bucket: con OAC, el acceso se firma
# desde el lado de CloudFront y la policy solo acepta esa distribucion.
resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = local.frontend_bucket_nombre
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "frontend_solo_cloudfront" {
  statement {
    sid       = "CloudFrontLeeElFrontend"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_solo_cloudfront.json
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled         = true
  comment         = "SPA de Malka Suite (ADR 0011)"
  price_class     = "PriceClass_100"
  is_ipv6_enabled = true

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # CachingOptimized
  }

  # SPA: las rutas las resuelve el router del navegador; sin esto, recargar
  # /documentos da 403 del bucket (no existe ese objeto).
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name = "${local.name}-frontend"
  }
}

output "frontend_url" {
  description = "URL publica del frontend (dominio default de CloudFront, ADR 0011)"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

# Para VITE_COGNITO_DOMAIN del build del frontend: el dominio completo del
# hosted UI, listo para usar.
output "cognito_dominio" {
  description = "Dominio del hosted UI de Cognito (publico, va al bundle del frontend)"
  value       = "${aws_cognito_user_pool_domain.principal.domain}.auth.${var.region}.amazoncognito.com"
}
