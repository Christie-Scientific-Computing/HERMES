"""
Phase 5 cutover: re-verify Starlette's default `redirect_slashes=True`
behavior against this app's ACTUAL route table, not just documented
framework behavior (Phase 0's own risk note -- every current Django route
ends in `/` via APPEND_SLASH, so an old bookmark/link hitting a
FastAPI route without one needs to still land somewhere sensible).

Builds the real app (every router included, matching main.py) via the
`app` fixture in conftest.py, and for every GET route registered WITHOUT a
trailing slash, confirms that requesting the same path WITH one added
redirects back to the no-slash form rather than 404ing.
"""
from fastapi.routing import APIRoute


def _concrete_path(route: APIRoute) -> str:
    """Fills in any {param} segments with a harmless dummy value -- this
    test only cares about slash-redirect routing, not what a real handler
    does with the value, so any non-empty string that doesn't itself
    contain a slash works."""
    path = route.path
    for param_name in route.param_convertors:
        path = path.replace(f"{{{param_name}}}", "dummy")
    return path


def _iter_api_routes(routes):
    """Recurses through include_router's wrapper objects (FastAPI >=0.14
    nests an included router's own APIRoutes under `.original_router.routes`
    rather than flattening them straight into `app.routes`) to reach the
    actual APIRoute instances."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)


def _get_routes_without_trailing_slash(app) -> list[str]:
    paths = []
    for route in _iter_api_routes(app.routes):
        if "GET" not in route.methods:
            continue
        path = _concrete_path(route)
        if path == "/" or path.endswith("/"):
            continue
        paths.append(path)
    return sorted(set(paths))


def test_every_get_route_is_registered_without_a_trailing_slash(app):
    """Guards the assumption the redirect test below relies on -- if this
    ever starts failing, a route was added WITH a trailing slash and the
    redirect direction assumption needs revisiting, not silently skipped."""
    paths = _get_routes_without_trailing_slash(app)
    assert len(paths) > 10  # sanity: every router above actually contributed routes


def test_adding_a_trailing_slash_redirects_back_to_the_registered_path(app, client):
    paths = _get_routes_without_trailing_slash(app)

    for path in paths:
        response = client.get(f"{path}/", follow_redirects=False)
        assert response.status_code in (307, 308), (
            f"{path}/ did not redirect (got {response.status_code}) -- an old bookmark or "
            f"hand-typed trailing-slash URL for this route would now 404"
        )
        # TestClient resolves Location against its base_url -- compare only
        # the path component, not the scheme/host it prepends.
        assert response.headers["location"].removeprefix("http://localhost") == path
