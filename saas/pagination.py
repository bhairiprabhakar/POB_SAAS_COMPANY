"""
Shared pagination bounds (issue: unbounded ?limit=... on list endpoints
could force large database scans / excessive result sizes).

Usage in a router:

    from fastapi import Query
    from ..pagination import PageLimit, PageOffset

    @router.get("")
    def list_things(limit: int = PageLimit(), offset: int = PageOffset(), ...):
        ...

`PageLimit()` defaults to 100, allows 1-1000. Override the default/max per
endpoint when a smaller or larger bound genuinely makes sense, e.g.
PageLimit(default=50, le=500).
"""
from fastapi import Query

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def PageLimit(default: int = DEFAULT_LIMIT, le: int = MAX_LIMIT):
    return Query(default, ge=1, le=le)


def PageOffset(default: int = 0):
    return Query(default, ge=0)
