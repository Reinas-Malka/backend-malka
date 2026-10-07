"""Autenticacion con los ID tokens de Cognito (#21).

El frontend manda el ID token en `Authorization: Bearer` (ver docs/cognito.md).
Aca se verifica la firma (RS256, con las claves publicas del user pool), el
emisor, la audiencia (el app client de la SPA), el vencimiento y que sea un ID
token y no un access token.

Las claves publicas (JWKS) no se descargan: la Lambda corre sin salida a
internet (ADR 0002). Estan versionadas en app/jwks_cognito_dev.json, viajan en
la imagen y se cargan una sola vez, al importar este modulo (ADR 0010).

El token nunca se loguea ni se devuelve en un error.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import contexto
from app.errores import NoAutenticadoError, SinPermisoError

CLAIM_TENANT = "custom:tenant_id"
CLAIM_GRUPOS = "cognito:groups"
CLAIM_USO = "token_use"
# Cognito marca sus ID tokens con token_use = "id" (los access tokens, "access").
USO_ESPERADO = "id"

# Grupos de Cognito (infra/cognito.tf). require_role rechaza cualquier otro
# nombre para que un error de tipeo no deje una ruta sin nadie que la use.
ROLES = frozenset({"admin", "produccion", "ventas", "lectura"})

# COGNITO_JWKS es la ruta al archivo, no una URL. Por defecto, el de dev.
JWKS_POR_DEFECTO = Path(__file__).resolve().parent / "jwks_cognito_dev.json"
RUTA_JWKS = Path(os.getenv("COGNITO_JWKS", str(JWKS_POR_DEFECTO)))

# auto_error=False: la falta de token se responde con la forma unica de
# errores (401), no con el 403 propio de FastAPI.
_bearer = HTTPBearer(auto_error=False)

logger = logging.getLogger("malka.auth")


def cargar_jwks(ruta: Path) -> jwt.PyJWKSet:
    """Lee el JWKS versionado. Si falta o esta roto, la Lambda no arranca."""
    try:
        return jwt.PyJWKSet.from_dict(json.loads(ruta.read_text(encoding="utf-8")))
    except (OSError, ValueError, jwt.PyJWKSetError) as error:
        raise RuntimeError(
            f"No se pudo cargar el JWKS de Cognito desde {ruta} (ver ADR 0010)."
        ) from error


# Una vez por contenedor, al importar: ningun request lee el archivo.
CLAVES = cargar_jwks(RUTA_JWKS)


@dataclass(frozen=True)
class ConfigCognito:
    """Lo necesario para validar tokens."""

    issuer: str
    client_id: str
    claves: jwt.PyJWKSet


@dataclass(frozen=True)
class Identidad:
    """Quien hace el request, segun un token ya validado."""

    user_id: str
    tenant_id: UUID
    grupos: frozenset[str]


@lru_cache(maxsize=1)
def obtener_config_cognito() -> ConfigCognito:
    """Emisor y app client (los carga Terraform) junto con las claves.

    Si faltan, falla con 500 y no con 401: es un error de despliegue y no se
    tiene que confundir con un token invalido.
    """
    try:
        return ConfigCognito(
            issuer=os.environ["COGNITO_ISSUER"],
            client_id=os.environ["COGNITO_CLIENT_ID"],
            claves=CLAVES,
        )
    except KeyError as error:
        raise RuntimeError(
            "Falta configurar Cognito: COGNITO_ISSUER y COGNITO_CLIENT_ID."
        ) from error


def validar_token(token: str, config: ConfigCognito) -> dict[str, Any]:
    """Devuelve los claims si es un ID token valido de este user pool."""
    try:
        kid = str(jwt.get_unverified_header(token).get("kid"))
    except jwt.InvalidTokenError as error:
        raise NoAutenticadoError(
            "El token no es valido.", code="token_invalido"
        ) from error

    try:
        clave = config.claves[kid]
    except KeyError:
        # Si es un token real, el user pool cambio de claves: hay que
        # regenerar el archivo (ADR 0010). El kid es publico, se puede loguear.
        logger.warning("token firmado con un kid que no esta en el JWKS: %s", kid)
        raise NoAutenticadoError(
            "El token esta firmado con una clave que la API no conoce: hay que "
            "regenerar el JWKS versionado (ADR 0010).",
            code="clave_desconocida",
        ) from None

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            clave.key,
            # Fijo: evita que un token pida "none" o HS256 con la clave publica.
            algorithms=["RS256"],
            audience=config.client_id,
            issuer=config.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "token_use"]},
        )
    except jwt.InvalidTokenError as error:
        raise NoAutenticadoError(
            "El token no es valido.", code="token_invalido"
        ) from error

    # El tenant_id viaja en custom:tenant_id, que solo trae el ID token.
    if claims[CLAIM_USO] != USO_ESPERADO:
        raise NoAutenticadoError(
            "Se esperaba el ID token de Cognito.", code="token_invalido"
        )
    return claims


def _grupos(claims: dict[str, Any]) -> frozenset[str]:
    grupos = claims.get(CLAIM_GRUPOS, [])
    if not isinstance(grupos, list):
        return frozenset()
    return frozenset(grupo for grupo in grupos if isinstance(grupo, str))


async def identidad_actual(
    request: Request,
    credenciales: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Identidad:
    """Dependencia base de toda ruta protegida: 401 si el token no sirve.

    Es async a proposito: asi corre en el mismo contexto que el endpoint y las
    variables de app/contexto.py llegan hasta el (una dependencia sync corre
    en otro hilo y sus cambios de contexto se pierden).
    """
    if credenciales is None:
        raise NoAutenticadoError("Falta el token de acceso.", code="token_ausente")

    claims = validar_token(credenciales.credentials, obtener_config_cognito())
    try:
        tenant_id = UUID(str(claims[CLAIM_TENANT]))
    except (KeyError, ValueError):
        # Sin tenant no hay a que criadero filtrar: se corta antes de la base.
        raise NoAutenticadoError(
            "El token no indica a que criadero pertenece el usuario.",
            code="tenant_ausente",
        ) from None

    identidad = Identidad(
        user_id=str(claims["sub"]), tenant_id=tenant_id, grupos=_grupos(claims)
    )
    contexto.tenant_id.set(str(identidad.tenant_id))
    contexto.user_id.set(identidad.user_id)
    contexto.grupos.set(identidad.grupos)
    # El log de acceso corre en el middleware, fuera de este contexto: lee
    # request.state (app/observabilidad.py).
    request.state.tenant_id = str(identidad.tenant_id)
    request.state.user_id = identidad.user_id
    return identidad


def require_role(*roles: str) -> Callable[..., Identidad]:
    """Dependencia que exige al menos uno de los roles indicados (403 si no).

    Se declara en el router, no con ifs dentro de cada endpoint:

        APIRouter(dependencies=[Depends(require_role("admin", "produccion"))])
    """
    permitidos = frozenset(roles)
    if not permitidos or not permitidos <= ROLES:
        raise ValueError(f"Roles invalidos: {sorted(permitidos - ROLES)}")

    def _verificar(identidad: Identidad = Depends(identidad_actual)) -> Identidad:
        if not identidad.grupos & permitidos:
            raise SinPermisoError(
                "Tu rol no permite esta operacion.",
                details={"roles_requeridos": sorted(permitidos)},
            )
        return identidad

    return _verificar
