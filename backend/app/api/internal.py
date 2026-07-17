from fastapi import APIRouter, Depends

from app.auth.deps import require_role

# Router-level guard, so every endpoint F13 hangs here (stats, cache flush, doc status, eval
# results) is admin-only by default rather than by remembering.
router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get("/ping")
async def ping() -> dict:
    return {"ok": True}
