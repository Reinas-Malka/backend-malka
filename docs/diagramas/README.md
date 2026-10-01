# Diagramas

| Archivo | Qué es |
|---|---|
| `arquitectura.png` | **Diagrama oficial** de arquitectura (versión curada, verificada contra `infra/` en octubre 2026). Es el que referencia el README del repo. |
| `pipeline.png` · `pipeline.pdf` | Flujo de deploy numerado: push a `main` → OIDC → ECR → migraciones → Lambdas → smoke test. |
| `arquitectura_generada.png` | Versión regenerable por script (ver abajo); se genera on demand, no se commitea. |

## Regenerar

Los scripts de esta carpeta redibujan los diagramas con la lib
[`diagrams`](https://diagrams.mingrammer.com/) (iconos oficiales de AWS) y
Graphviz:

```bash
pip install diagrams            # más el sistema: sudo apt install graphviz
python docs/diagramas/generar_arquitectura.py   # -> arquitectura_generada.png/pdf
python docs/diagramas/generar_pipeline.py       # -> pipeline.png/pdf
```

`generar_arquitectura.py` genera la **base regenerable**: cuando cambie la
infraestructura, correrlo, comparar contra `arquitectura.png` y actualizar la
versión oficial (curada) con los cambios. `generar_pipeline.py` sí regenera
directamente el diagrama oficial de deploy.
