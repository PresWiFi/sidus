"""Team: a manager (Claude or Codex) plans an Ideas milestone into tasks, you approve them, and Claude Code and
Codex work through them one at a time, by plan usage. Turn on with SIDUS_PLUGINS=ideas,team.

The plugin itself lives in .plugin and is imported only when Sidus asks for PLUGIN, so `python -m sidus.team.mcp`
(the MCP server the agents start) stays light.
"""


def __getattr__(name: str):
    if name == "PLUGIN":
        from .plugin import PLUGIN
        return PLUGIN
    raise AttributeError(name)
