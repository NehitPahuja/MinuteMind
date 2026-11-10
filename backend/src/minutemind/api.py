"""FastAPI application for the MinuteMind local service.

Every request must carry the per-launch session token that Electron generates,
so other programs on the machine cannot drive the service.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, status

from minutemind import __version__

TOKEN_HEADER = "X-MinuteMind-Token"


def create_app(token: str) -> FastAPI:
    if not token:
        raise ValueError("A non-empty session token is required")

    def require_token(
        x_minutemind_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    ) -> None:
        if x_minutemind_token is None or not secrets.compare_digest(
            x_minutemind_token, token
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    app = FastAPI(
        title="MinuteMind",
        version=__version__,
        dependencies=[Depends(require_token)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
