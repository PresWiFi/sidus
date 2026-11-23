"""Ideas: a project tracker that Claude fills in from any Sidus session. Turn on with SIDUS_PLUGINS=ideas.

The plugin itself lives in .plugin and is imported only when Sidus asks for PLUGIN. That keeps
`python -m sidus.ideas.mcp` (the MCP server Claude starts) light: it doesn't load FastAPI or Sidus's settings.
"""


def __getattr__(name: str):
    if name == "PLUGIN":
        from .plugin import PLUGIN
        return PLUGIN
    raise AttributeError(name)
