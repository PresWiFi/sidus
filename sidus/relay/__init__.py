"""Relay: one thread, two agents. Each turn goes to Claude Code or Codex, whichever plan has room, and the two can
ask each other questions or hand work over. Turn on with SIDUS_PLUGINS=relay.

The plugin itself lives in .plugin and is imported only when Sidus asks for PLUGIN, so `python -m sidus.relay.mcp`
(the MCP server the agents start) stays light.
"""


def __getattr__(name: str):
    if name == "PLUGIN":
        from .plugin import PLUGIN
        return PLUGIN
    raise AttributeError(name)
