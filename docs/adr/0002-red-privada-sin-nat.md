# ADR 0002 — Red privada sin NAT Gateway, con VPC endpoints

- **Estado:** aceptada
- **Fecha:** 2026-09-20
- **Decisores:** equipo Reinas Malka
- **Issues:** #4

## Contexto

La Lambda necesita alcanzar la base de datos y leer su contraseña de Secrets Manager. Para hablar con RDS tiene que estar dentro de la VPC, y una Lambda en VPC **pierde la salida a internet** salvo que exista una ruta explícita.

La solución de manual es un **NAT Gateway**, pero cuesta alrededor de **32 USD/mes** más el tráfico procesado, por cada AZ. Para un TP que debe vivir en el plan gratuito, ese solo recurso sería el gasto más grande del proyecto.

## Decisión

La VPC (`10.0.0.0/16`) tiene **solo subredes privadas, sin internet gateway y sin NAT Gateway**. El acceso a los servicios de AWS se resuelve con **VPC endpoints**:

- **S3**: endpoint tipo *gateway* — sin costo fijo.
- **Secrets Manager**: endpoint tipo *interface* — ~7,30 USD/mes.

Los security groups implementan el acceso mínimo: el SG de RDS acepta el puerto 5432 **únicamente desde el SG de la Lambda** (referencia entre grupos, no rangos de IP), y el SG de los endpoints acepta 443 solo desde el SG de la Lambda.

Se usan dos subredes en AZ distintas (`us-east-1a` y `us-east-1b`) porque el subnet group de RDS exige al menos dos zonas, incluso para una instancia single-AZ.

## Consecuencias

**A favor**

- Ahorro de aproximadamente **25 USD/mes** frente al NAT, con mejor postura de seguridad: el tráfico a AWS nunca sale a internet.
- Superficie de ataque mínima: ningún recurso de la aplicación es alcanzable desde internet salvo a través del API Gateway.
- Reglas de firewall que se leen como intenciones ("la base acepta a la Lambda") y no como direcciones IP.

**En contra**

- **Cada servicio nuevo de AWS que la Lambda necesite requiere agregar su endpoint**, y los de tipo interface cuestan ~7 USD/mes cada uno. Esto impacta directo en el Checkpoint 2: SQS y Bedrock necesitarán el suyo.
- La Lambda **no puede llamar a APIs públicas de internet**. Si en algún momento hiciera falta, habría que reevaluar esta decisión.
- Hay un costo fijo mensual que corre aunque el proyecto esté sin uso.

## Alternativas consideradas

**NAT Gateway.** Lo más flexible y el camino por defecto de la documentación de AWS. Descartada por costo: es entre 4 y 5 veces el gasto del endpoint de interface.

**Lambda fuera de la VPC.** Elimina el problema de raíz y mejora el cold start, pero obligaba a exponer la base públicamente. Descartada por seguridad: es exactamente lo que la materia pide no hacer.

**Instancia NAT en EC2 t4g.nano.** Más barata que el NAT administrado, pero nos volvía responsables de mantener y monitorear ese servidor, y se convierte en un punto único de falla. Descartada por complejidad operativa.
