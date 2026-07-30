"""
Async reverse proxy helper.

Forwards a FastAPI request to the HERMES backend, transparently handling
both regular JSON responses and SSE streaming responses (import/export).
No business logic lives here on purpose -- PACS queries and anonymisation
are handled inside the backend itself now (backend/src/identity/anon.py),
since the mapping DB is reachable directly from the backend's network.
"""
import httpx
from fastapi import Request, Response, HTTPException
from fastapi.responses import StreamingResponse

# Headers that must not be forwarded between hops
_STRIP_HEADERS = {
    'host', 'content-length', 'transfer-encoding',
    'connection', 'keep-alive', 'upgrade',
    'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers',
}


async def proxy_request(request: Request, path: str) -> Response:
    """
    Forward `request` to `/{path}` on the HERMES backend, preserving method,
    body, query params, and headers. SSE streams are forwarded as-is.
    """
    client: httpx.AsyncClient = request.app.state.client

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_HEADERS}
    body = await request.body()

    try:
        req = client.build_request(
            method=request.method,
            url=f"/{path}",
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )
        response = await client.send(req, stream=True)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="HERMES backend is unreachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="HERMES backend timed out")

    content_type = response.headers.get('content-type', '')
    forward_headers = {
        k: v for k, v in response.headers.items()
        if k.lower() not in _STRIP_HEADERS
    }

    if 'text/event-stream' in content_type:
        async def generate():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            generate(),
            status_code=response.status_code,
            headers=forward_headers,
            media_type='text/event-stream',
        )

    content = await response.aread()
    await response.aclose()
    return Response(
        content=content,
        status_code=response.status_code,
        headers=forward_headers,
        media_type=content_type or None,
    )
