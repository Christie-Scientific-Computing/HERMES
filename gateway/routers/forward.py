"""
Catch-all proxy router.

Forwards any request that isn't matched by a more specific gateway router
transparently to the Hermes backend.  This covers /import/*, /export/*,
/results/*, and any future Hermes endpoints without requiring gateway changes.

Must be included last in main.py so specific routers take precedence.
"""
from fastapi import APIRouter, Request
from proxy import proxy_request

router = APIRouter(tags=["proxy"])


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def forward(request: Request, path: str):
    return await proxy_request(request, path)
