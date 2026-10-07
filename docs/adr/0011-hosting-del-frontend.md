# ADR 0011 — Hosting del frontend: S3 privado + CloudFront con OAC

- **Estado:** aceptada
- **Fecha:** 2026-10-07
- **Decisores:** equipo Reinas Malka
- **Issues:** #18 del frontend (ADR de hosting), #7 (CI), deploy del front

## Contexto

La defensa es el 30/11 y hasta ahora el frontend solo existía en `localhost`.
Hacía falta hosting con HTTPS, sin dominio propio: una hosted zone de Route53
son 0,50 USD/mes fijos más el dominio y su propagación, con un margen de
presupuesto que no da para eso.

## Decisión

- **Bucket S3 privado** (Block Public Access completo, SSE) servido por
  **CloudFront con Origin Access Control**. Nada de website hosting del
  bucket ni abrirlo al público: solo la distribución firma el acceso.
- **Sin dominio propio**: se usa el default `xxxx.cloudfront.net`, que trae
  HTTPS gratis. **Decisión reversible**: sumar después un alias CNAME con
  certificado de ACM no obliga a rehacer nada (bucket, OAC y workflow son
  iguales; solo cambia el certificado y `origenes_permitidos`).
- **`PriceClass_100`**: la clienta y los evaluadores están en Argentina;
  cubre Norteamérica y Europa, que sobra para la demo.
- **Errores 403/404 → `/index.html` con 200**: sin eso, recargar una ruta
  del SPA rompe, porque el objeto no existe en el bucket.
- **El deploy del frontend usa el mismo rol OIDC** que el backend, con la
  confianza extendida al sub del repo `frontend-malka` (mismo formato con
  IDs estables del ADR 0005) y permisos nuevos: escritura en el bucket y
  `cloudfront:CreateInvalidation`.
- **Variables del build** (`VITE_*`): 5 nombres fijados por contrato en el
  issue #6 del frontend (`VITE_API_BASE_URL`, `VITE_COGNITO_USER_POOL_ID`,
  `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_DOMAIN`, `VITE_COGNITO_REGION`).
  Ninguna es secreta — terminan en el bundle de JavaScript por diseño — así
  que van como **variables del repo**, no como secrets. El redirect y el
  logout NO van como variables: el front los deriva de
  `window.location.origin`, así el mismo build anda en localhost y en
  CloudFront. La única lista sincronizada es `origenes_permitidos`
  (consumida por callbacks de Cognito, CORS de la API y CORS del bucket de
  documentos), que Terraform completa con el dominio de CloudFront.
- **Se mergea sin aplicar**: el apply llega cuando el login funcione (#6),
  para no pagar la distribución antes de tiempo.

## Consecuencias

- Costo: CloudFront entra en la capa gratuita (1 TB/mes y 10M requests); el
  bucket casi vacío cuesta centavos.
- Un solo lugar para agregar un origen (CORS + callbacks juntos).
- La invalidación en cada deploy evita servir el bundle viejo tras un
  `--delete` del sync.

## Alternativas consideradas

**Website hosting del bucket.** Gratuita pero sin HTTPS custom ni dominio:
descartada por mezcla de contenido activo y sin control de caché.

**Vercel/Netlify.** Más simple, pero saca el deploy del pipeline propio del
repo y agrega una cuenta externa más para la defensa. Descartada.
