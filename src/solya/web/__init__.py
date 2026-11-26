"""Web layer: OAuth connect/callback routes and the customer sign-up journey."""
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.staticfiles import StaticFiles

from solya.web import admin, journey, oauth, settings

router = APIRouter()
router.include_router(oauth.router)
router.include_router(journey.router)
router.include_router(admin.router)
router.include_router(settings.router)

_FONTS_DIR = Path(__file__).resolve().parent.parent / "render" / "fonts"


def mount_static(app: FastAPI) -> None:
    """Serve the stylesheet and the bundled Hebrew font (the same file the card renderer
    uses), so pages load no external assets."""
    app.mount("/static", StaticFiles(directory=journey.STATIC_DIR), name="static")
    app.mount("/fonts", StaticFiles(directory=_FONTS_DIR), name="fonts")


_ASSET_PREFIXES = ("/static/", "/fonts/")
PAGE_HEADERS = {
    # /ready carries the bearer link token and /sites the owner's sites: never cached
    "Cache-Control": "no-store",
    # no page may be framed (final review M10)
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def add_page_headers(app: FastAPI) -> None:
    """Every journey, OAuth and admin response is uncacheable and unframeable; the
    stylesheet and font stay cacheable."""

    @app.middleware("http")
    async def page_headers(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(_ASSET_PREFIXES):
            response.headers.update(PAGE_HEADERS)
        return response


__all__ = ["add_page_headers", "mount_static", "router"]
