from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.deps import access_claims, client_ip, get_current_user
from app.auth.schemas import Principal, RefreshRequest, RegisterRequest, TokenResponse, UserOut
from app.core.settings import settings
from app.db.models import User
from app.db.session import get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest, session: AsyncSession = Depends(get_session)) -> User:
    return await service.register(session, req, settings=settings)


@router.post("/token", response_model=TokenResponse)
async def token(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    access, refresh = await service.authenticate(
        session,
        form.username,
        form.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        settings=settings,
    )
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    req: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Rotation revokes the old family, so access tokens minted from it stop working here — by
    design, and safe because clients refresh at ~15min, when the access token expires anyway."""
    access, new_refresh = await service.rotate_refresh(
        session,
        req.refresh_token,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        settings=settings,
    )
    return TokenResponse(access_token=access, refresh_token=new_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    claims: dict = Depends(access_claims),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoking the family kills every access token minted from it, not just the refresh token.
    An API-key caller has no session to log out and is rejected by `access_claims`."""
    await service.revoke_family(session, claims["sid"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
async def me(
    principal: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    return await session.get(User, principal.user_id)
