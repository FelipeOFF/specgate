"""Bounded large workflow responses for the pinned MCP 2.2 transport.

Adapted from modelcontextprotocol/python-sdk streamable_http.py (MIT).
Copyright (c) 2024 Anthropic, PBC. The upstream license is retained in
licenses/mcp-python-sdk-MIT.txt. Remove this adapter when the SDK exposes
EventSource.max_event_size on its client transport.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
from mcp.client._transport import TransportStreams
from mcp.client.streamable_http import RequestContext, StreamableHTTPTransport
from mcp.shared._context_streams import create_context_streams
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCRequest

MAX_EVENT_BYTES = 8 * 1024 * 1024


class _WorkflowTransport(StreamableHTTPTransport):
    async def _handle_sse_response(
        self, response: httpx2.Response, ctx: RequestContext
    ) -> None:
        assert isinstance(ctx.session_message.message, JSONRPCRequest)
        request_id = ctx.session_message.message.id
        last_event_id = None
        retry_interval_ms = None
        try:
            async for event in httpx2.EventSource(
                response, max_event_size=MAX_EVENT_BYTES
            ):
                if event.id:
                    last_event_id = event.id
                if event.retry is not None:
                    retry_interval_ms = event.retry
                complete = await self._handle_sse_event(
                    event,
                    ctx.read_stream_writer,
                    original_request_id=request_id,
                    resumption_callback=(
                        ctx.metadata.on_resumption_token_update
                        if ctx.metadata
                        else None
                    ),
                )
                if complete:
                    await response.aclose()
                    return
        except httpx2.SSEError:
            await self._resolve_abandoned_request(
                ctx.read_stream_writer,
                request_id,
                "SSE response exceeds the workflow event limit",
            )
            return
        except (httpx2.HTTPError, OSError):
            pass
        if last_event_id is not None:
            await self._handle_reconnection(ctx, last_event_id, retry_interval_ms)
        else:
            await self._resolve_abandoned_request(
                ctx.read_stream_writer,
                request_id,
                "SSE stream ended without a response",
            )


@asynccontextmanager
async def workflow_http_client(
    url: str, *, http_client: httpx2.AsyncClient
) -> AsyncIterator[TransportStreams]:
    transport = _WorkflowTransport(url)
    reader_writer, reader = create_context_streams[SessionMessage | Exception](0)
    writer, writer_reader = create_context_streams[SessionMessage](0)
    async with (
        reader_writer,
        reader,
        writer,
        writer_reader,
        anyio.create_task_group() as tasks,
    ):

        def start_get_stream() -> None:
            tasks.start_soon(transport.handle_get_stream, http_client, reader_writer)

        tasks.start_soon(
            transport.post_writer,
            http_client,
            writer_reader,
            reader_writer,
            writer,
            start_get_stream,
            tasks,
        )
        try:
            yield reader, writer
        finally:
            if transport.session_id:
                await transport.terminate_session(http_client)
            tasks.cancel_scope.cancel()
