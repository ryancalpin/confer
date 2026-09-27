"""Confer — open agent-to-agent coordination for personal AI assistants."""

__version__ = "0.1.0"

from .node import Node, NodeError  # noqa: E402

__all__ = ["Node", "NodeError", "__version__"]
