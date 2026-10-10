"""The engine's MCP endpoint: agents see one MCP server, the engine decides and dispatches every call.

Only tools are offered. Resources, prompts, sampling and elicitation are not implemented here, so
clients see no such capability rather than an unchecked pass-through.
"""
from contextlib import asynccontextmanager

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from starlette.responses import JSONResponse

from .broker import lookup


def _bearer(headers):
    scheme, _, credential = (headers.get("authorization") or "").partition(" ")
    return credential.strip() if scheme.lower() == "bearer" else ""


def _text(message, error=True):
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=error)


def build(get_session):
    """Return the MCP endpoint, routing each credential to its own live session."""

    def caller(ctx):
        headers = getattr(ctx.request, "headers", None) or getattr(getattr(ctx, "transport", None), "headers", None) or {}
        bound = lookup(_bearer(headers))
        session = get_session(bound[0]) if bound else None
        if session is None or session.mcp is None or session.closed:
            raise MCPError(code=types.INVALID_REQUEST, message="Unknown or expired agent credential")
        return session.mcp, bound[1]

    async def list_tools(ctx, params):
        broker, agent = caller(ctx)
        tools = []
        for exposed in await anyio.to_thread.run_sync(broker.visible, agent):
            server, tool, _ = broker.resolve(exposed)
            listed = (await broker.pool.list_tools(server)).get(tool)
            if listed is not None:
                tools.append(types.Tool(name=exposed, description=listed.description,
                                        input_schema=listed.input_schema))
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params):
        broker, agent = caller(ctx)
        arguments = params.arguments or {}
        admission = await anyio.to_thread.run_sync(broker.admit, agent, params.name, arguments)
        if not admission.allowed:
            d = admission.decision
            return _text(f"Refused by the ASP gateway ({d['rule']}): {d['reason']}. The tool was not run.")
        try:
            result = await broker.pool.call(admission.server, admission.tool, arguments)
        except Exception:
            await anyio.to_thread.run_sync(broker.record, admission, "failed")
            return _text("The downstream tool failed or was unreachable.")
        return await anyio.to_thread.run_sync(broker.record, admission,
                                              "failed" if result.is_error else "succeeded", result)

    server = Server("swarmsentinel-mcp-proxy", on_list_tools=list_tools, on_call_tool=call_tool)
    return Endpoint(server)


class Endpoint:
    """ASGI app for the MCP route. A session manager runs once, so each engine lifespan gets a new one."""

    def __init__(self, server):
        self.server = server
        self.inner = None

    @asynccontextmanager
    async def run(self):
        # Stateless JSON exchanges: every request carries its credential, no server-side MCP session.
        manager = StreamableHTTPSessionManager(app=self.server, stateless=True, json_response=True)
        async with manager.run():
            self.inner = StreamableHTTPASGIApp(manager)
            try:
                yield
            finally:
                self.inner = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            if lookup(_bearer(headers)) is None:
                response = JSONResponse({"detail": "Agent credential required"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
                return await response(scope, receive, send)
            if self.inner is None:
                return await JSONResponse({"detail": "MCP endpoint is not running"}, status_code=503)(scope, receive, send)
        await self.inner(scope, receive, send)
