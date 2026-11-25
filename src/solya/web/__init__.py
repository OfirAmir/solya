"""Web layer: OAuth connect/callback routes and the customer sign-up journey."""
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

from solya.web import journey, oauth

router = APIRouter()
router.include_router(oauth.router)
router.include_router(journey.router)

_FONTS_DIR = Path(__file__).resolve().parent.parent / "render" / "fonts"


def mount_static(app: FastAPI) -> None:
    """Serve the stylesheet and the bundled Hebrew font (the same file the card renderer
    uses), so pages load no external assets."""
    app.mount("/static", StaticFiles(directory=journey.STATIC_DIR), name="static")
    app.mount("/fonts", StaticFiles(directory=_FONTS_DIR), name="fonts")


__all__ = ["mount_static", "router"]
