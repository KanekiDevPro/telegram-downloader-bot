"""Handlers package: aiogram routers.

: data:`ROUTERS` is the *dispatch order* the entrypoint registers — most
specific first. The admin router's FSM-state handlers (a broadcast draft above
all) must claim their message before the user router's text wildcard answers
it; with the wildcard first, an admin's announcement text was swallowed by the
generic "another step is in progress" fallback and the broadcast never started.
"""

from handlers.admin import router as admin_router
from handlers.payment import router as payment_router
from handlers.user import router as user_router

#: The routers, in the order updates are offered to them.
ROUTERS = (admin_router, payment_router, user_router)

__all__ = ["ROUTERS", "admin_router", "payment_router", "user_router"]
