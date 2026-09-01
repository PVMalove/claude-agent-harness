from pathlib import Path

from ...domain.policy import RetryPolicy
from .cli import CLIProvider


class MCPProvider(CLIProvider):
    """Run an MCP adapter using the shared provider stream contract."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: float = 600.0,
        cwd: str | Path | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(
            command=command,
            args=args,
            timeout=timeout,
            cwd=cwd,
            retry_policy=retry_policy,
        )
