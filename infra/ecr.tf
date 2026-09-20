# Repositorio de imagenes del backend.
# La Lambda no corre un zip sino esta imagen, asi que el repositorio tiene que
# existir y tener al menos una imagen antes de crear la funcion.

resource "aws_ecr_repository" "backend" {
  name                 = "${local.name}-backend"
  image_tag_mutability = "MUTABLE"

  # Permite borrar el repositorio aunque tenga imagenes. Practico en un
  # proyecto de cursada, no lo dejariamos asi en produccion.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${local.name}-backend"
  }
}

# Sin esto el repositorio acumula todas las imagenes viejas y empieza a costar.
resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Conservar unicamente las ultimas 10 imagenes"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
