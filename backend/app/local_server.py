"""Verified local transport for Phase 13 HTTP: python -m app.local_server.

No public bind, forwarded-peer middleware or deployment flag can enable this
capability. Do not expose this unauthenticated server through a reverse proxy.
"""
from ipaddress import ip_address
import signal
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


class LocalServer(uvicorn.Server):
    """Consume Uvicorn's SIGINT replay only after verified successful shutdown."""

    def __init__(self, config):
        super().__init__(config)
        self.shutdown_completed = False

    def _check_lifespan(self, phase):
        # Uvicorn logs ASGI failure messages but does not raise them to run().
        if any(getattr(self.lifespan, name, False) for name in
               ("startup_failed", "shutdown_failed", "error_occurred")):
            raise RuntimeError(f"Local ASGI lifespan {phase} failed; see Uvicorn logs")

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self._check_lifespan("startup")

    async def shutdown(self, sockets=None):
        await super().shutdown(sockets=sockets)
        self._check_lifespan("shutdown")
        self.shutdown_completed = self.started and not self.force_exit

    def run(self, sockets=None):
        try:
            return super().run(sockets=sockets)
        except KeyboardInterrupt:
            # 0.49 replays captured signals after shutdown. Never hide an early
            # interruption, forced exit, or a failure in startup/lifespan/shutdown.
            if not (self.shutdown_completed and self._captured_signals == [signal.SIGINT]):
                raise


def main():
    from app.main import create_app
    LocalServer(server_config(create_app())).run()


if __name__ == "__main__":
    main()
