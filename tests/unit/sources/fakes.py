"""A fake host for source tests: containers, their environment and the commands drkit runs."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from drkit.runner import CommandResult, PipeResult

# (producer exit code, consumer exit code, consumer stdout[, producer stderr[, consumer stderr]])
PipeResponse = tuple[int, int, str] | tuple[int, int, str, str] | tuple[int, int, str, str, str]


def summary(snapshot_id: str, size: int) -> str:
    """restic backup --json output for a successful snapshot."""
    return json.dumps(
        {
            "message_type": "summary",
            "snapshot_id": snapshot_id,
            "total_bytes_processed": size,
            "data_added": size,
        }
    )


@dataclass
class FakeHost:
    env: dict[str, str] = field(default_factory=dict)
    executables: set[str] = field(default_factory=set)
    run_outputs: dict[str, str] = field(default_factory=dict)
    pipes: list[PipeResponse] = field(default_factory=list)

    containers: list[tuple[str, str]] = field(default_factory=list)
    run_calls: list[list[str]] = field(default_factory=list)
    run_envs: list[dict[str, str] | None] = field(default_factory=list)
    pipe_calls: list[tuple[list[str], list[str], dict[str, Any]]] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)

    def compose_container(self, project: str, service: str) -> str:
        self.containers.append((project, service))
        return "ctr"

    def container_env(self, container: str, name: str) -> str | None:
        return self.env.get(name)

    def has_executable(self, container: str, executable: str) -> bool:
        return executable in self.executables

    def run(self, args: Sequence[str], **kwargs: Any) -> CommandResult:
        argv = list(args)
        if argv[:2] == ["restic", "forget"]:
            self.forgotten.extend(argv[2:])
            return CommandResult(tuple(argv), 0, "", "", 0.0)
        self.run_calls.append(argv)
        self.run_envs.append(kwargs.get("env"))
        # docker exec <ctr> <binary> ... → answer by binary name
        binary = argv[argv.index("ctr") + 1] if "ctr" in argv else argv[0]
        return CommandResult(tuple(argv), 0, self.run_outputs.get(binary, ""), "", 0.0)

    def pipe(self, producer: Sequence[str], consumer: Sequence[str], **kwargs: Any) -> PipeResult:
        self.pipe_calls.append((list(producer), list(consumer), kwargs))
        p_code, c_code, c_out, *errors = self.pipes.pop(0)
        p_err = errors[0] if errors else ""
        c_err = errors[1] if len(errors) > 1 else ""
        return PipeResult(
            producer=CommandResult(tuple(producer), p_code, "", p_err, 0.0),
            consumer=CommandResult(tuple(consumer), c_code, c_out, c_err, 0.0),
        )
