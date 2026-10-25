"""The contract for evidence crossing the MCP boundary.

Shared deliberately by the server and the client. The server returns these models so MCP publishes
an `outputSchema` for each tool, and the client validates against the same models before anything
reaches a prompt - so "what a tool is allowed to return" is written once, in one file, rather than
being implied by whatever the server happened to send.

Every field mirrors what the corresponding `*_tool.collect()` already returns. Nothing is renamed,
reordered or rounded: swapping a Python import for a protocol call must not change a single
measured number, which is what the verification in Step 1 checks.
"""

from pydantic import BaseModel, ConfigDict, Field


class FailingOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str | None = Field(description="span name of the failing operation")
    error_ratio: float = Field(description="fraction of calls to it that failed, 0-1")


class PrometheusEvidence(BaseModel):
    """What prometheus_tool.collect() produces for one service.

    `values` holds the 13 measurements and their baselines; a measurement that could not be taken
    is null rather than estimated, so every float is optional. `queries` carries the exact PromQL
    behind each one, which is what makes a number auditable.
    """

    model_config = ConfigDict(extra="forbid")

    service: str | None
    collected_at: str
    prometheus: str
    values: dict[str, float | None]
    queries: dict[str, str]
    failing_operations: list[FailingOperation]
    errors: list[str]
    text: str = Field(description="the evidence rendered for an agent prompt")


class LogLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str | None
    text: str
    count: int
    sources: list[str]


class LogSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool | None = None
    total: int | None = None
    reason: str | None = None


class LogWindow(BaseModel):
    # "from" is a Python keyword, so the field is named from_ and aliased; populate_by_name lets it
    # round-trip under either spelling.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class LogEvidence(BaseModel):
    """What log_tool.collect() produces: OpenSearch and container stdout, merged and de-duplicated.

    This is the tool carrying text written by application code, so it is the one place where
    untrusted content enters the system. The guardrail layer validates and neutralises that content
    on top of this shape rather than in place of it.
    """

    model_config = ConfigDict(extra="forbid")

    service: str | None
    collected_at: str
    sources: dict[str, LogSource]
    error_lines: list[LogLine]
    sample_lines: list[LogLine]
    counts: dict[str, int]
    window: LogWindow | None = None
    text: str = Field(description="the evidence rendered for an agent prompt")


class ActiveFlag(BaseModel):
    """A feature flag flagd is serving as something other than "off".

    `relation` is added by change_tool after reading the real dependency graph from the containers'
    environment, so the agent is told whether the flag matters to *this* incident rather than
    guessing which services call which.
    """

    model_config = ConfigDict(extra="forbid")

    flag: str
    variant: str
    value: bool | float | int | str | None = None
    affects: str
    relation: str | None = None


class ContainerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool | None = None
    status: str | None = None
    started_at: str | None = None
    restart_count: int | None = None
    image: str | None = None
    started_before_incident: str | None = None
    recently_restarted: bool | None = None
    error: str | None = None


class ChangeEvidence(BaseModel):
    """What change_tool.collect() produces: flags, container state and the dependency graph."""

    model_config = ConfigDict(extra="forbid")

    service: str | None
    collected_at: str
    active_flags: list[ActiveFlag]
    flag_error: str | None = None
    container: ContainerState
    notes: list[str]
    dependencies: list[str] = Field(default_factory=list)
    text: str = Field(description="the evidence rendered for an agent prompt")
