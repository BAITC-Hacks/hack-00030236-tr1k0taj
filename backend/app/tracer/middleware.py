"""Чистый ASGI middleware: SERVER span на каждый HTTP-запрос.

Не BaseHTTPMiddleware — чтобы SSE шёл потоком. Span закрывается после последнего куска тела
(`more_body=False`), при обрыве клиента или исключении, а не при возврате обработчика.
"""

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.tracer.setup import get_tracer

_propagator = TraceContextTextMapPropagator()


def traceparent(ctx: trace.SpanContext) -> str:
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


class TraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        carrier = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method, path = scope["method"], scope["path"]
        span = get_tracer().start_span(
            f"{method} {path}", context=_propagator.extract(carrier), kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": path},
        )
        ctx = span.get_span_context()
        ended = False

        def finish() -> None:
            nonlocal ended
            if not ended:
                ended = True
                route = scope.get("route")
                template = getattr(route, "path", None)
                if template:
                    span.set_attribute("http.route", template)
                    span.update_name(f"{method} {template}")
                span.end()

        async def traced_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                if ctx.is_valid:
                    headers = list(message.get("headers", []))
                    headers.append((b"traceparent", traceparent(ctx).encode()))
                    headers.append((b"x-trace-id", f"{ctx.trace_id:032x}".encode()))
                    message = {**message, "headers": headers}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finish()

        token = otel_context.attach(trace.set_span_in_context(span))
        try:
            await self.app(scope, receive, traced_send)
            if not ended:  # приложение вернулось, не дописав тело: клиент отключился
                span.add_event("disconnected")
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        except BaseException:
            span.add_event("cancelled")
            raise
        finally:
            finish()
            otel_context.detach(token)
