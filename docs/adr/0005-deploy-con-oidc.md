# ADR 0005 — Deploy continuo con GitHub Actions y OIDC

- **Estado:** aceptada
- **Fecha:** 2026-09-30
- **Decisores:** equipo Reinas Malka
- **Issues:** #9, #10, #53

## Contexto

Hasta ahora el deploy salía de la notebook de una sola persona, con credenciales de administrador locales: no era reproducible, no quedaba registro de quién desplegó qué y, sin esa máquina, nadie podía publicar. La cátedra pide CI/CD con GitHub Actions.

Para que GitHub publique en AWS necesita autenticarse. La forma habitual, un usuario IAM con *access keys* guardadas en los Secrets del repositorio, deja credenciales de larga duración que se filtran por un log o un fork y que en la práctica nadie rota. El repositorio además es público.

## Decisión

- **Integración continua (#9):** cada PR corre formato, lint, mypy estricto, tests, `terraform validate` y el build de la imagen. Los cuatro jobs son *required status checks* del ruleset de `main`: un PR en rojo no se puede mergear.
- **Despliegue continuo (#10):** cada merge a `main` construye la imagen, la sube a ECR con el SHA y `latest`, aplica las migraciones, actualiza la API y el worker, y corre un smoke test contra `/health/ready`.
- **Autenticación por OIDC:** AWS confía en los tokens que emite GitHub. El rol `malka-suite-dev-github-actions` solo acepta tokens con `sub = repo:Reinas-Malka@329292836/backend-malka@1375132426:ref:refs/heads/main`, y STS entrega credenciales temporales de una hora como máximo. *(Enmienda, PR #57: desde 2025 GitHub emite el `sub` con los IDs estables de org y repo además de los nombres — sin los IDs, la condición `StringEquals` nunca matcheaba y STS rechazaba con `Not authorized`. Los claims reales se verificaron con un workflow de debug.)*
- **Mínimo privilegio:** el rol puede subir imágenes a un repositorio de ECR, actualizar las Lambdas del proyecto e invocar la de migraciones. Nada de IAM, VPC, RDS ni Secrets Manager.
- **La infraestructura sigue siendo manual:** el pipeline no corre `terraform apply`.

## Consecuencias

**A favor**

- No existe ningún secreto de AWS que se pueda filtrar o haya que rotar.
- Un fork, otra rama u otro repositorio no pueden asumir el rol, aunque usen el mismo emisor.
- Cada deploy queda registrado en GitHub Actions y cada `AssumeRoleWithWebIdentity` en CloudTrail, con el repositorio y el commit.
- Cualquier integrante puede publicar mergeando un PR aprobado.
- Si la migración falla, el deploy se frena antes de tocar la API.

**En contra**

- El rol, el proveedor OIDC y los secrets se configuran a mano una vez: el pipeline no puede crear el rol con el que se autentica.
- El deploy solo se puede probar desde `main`, porque el rol no confía en otras ramas.
- CI y deploy corren en paralelo sobre el mismo push: la garantía de que se despliega código probado viene del ruleset, no del orden de los workflows.
- Las migraciones corren antes del código nuevo, así que cada una tiene que ser compatible con la versión anterior.
- `/health` todavía informa `latest` y no el SHA desplegado.

## Alternativas consideradas

**Access keys de un usuario IAM en los Secrets.** Lo más simple de configurar. Descartada por ser credenciales de larga duración en un repositorio público.

**`terraform apply` desde el pipeline.** Automatizaría también la infraestructura, pero el rol necesitaría permisos sobre IAM, con los que podría darse más permisos a sí mismo (escalamiento de privilegios). Queda para evaluar en el Checkpoint 2, con aprobación manual.

**Jenkins.** Necesita un servidor propio que mantener y exponer a internet para los webhooks, lo que contradice el ADR 0001. Su integración con OIDC es más débil (rol de instancia sin restricción por repo) o más compleja. Y la cátedra pide GitHub Actions.

**Actualizar las Lambdas apuntando al tag con el SHA.** Más explícito, pero Terraform vería la diferencia con su `image_uri` (`latest`) y propondría revertirla en el próximo `apply`.