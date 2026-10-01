"""Flujo de deploy continuo de Malka Suite (GitHub Actions + OIDC).

Complementa docs/diagramas/arquitectura: acá solo el camino que recorre
cada merge a main, paso por paso. Regenerar con
`python docs/diagramas/generar_pipeline.py`.
"""

from diagrams import Diagram, Edge
from diagrams.aws.compute import ECR, Lambda
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import APIGateway
from diagrams.aws.security import IAMRole
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.vcs import Github

GRAFICO = {
    "fontsize": "22",
    "bgcolor": "white",
    "pad": "0.4",
    "nodesep": "0.7",
    "ranksep": "0.9",
    "splines": "ortho",
}

PASO = lambda n: Edge(color="#2b6cb0", label=f"{n}")  # noqa: E731
FRENADO = Edge(color="#c0392b", style="bold", label="frena el deploy")

with Diagram(
    "Malka Suite — deploy continuo en cada merge a main",
    filename="docs/diagramas/pipeline",
    show=False,
    direction="LR",
    graph_attr=GRAFICO,
    outformat=["png", "pdf"],
):
    repo = Github("GitHub\npush a main")
    ci = GithubActions("CI en cada PR\nlint · mypy · pytest\nterraform · build")
    deploy = GithubActions("Deploy\n(.github/workflows/deploy.yml)")
    oidc = IAMRole("STS por OIDC\nrol mínimo privilegio\nsolo main · sin claves")
    ecr = ECR("ECR\nbuild + push\n:SHA y :latest")
    migraciones = Lambda("Lambda migraciones\nupgrade → head\nespera hasta 320 s")
    lambdas = Lambda("Lambda API + worker\nupdate-function-code\napuntando a :latest")
    smoke = APIGateway("Smoke test\nGET /health/ready\n5 reintentos")
    exito = Cloudwatch("Deploy verde\nresumen con commit\ny SHA de la imagen")

    repo >> PASO("1") >> deploy
    repo >> Edge(style="dotted", label="PR") >> ci
    deploy >> PASO("2") >> oidc
    oidc >> PASO("3") >> ecr
    ecr >> PASO("4") >> migraciones
    migraciones >> PASO("5") >> lambdas
    lambdas >> PASO("6") >> smoke
    smoke >> PASO("7") >> exito

    # Si la migración falla, la API queda con la versión anterior.
    migraciones >> FRENADO >> exito
