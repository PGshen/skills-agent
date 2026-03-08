"""Output sink implementations."""
from .sink import OutputSink, NullSink
from .cli_sink import CLISink
from .sse_sink import SSESink

__all__ = ["OutputSink", "NullSink", "CLISink", "SSESink"]
