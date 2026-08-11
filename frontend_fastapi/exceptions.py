"""
Control-flow exceptions raised by deps.py's auth gates and turned into an
actual response by main.py's exception handlers. Split into their own
module (rather than living in deps.py, where they were originally defined)
so database.py can import them too without a deps.py <-> database.py
import cycle -- see database.get_db's docstring for why it needs to tell
these apart from a real error.
"""


class NotAuthenticated(Exception):
    """No active session user -- caught in main.py, redirects to login."""


class Forbidden(Exception):
    """Logged in, but not staff, or a failed CSRF check -- caught in
    main.py, renders a 403 page."""
