"""Authenticated MCP over explicit HTTPS or local HTTP, with a total deadline."""

import asyncio
import ipaddress
import logging
import math
import ssl
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.dispatcher import ProgressFnT
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Implementation

from specgate.payload import request_overflow
from specgate.privacy import ensure_safe_content
from specgate.protocol import (
    HANDSHAKE_TOOL,
    PROTOCOL_VERSION,
    NegotiatedProtocol,
    negotiate,
)
from specgate.sse_transport import workflow_http_client as streamable_http_client

_private_transport: ContextVar[bool] = ContextVar(
    "private_mcp_transport", default=False
)


class _PrivateMCPLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _private_transport.get():
            # SDK debug messages and parsing exceptions can contain entire payloads.
            record.msg, record.args = "MCP transport event (payload omitted)", ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_protocol_log_filter = _PrivateMCPLogs()
for _logger in (
    "mcp.client.sse",
    "mcp.client.streamable_http",
    "mcp.shared.dispatcher",
    "mcp.shared.jsonrpc_dispatcher",
    "client",
    "httpx2",
):
    logging.getLogger(_logger).addFilter(_protocol_log_filter)


class MCPTransportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MCPProtocolError(MCPTransportError):
    fallback = "harness"

    def __init__(self, message: str) -> None:
        super().__init__("protocol_incompatible", message)


async def call_tool(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
    *,
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    progress_callback: ProgressFnT | None = None,
    project_id: str | None = None,
) -> CallToolResult:
    ensure_safe_content(arguments, secrets=(token,))
    if overflow := request_overflow(arguments):
        raise overflow
    async with _session(
        url,
        token,
        transport=transport,
        ca_file=ca_file,
        timeout_seconds=timeout_seconds,
        project_id=project_id,
    ) as (session, _protocol):
        return await session.call_tool(
            name, arguments, progress_callback=progress_callback
        )


async def negotiate_protocol(
    url: str,
    token: str,
    *,
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    project_id: str | None = None,
) -> NegotiatedProtocol:
    """Authenticate and negotiate the public protocol without invoking a tool."""
    async with _session(
        url,
        token,
        transport=transport,
        ca_file=ca_file,
        timeout_seconds=timeout_seconds,
        project_id=project_id,
    ) as (_connected, protocol):
        return protocol


async def list_tools(
    url: str,
    token: str,
    *,
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    project_id: str | None = None,
) -> tuple[str, ...]:
    """List authenticated MCP tools without invoking a provider."""
    async with _session(
        url,
        token,
        transport=transport,
        ca_file=ca_file,
        timeout_seconds=timeout_seconds,
        project_id=project_id,
    ) as (session, _protocol):
        result = await session.list_tools()
        return tuple(tool.name for tool in result.tools)


@asynccontextmanager
async def _session(
    url: str,
    token: str,
    *,
    transport: str,
    ca_file: Path | None,
    timeout_seconds: float,
    project_id: str | None,
) -> AsyncIterator[tuple[ClientSession, NegotiatedProtocol]]:
    endpoint = urlsplit(url)
    try:
        loopback = ipaddress.ip_address(endpoint.hostname or "").is_loopback
    except ValueError:
        loopback = endpoint.hostname == "localhost"
    if (
        endpoint.scheme not in {"http", "https"}
        or (endpoint.scheme == "http" and not loopback)
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        raise ValueError(
            "Configure HTTPS explícito ou HTTP loopback, sem credenciais na URL."
        )
    if not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Configure SPECGATE_MCP_API_KEY válida no cliente.")
    if transport not in {"streamable", "legacy"}:
        raise ValueError("Escolha o transporte streamable ou legacy.")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
        raise ValueError("O prazo total deve ser maior que zero e até 300 segundos.")
    try:
        tls = ssl.create_default_context(cafile=ca_file)
    except (OSError, ValueError):
        raise ValueError(
            "Não foi possível carregar a CA configurada para o MCP."
        ) from None

    def http_client(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=tls,
            follow_redirects=False,
            trust_env=False,
        )

    logging_scope = _private_transport.set(True)
    protocol_failure: MCPProtocolError | None = None
    try:
        # Level cancellation also bounds SDK cleanup after the deadline fires.
        with anyio.fail_after(timeout_seconds):
            async with AsyncExitStack() as stack:
                headers = {"Authorization": f"Bearer {token}"}
                if project_id:
                    headers["X-Dev-Decision-Project-ID"] = project_id
                if transport == "legacy":
                    connection = sse_client(
                        url,
                        headers=headers,
                        timeout=timeout_seconds,
                        sse_read_timeout=timeout_seconds,
                        httpx_client_factory=http_client,
                    )
                else:
                    http = await stack.enter_async_context(
                        http_client(headers, httpx2.Timeout(timeout_seconds))
                    )
                    connection = streamable_http_client(url, http_client=http)
                read, write = await stack.enter_async_context(connection)
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timeout_seconds,
                        client_info=Implementation(
                            name="specgate-python", version=PROTOCOL_VERSION
                        ),
                    )
                )
                await session.initialize()
                try:
                    handshake = await session.call_tool(HANDSHAKE_TOOL, {})
                    if handshake.is_error:
                        raise ValueError("The MCP handshake failed.")
                    protocol = negotiate(handshake.structured_content)
                except (ValueError, MCPError) as error:
                    protocol_failure = MCPProtocolError(str(error))
                    raise protocol_failure from None
                yield session, protocol
    except TimeoutError:
        raise MCPTransportError(
            "timeout", "O MCP excedeu o prazo total da consulta."
        ) from None
    except asyncio.CancelledError:
        raise MCPTransportError(
            "interrupted", "A consulta ao MCP foi interrompida."
        ) from None
    except MCPTransportError:
        raise
    except (
        ExceptionGroup,
        httpx2.HTTPError,
        MCPError,
        OSError,
        ValueError,
        RuntimeError,
    ):
        if protocol_failure is not None:
            raise protocol_failure from None
        raise MCPTransportError(
            "connection",
            "Falha ao consultar o MCP; confira conexão, TLS, API key e endpoint.",
        ) from None
    finally:
        _private_transport.reset(logging_scope)
