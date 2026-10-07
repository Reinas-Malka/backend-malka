"""Validacion de los ID tokens de Cognito y roles (#21).

Se usa el mismo JWKS que produccion (app/jwks_cognito_dev.json) mas una clave
RSA de prueba: con la de prueba se firman los tokens validos, y con las reales
se prueba que una firma ajena no pasa. No hace falta AWS ni red.
"""

import io
import json
import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import auth, contexto, db
from app.auth import Identidad, identidad_actual, obtener_config_cognito, require_role
from app.errores import registrar_manejadores
from app.observabilidad import instalar_observabilidad

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PRUEBA"
CLIENT_ID = "cliente-spa-de-prueba"
KID = "clave-de-prueba"
TENANT = "11111111-1111-4111-8111-111111111111"

CLAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTRA_CLAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)

JWKS_PRODUCCION = json.loads(auth.RUTA_JWKS.read_text(encoding="utf-8"))
KID_REAL = JWKS_PRODUCCION["keys"][0]["kid"]


@pytest.fixture(autouse=True)
def cognito(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """El JWKS de produccion mas la clave de prueba, como si fuera del pool."""
    publica = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(CLAVE.public_key()))
    publica.update({"kid": KID, "alg": "RS256", "use": "sig"})
    claves = jwt.PyJWKSet.from_dict({"keys": [*JWKS_PRODUCCION["keys"], publica]})
    monkeypatch.setattr(auth, "CLAVES", claves)
    monkeypatch.setenv("COGNITO_ISSUER", ISSUER)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT_ID)
    obtener_config_cognito.cache_clear()
    yield
    obtener_config_cognito.cache_clear()


def emitir(
    clave: Any = CLAVE, algoritmo: str = "RS256", kid: str = KID, **cambios: Any
) -> str:
    """Un ID token como los de Cognito; cada test cambia lo que quiere probar."""
    ahora = int(time.time())
    claims: dict[str, Any] = {
        "sub": "usuario-1",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "iat": ahora,
        "exp": ahora + 3600,
        "custom:tenant_id": TENANT,
        "cognito:groups": ["admin"],
    }
    claims.update(cambios)
    claims = {nombre: valor for nombre, valor in claims.items() if valor is not None}
    return jwt.encode(claims, clave, algorithm=algoritmo, headers={"kid": kid})


def crear_app() -> FastAPI:
    app = FastAPI()
    registrar_manejadores(app)
    instalar_observabilidad(app)

    @app.get("/yo")
    def yo(identidad: Identidad = Depends(identidad_actual)) -> dict[str, Any]:
        return {
            "user_id": identidad.user_id,
            "tenant_id": str(identidad.tenant_id),
            "grupos": sorted(identidad.grupos),
        }

    @app.get("/contexto", dependencies=[Depends(identidad_actual)])
    def en_contexto() -> dict[str, Any]:
        return {
            "user_id": contexto.user_id_actual(),
            "tenant_id": contexto.tenant_id_actual(),
            "grupos": sorted(contexto.grupos_actuales()),
        }

    @app.get("/solo-admin", dependencies=[Depends(require_role("admin"))])
    def solo_admin() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def cliente() -> TestClient:
    return TestClient(crear_app())


def _con(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _codigo(respuesta: Any) -> str:
    return str(respuesta.json()["error"]["code"])


# DoD del #21: valido, expirado, de otra audiencia, firma alterada, kid
# desconocido y access token en lugar de ID token.


def test_token_valido(cliente: TestClient) -> None:
    respuesta = cliente.get("/yo", headers=_con(emitir()))

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "user_id": "usuario-1",
        "tenant_id": TENANT,
        "grupos": ["admin"],
    }


def test_token_expirado(cliente: TestClient) -> None:
    vencido = emitir(exp=int(time.time()) - 60)
    respuesta = cliente.get("/yo", headers=_con(vencido))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


def test_token_de_otra_audiencia(cliente: TestClient) -> None:
    respuesta = cliente.get("/yo", headers=_con(emitir(aud="otra-aplicacion")))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


def test_token_firmado_con_otra_clave(cliente: TestClient) -> None:
    respuesta = cliente.get("/yo", headers=_con(emitir(clave=OTRA_CLAVE)))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


def test_token_con_kid_real_y_firma_ajena(cliente: TestClient) -> None:
    """Dice venir de una clave del archivo, pero no la firmo Cognito."""
    respuesta = cliente.get("/yo", headers=_con(emitir(kid=KID_REAL)))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


def test_token_con_contenido_alterado(cliente: TestClient) -> None:
    """Se cambia el tenant del token y se deja la firma original."""
    encabezado, _, firma = emitir().split(".")
    _, otro_contenido, _ = emitir(**{"custom:tenant_id": str(uuid.uuid4())}).split(".")
    alterado = f"{encabezado}.{otro_contenido}.{firma}"
    respuesta = cliente.get("/yo", headers=_con(alterado))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


