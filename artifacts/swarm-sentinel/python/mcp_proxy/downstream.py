"""Engine-launched downstream MCP servers, one pool per live session.

Commands come only from the policy's mcp section; callers never supply a command, argument or path.
Each server holds its connection in a dedicated task so the transport's cancel scopes are entered
and exited in the same task, while request handlers in other tasks share the open client.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

FIXTURES = Path(__file__).parent / "fixtures"


class DownstreamUnavailable(RuntimeError):
    pass


class _Connection:
    def __init__(self, params):
        self.params = params
        self.client = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.failed = False
        self.task = asyncio.create_task(self._hold())

    async def _hold(self):
        try:
            async with Client(self.params) as client:
                self.client = client
                self.ready.set()
                await self.stop.wait()
        except Exception:
            self.failed = True
        finally:
            self.client = None
            self.ready.set()


class DownstreamPool:
    def __init__(self, mcp):
        self.mcp = mcp
        self._workspace = tempfile.TemporaryDirectory(prefix="swarm-mcp-")
        self.workspace = Path(self._workspace.name)
        self.connections = {}
        self.listings = {}
        self.loop = None
        self.lock = None
        self.closed = False

    def parameters(self, name):
        server = self.mcp.servers[name]
        workspace = self.workspace / name
        workspace.mkdir(exist_ok=True)
        values = {"{python}": sys.executable, "{fixtures}": str(FIXTURES), "{workspace}": str(workspace)}

        def fill(text):
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            return text
        return StdioServerParameters(command=fill(server.command), args=[fill(a) for a in server.args],
                                     cwd=str(workspace))

    async def client(self, name):
        if self.lock is None:
            self.loop, self.lock = asyncio.get_running_loop(), asyncio.Lock()
        async with self.lock:
            if self.closed:
                raise DownstreamUnavailable("Session is closed")
            connection = self.connections.get(name)
            if connection is None or connection.failed or connection.task.done():
                connection = self.connections[name] = _Connection(self.parameters(name))
        await connection.ready.wait()
        if connection.client is None:
            raise DownstreamUnavailable(f"Downstream server {name} is unavailable")
        return connection.client

    async def list_tools(self, name):
        if name not in self.listings:
            result = await (await self.client(name)).list_tools()
            self.listings[name] = {tool.name: tool for tool in result.tools}
        return self.listings[name]

    async def call(self, name, tool, arguments):
        return await (await self.client(name)).call_tool(tool, arguments)

    async def aclose(self):
        self.closed = True
        for connection in self.connections.values():
            connection.stop.set()
        await asyncio.gather(*(c.task for c in self.connections.values()), return_exceptions=True)
        self._workspace.cleanup()

    def close(self):
        """Safe from any thread. Server processes stop before their workspace is removed."""
        self.closed = True
        if self.loop is None or self.loop.is_closed():
            self._workspace.cleanup()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(self.aclose())
        else:
            asyncio.run_coroutine_threadsafe(self.aclose(), self.loop)
