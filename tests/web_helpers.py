from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def proxied_client(app, trusted: list[str], client_addr: str) -> TestClient:
    """TestClient whose requests come from `client_addr` through uvicorn's
    ProxyHeadersMiddleware (the deployed path), trusting `trusted` peers."""
    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts=trusted), client=(client_addr, 50000))