def test_kid_desconocido_pide_regenerar_el_jwks(cliente: TestClient) -> None:
    respuesta = cliente.get("/yo", headers=_con(emitir(kid="otra-clave")))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "clave_desconocida"
    assert "regenerar el JWKS" in respuesta.json()["error"]["message"]


def test_access_token_en_lugar_de_id_token(cliente: TestClient) -> None:
    respuesta = cliente.get("/yo", headers=_con(emitir(**{"token_use": "access"})))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


# Otros casos que tienen que dar 401.


def test_sin_token(cliente: TestClient) -> None:
    respuesta = cliente.get("/yo")

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_ausente"


@pytest.mark.parametrize(
    "token",
    [
        "no-es-un-jwt",
        emitir(iss="https://otro-emisor.example.com"),
        emitir(exp=None),
        # Confusion de algoritmo: HS256 firmado con un secreto cualquiera.
        emitir(clave=secrets.token_bytes(32), algoritmo="HS256"),
    ],
    ids=["basura", "otro_emisor", "sin_exp", "hs256"],
)
def test_tokens_invalidos(cliente: TestClient, token: str) -> None:
    respuesta = cliente.get("/yo", headers=_con(token))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "token_invalido"


@pytest.mark.parametrize("tenant", [None, "no-es-un-uuid"], ids=["ausente", "invalido"])
def test_token_sin_tenant_valido(cliente: TestClient, tenant: str | None) -> None:
    token = emitir(**{"custom:tenant_id": tenant})
    respuesta = cliente.get("/yo", headers=_con(token))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "tenant_ausente"


# Contexto del request.


def test_la_identidad_queda_en_el_contexto(cliente: TestClient) -> None:
    respuesta = cliente.get("/contexto", headers=_con(emitir()))

    assert respuesta.json() == {
        "user_id": "usuario-1",
        "tenant_id": TENANT,
        "grupos": ["admin"],
    }


# Roles.


def test_rol_suficiente(cliente: TestClient) -> None:
    respuesta = cliente.get("/solo-admin", headers=_con(emitir()))

    assert respuesta.status_code == 200


def test_rol_insuficiente_es_403(cliente: TestClient) -> None:
    token = emitir(**{"cognito:groups": ["lectura"]})
    respuesta = cliente.get("/solo-admin", headers=_con(token))

    assert respuesta.status_code == 403
    assert _codigo(respuesta) == "sin_permiso"
    assert respuesta.json()["error"]["details"] == {"roles_requeridos": ["admin"]}


def test_sin_grupos_es_403(cliente: TestClient) -> None:
    token = emitir(**{"cognito:groups": None})
    respuesta = cliente.get("/solo-admin", headers=_con(token))

    assert respuesta.status_code == 403


def test_token_invalido_en_ruta_con_rol_es_401(cliente: TestClient) -> None:
    respuesta = cliente.get("/solo-admin", headers=_con(emitir(clave=OTRA_CLAVE)))

    assert respuesta.status_code == 401


def test_require_role_rechaza_roles_inexistentes() -> None:
    with pytest.raises(ValueError, match="Roles invalidos"):
        require_role("admn")


# Archivo JWKS y configuracion.


def test_el_jwks_versionado_se_cargo_al_importar() -> None:
    kids = {clave["kid"] for clave in JWKS_PRODUCCION["keys"]}

    assert kids
    assert {clave["alg"] for clave in JWKS_PRODUCCION["keys"]} == {"RS256"}
    assert {clave.key_id for clave in auth.cargar_jwks(auth.RUTA_JWKS).keys} == kids


def test_sin_archivo_jwks_no_arranca() -> None:
    with pytest.raises(RuntimeError, match="No se pudo cargar el JWKS"):
        auth.cargar_jwks(Path("no-existe.json"))


def test_sin_configuracion_falla_como_error_interno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COGNITO_ISSUER")
    obtener_config_cognito.cache_clear()

    with pytest.raises(RuntimeError, match="Falta configurar Cognito"):
        obtener_config_cognito()


def test_el_token_no_aparece_en_los_logs(
    cliente: TestClient, logs: io.StringIO
) -> None:
    token = emitir()
    cliente.get("/yo", headers=_con(token))
    cliente.get("/yo", headers=_con(emitir(clave=OTRA_CLAVE)))

    salida = logs.getvalue()
    assert token not in salida
    assert TENANT in salida


# Sesion de base: el tenant del token llega a sesion_de_tenant.


def test_sin_tenant_no_se_abre_conexion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un token valido pero sin custom:tenant_id corta con 401 sin tocar la base."""

    def explotar() -> None:
        raise AssertionError("no tiene que abrir una conexion")

    monkeypatch.setattr(db, "obtener_motor", explotar)
    app = FastAPI()
    registrar_manejadores(app)

    @app.get("/razas")
    def razas(sesion: Session = Depends(db.obtener_sesion)) -> list[str]:
        return []

    token = emitir(**{"custom:tenant_id": None})
    respuesta = TestClient(app).get("/razas", headers=_con(token))

    assert respuesta.status_code == 401
    assert _codigo(respuesta) == "tenant_ausente"
