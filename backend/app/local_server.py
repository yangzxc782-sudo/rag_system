"""Verified local transport for Phase 13 HTTP: python -m app.local_server.

No public bind, forwarded-peer middleware or deployment flag can enable this
capability. Do not expose this unauthenticated server through a reverse proxy.
"""
from ipaddress import ip_address
from urllib.parse import urlsplit

import uvicorn

from app.api.v1.conversations import LOCAL_TRANSPORT


def loopback(value):
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


class LocalConversationTransport:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            headers = dict(scope.get("headers", []))
            client = scope.get("client")
            # Reject forwarding entirely. Host/Origin are additional browser
            # rebinding/CSRF defences, never the source of the local capability.
            forwarded = any(k == b"forwarded" or k.startswith(b"x-forwarded-") for k in headers)
            origin = headers.get(b"origin")
            try:
                host = urlsplit("//" + headers.get(b"host", b"").decode("latin1")).hostname or ""
                origin_host = urlsplit(origin.decode("latin1")).hostname if origin else None
            except ValueError:
                host, origin_host = "", ""
            allowed = bool(client and loopback(client[0]) and not forwarded
                           and (loopback(host) or host == "localhost")
                           and (not origin or origin_host == "localhost" or loopback(origin_host or "")))
            scope["conversation.local_transport"] = LOCAL_TRANSPORT
            scope["conversation.local_peer"] = allowed
        await self.app(scope, receive, send)


def server_config(app, *, port=8000):
    # Keep the wrapper inside a server whose socket peer cannot be rewritten.
    return uvicorn.Config(LocalConversationTransport(app), host="127.0.0.1", port=port,
                          proxy_headers=False, forwarded_allow_ips="", access_log=False)


def main():
    from app.main import create_app
    uvicorn.Server(server_config(create_app())).run()


if __name__ == "__main__":
    main()
