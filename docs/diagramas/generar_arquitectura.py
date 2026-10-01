"""Base regenerable del diagrama de arquitectura de Malka Suite sobre AWS.

El diagrama OFICIAL para el README y la entrega es `arquitectura.png`
(versión curada, verificada contra infra/). Este script genera
`arquitectura_generada.png/pdf` con la lib `diagrams` (iconos oficiales de
AWS + Graphviz) y sirve de base cuando la infra cambia y hay que redibujar:

    python docs/diagramas/generar_arquitectura.py

Se organiza en zonas: el pipeline de CI/CD a la izquierda, el flujo de la
aplicación en el centro (dentro de la VPC), y los secretos y el
almacenamiento a la derecha. El deploy paso a paso está en pipeline.png.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import ECR, Lambda
from diagrams.aws.database import RDS
from diagrams.aws.integration import SQS
from diagrams.aws.management import Cloudwatch, CloudwatchAlarm
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway
from diagrams.aws.security import Cognito, IAMRole, SecretsManager
from diagrams.aws.storage import S3
from diagrams.generic.blank import Blank
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import Users
from diagrams.onprem.vcs import Github

GRAFICO = {
    "fontsize": "24",
    "bgcolor": "white",
    "pad": "0.4",
    "nodesep": "0.7",
    "ranksep": "0.9",
    "splines": "spline",
}

FLUJO = Edge(color="#2b6cb0")
PREFIRMADA = Edge(color="#dd6b20", fontcolor="#dd6b20")
PLANEADO = Edge(color="#9b59b6", style="dashed", fontcolor="#7d3c98")
FALLA = Edge(color="#c0392b", style="dashed", fontcolor="#c0392b")
SUAVE = Edge(color="#8a8a8a", style="dotted")

with Diagram(
    "Malka Suite — arquitectura en AWS",
    filename="docs/diagramas/arquitectura_generada",
    show=False,
    direction="TB",
    graph_attr=GRAFICO,
    outformat=["png", "pdf"],
):
    # ============================================================ ZONA CI/CD ===
    with Cluster("CI/CD — deploy automático (ADR 0005)"):
        github = Github("GitHub\npush a main")
        actions = GithubActions("GitHub Actions\nci.yml · deploy.yml")
        rol_deploy = IAMRole("Rol de deploy\nOIDC · solo main\nsin claves")
        ecr = ECR("ECR\nmalka-suite-dev-backend")

    # ====================================================== ZONA APLICACIÓN ====
    navegador = Users("Navegador\nde los clientes")
    cognito = Cognito("Cognito\n(#20 planeado)")
    apigw = APIGateway("API Gateway\nCORS · 20 rps / 50 burst")

    with Cluster("VPC 10.20.0.0/16 — sin NAT Gateway ni salida a internet (ADR 0002)"):
        with Cluster("Subredes privadas · 2 AZs"):
            api = Lambda("Lambda API\nFastAPI + Mangum")
            worker = Lambda("Lambda worker\nasync · batch 1")

        with Cluster("VPC endpoints — salida sin NAT"):
            ep_s3 = S3("S3\n(gateway, gratis)")
            ep_sqs = SQS("SQS")
            ep_bedrock = Bedrock("bedrock-\nruntime")
            ep_secretos = SecretsManager("Secrets\nManager")

        sqs = SQS("SQS documentos · ingesta\nvisibility 360 s")
        dlq = SQS("DLQ (3 fallos)\nretención 14 días")
        alarma = CloudwatchAlarm("Alarma si\nla DLQ tiene mensajes")

    # ================================================= ZONA DATOS Y SECRETOS ===
    with Cluster("Datos y gestión de secretos"):
        secretos = SecretsManager("Secrets Manager\ndb/owner\n(gestor de contraseñas)")
        rds = RDS("RDS PostgreSQL 16\nforce SSL · RLS (#19)")
        bucket = S3("S3 documentos\nversionado · SSE (#38)")

    bedrock = Bedrock("Amazon Bedrock\nClaude Haiku 4.5 · temp 0 (#40)")
    logs = Cloudwatch("CloudWatch\n+ X-Ray")

    # --- zona CI/CD: del push a la imagen que corre todo ---
    github >> FLUJO >> actions >> FLUJO >> rol_deploy >> FLUJO >> ecr
    ecr >> SUAVE >> api
    ecr >> SUAVE >> worker

    # --- zona aplicación: pedido sincrono ---
    navegador >> Edge(color="#2b6cb0", label="HTTPS") >> apigw
    navegador >> PLANEADO >> cognito >> PLANEADO >> apigw
    apigw >> FLUJO >> api

    # --- camino asincronico ---
    api >> Edge(color="#2b6cb0", label="encola") >> sqs
    sqs >> Edge(color="#2b6cb0", label="event source") >> worker
    (
        sqs
        >> Edge(color="#c0392b", style="dashed", label="si falla 3 veces")
        >> dlq
        >> FLUJO
        >> alarma
    )

    # --- el binario nunca pasa por la API ---
    navegador >> Edge(color="#dd6b20", label="sube/baja por\nURL prefirmada") >> bucket
    api >> Edge(color="#dd6b20", label="firma URLs") >> ep_s3 >> SUAVE >> bucket

    # --- el worker consulta y redacta ---
    worker >> Edge(color="#2b6cb0", label="contexto") >> rds
    (
        worker
        >> Edge(color="#2b6cb0", label="converse · salida\nvalidada (#40)")
        >> ep_bedrock
        >> Edge(color="#2b6cb0", minlen="2")
        >> bedrock
    )
    api >> SUAVE >> ep_sqs >> SUAVE >> sqs

    # --- secretos: credenciales de la base, nunca en codigo ---
    api >> SUAVE >> ep_secretos >> FLUJO >> secretos
    secretos >> Edge(color="#2b6cb0", label="credenciales\ndel owner") >> rds
    worker >> SUAVE >> logs
    api >> SUAVE >> logs

    # ================================================================ LEYENDA ==
    with Cluster("Leyenda"):
        a1, a2 = Blank("·"), Blank("·")
        a1 >> Edge(color="#2b6cb0", label="flujo") >> a2
        a3, a4 = Blank("·"), Blank("·")
        a3 >> Edge(color="#dd6b20", label="URL prefirmada") >> a4
        a5, a6 = Blank("·"), Blank("·")
        a5 >> PLANEADO >> a6
        a7, a8 = Blank("·"), Blank("·")
        a7 >> FALLA >> a8
        a9, a10 = Blank("·"), Blank("·")
        a9 >> SUAVE >> a10
