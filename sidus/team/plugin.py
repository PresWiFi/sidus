"""The Team plugin definition, loaded by Sidus when SIDUS_PLUGINS includes team."""
from __future__ import annotations

import threading

from ..plugins import Plugin
from . import api, db, engine, token


def _startup() -> None:
    db.conn()    # create team.db
    token.get()  # create the MCP token on first start
    engine.recover()
    threading.Thread(target=engine.ticker, daemon=True, name="team-ticker").start()


PLUGIN = Plugin(name="team", router=api.router, startup=_startup, authorize=token.authorize,
                nav={"label": "Team", "href": "#/team"}, script="/api/team/ui.js", style="/api/team/ui.css")
