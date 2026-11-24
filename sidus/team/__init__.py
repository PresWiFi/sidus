"""Team: hand it a goal or a runbook. A manager (Claude or Codex) splits it into phases, you approve them once, and
Claude Code and Codex work through the tasks one at a time, by plan usage. Turn on with SIDUS_PLUGINS=team.

The plugin itself lives in .plugin and is imported only when Sidus asks for PLUGIN, so `python -m sidus.team.mcp`
(the MCP server the agents start) stays light.
"""


def __getattr__(name: str):
    if name == "PLUGIN":
        from .plugin import PLUGIN
        return PLUGIN
    raise AttributeError(name)
