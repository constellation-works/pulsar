"""The ``pulsar`` commands, one module each (or one per help group of a few).

Each module declares its commands in ``register`` and holds their handlers.
``REGISTER`` is the order argparse lists them in.
"""

from . import auth, history, maintenance, publish, reconcile, services, status

REGISTER = (
    auth.register,
    status.register,
    history.register,
    publish.register,
    reconcile.register,
    maintenance.register,
    services.register,
)

__all__ = ["REGISTER"]
