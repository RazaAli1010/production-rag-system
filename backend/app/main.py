import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import auth, internal
from app.core.exceptions import AuthError

logger = structlog.get_logger(__name__)

app = FastAPI(title="CampusRAG")

app.include_router(auth.router)
app.include_router(internal.router)


@app.exception_handler(AuthError)
async def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    # The distinguishing detail goes to the log; the response body stays generic so the endpoint
    # is not an enumeration oracle.
    logger.info("auth.reject", reason=exc.reason, status=exc.status, path=request.url.path)
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=headers)
