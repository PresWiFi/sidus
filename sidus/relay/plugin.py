"""The Relay plugin definition, loaded by Sidus when SIDUS_PLUGINS includes relay."""
from __future__ import annotations

import threading

from ..plugins import Plugin
from . import api, db, runner, token


def _startup() -> None:
    db.conn()    # create relay.db
    token.get()  # create the MCP token on first start
    runner.recover()
    threading.Thread(target=runner.ticker, daemon=True, name="relay-ticker").start()


PLUGIN = Plugin(name="relay", router=api.router, startup=_startup, authorize=token.authorize,
                nav={"label": "Relay", "href": "#/relay"}, script="/api/relay/ui.js", style="/api/relay/ui.css")
