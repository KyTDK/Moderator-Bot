from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Sequence

log = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_IMAGE = "ghcr.io/kytdk/moderator-bot:latest"
DEFAULT_CONTAINER_ARGS = (
    "--restart",
    "always",
    "--env-file",
    "/srv/modbot/.env",
    "--network",
    "host",
    "--add-host",
    "host.docker.internal:host-gateway",
)
DEFAULT_SERVICES = ("moderator-bot",)


class UpdateConfigError(ValueError):
    """Raised when the Docker update configuration is invalid."""


class DockerCommandError(RuntimeError):
    """Raised when a Docker CLI command fails."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        exit_code: int,
        stdout: str,
        stderr: str,
        message: str | None = None,
    ) -> None:
        self.command = tuple(command)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        detail = message or self._build_detail()
        super().__init__(detail)

    def _build_detail(self) -> str:
        quoted = " ".join(shlex.quote(arg) for arg in self.command)
        base = f"{quoted} failed with exit code {self.exit_code}"
        detail = (self.stderr or self.stdout).strip()
        if not detail:
            return base
        snippet = detail if len(detail) <= 400 else detail[:397] + "..."
        return f"{base}: {snippet}"


@dataclass(slots=True)
class CommandOutcome:
    """Structured result for a completed CLI invocation."""

    command: tuple[str, ...]
    stdout: str
    stderr: str
    duration: float
    exit_code: int = 0


@dataclass(slots=True)
class ServiceUpdateResult:
    service: str
    outcome: CommandOutcome


@dataclass(slots=True)
class UpdateReport:
    image: str
    pull: CommandOutcome
    services: list[ServiceUpdateResult]
    rollout_mode: str

    @property
    def total_duration(self) -> float:
        return self.pull.duration + sum(result.outcome.duration for result in self.services)


CommandRunner = Callable[[Sequence[str], float | None, Mapping[str, str] | None], Awaitable[CommandOutcome]]


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _parse_positive_float(value: str | None, *, default: float, name: str) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        parsed = float(value)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise UpdateConfigError(f"{name} must be numeric, received {value!r}") from exc
    if parsed <= 0:
        raise UpdateConfigError(f"{name} must be greater than 0, received {value!r}")
    return parsed


def _parse_positive_int(value: str | None, *, default: int, name: str) -> int:
    parsed = _parse_positive_float(value, default=float(default), name=name)
    final = int(parsed)
    if final < 1:
        raise UpdateConfigError(f"{name} must be at least 1, received {value!r}")
    return final


def _parse_services(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return tuple()
    entries = []
    for chunk in raw.replace("\n", ",").split(","):
        cleaned = chunk.strip()
        if cleaned:
            entries.append(cleaned)
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return tuple(ordered)


def _parse_extra_flags(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return tuple()
    return tuple(shlex.split(raw))


@dataclass(slots=True)
class DockerUpdateConfig:
    image: str
    services: tuple[str, ...]
    docker_binary: str
    with_registry_auth: bool
    update_order: str
    update_parallelism: int
    update_delay: str
    pull_timeout: float
    update_timeout: float
    extra_flags: tuple[str, ...]
    env: dict[str, str]
    deployment_mode: str
    container_name: str
    container_args: tuple[str, ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DockerUpdateConfig:
        source = env if env is not None else os.environ

        def _first(*keys: str, default: str | None = None) -> str | None:
            for key in keys:
                value = source.get(key)
                if value is not None and value.strip():
                    return value.strip()
            return default

        image = _first("MODBOT_DOCKER_IMAGE", "DOCKER_IMAGE", default=DEFAULT_IMAGE) or DEFAULT_IMAGE

        services_raw = _first("MODBOT_DOCKER_SERVICES", "DOCKER_SERVICES")
        if services_raw:
            services = _parse_services(services_raw)
            if not services:
                raise UpdateConfigError(
                    "Set MODBOT_DOCKER_SERVICES to at least one service name (comma separated)."
                )
        else:
            services = DEFAULT_SERVICES

        docker_binary = _first(
            "MODBOT_DOCKER_BINARY",
            "MODBOT_DOCKER_BIN",
            "DOCKER_BINARY",
            "DOCKER_BIN",
            default="docker",
        ) or "docker"

        with_registry_auth = _parse_bool(
            _first("MODBOT_DOCKER_WITH_REGISTRY_AUTH", "DOCKER_WITH_REGISTRY_AUTH"),
            default=False,
        )

        update_order = (_first("MODBOT_DOCKER_UPDATE_ORDER", "DOCKER_UPDATE_ORDER", default="start-first") or "start-first").lower()
        if update_order not in {"start-first", "stop-first"}:
            raise UpdateConfigError("MODBOT_DOCKER_UPDATE_ORDER must be 'start-first' or 'stop-first'.")

        update_parallelism = _parse_positive_int(
            _first("MODBOT_DOCKER_UPDATE_PARALLELISM", "DOCKER_UPDATE_PARALLELISM"),
            default=1,
            name="MODBOT_DOCKER_UPDATE_PARALLELISM",
        )

        update_delay = _first("MODBOT_DOCKER_UPDATE_DELAY", "DOCKER_UPDATE_DELAY", default="0s") or "0s"

        pull_timeout = _parse_positive_float(
            _first("MODBOT_DOCKER_PULL_TIMEOUT", "DOCKER_PULL_TIMEOUT"),
            default=300.0,
            name="MODBOT_DOCKER_PULL_TIMEOUT",
        )
        update_timeout = _parse_positive_float(
            _first("MODBOT_DOCKER_UPDATE_TIMEOUT", "DOCKER_UPDATE_TIMEOUT"),
            default=600.0,
            name="MODBOT_DOCKER_UPDATE_TIMEOUT",
        )

        extra_flags = _parse_extra_flags(_first("MODBOT_DOCKER_EXTRA_FLAGS", "DOCKER_EXTRA_FLAGS"))

        env_overrides: dict[str, str] = {}
        docker_host = _first("MODBOT_DOCKER_HOST", "DOCKER_HOST")
        if docker_host:
            env_overrides["DOCKER_HOST"] = docker_host
        docker_context = _first("MODBOT_DOCKER_CONTEXT", "DOCKER_CONTEXT")
        if docker_context:
            env_overrides["DOCKER_CONTEXT"] = docker_context
        tls_verify = _first("MODBOT_DOCKER_TLS_VERIFY", "DOCKER_TLS_VERIFY")
        if tls_verify is not None:
            env_overrides["DOCKER_TLS_VERIFY"] = tls_verify
        cert_path = _first("MODBOT_DOCKER_CERT_PATH", "DOCKER_CERT_PATH")
        if cert_path:
            env_overrides["DOCKER_CERT_PATH"] = cert_path
        config_path = _first("MODBOT_DOCKER_CONFIG", "DOCKER_CONFIG")
        if config_path:
            env_overrides["DOCKER_CONFIG"] = config_path

        deployment_mode = (_first("MODBOT_DOCKER_DEPLOYMENT", "MODBOT_DEPLOYMENT_MODE", default="container") or "container").lower()
        if deployment_mode not in {"service", "container"}:
            raise UpdateConfigError("MODBOT_DOCKER_DEPLOYMENT must be 'service' or 'container'.")

        container_name = _first("MODBOT_DOCKER_CONTAINER_NAME", "DOCKER_CONTAINER_NAME", default="moderator-bot") or "moderator-bot"
        container_args_raw = _first("MODBOT_DOCKER_CONTAINER_ARGS", "DOCKER_CONTAINER_ARGS")
        if container_args_raw:
            container_args = _parse_extra_flags(container_args_raw)
        else:
            container_args = DEFAULT_CONTAINER_ARGS

        return cls(
            image=image,
            services=services,
            docker_binary=docker_binary,
            with_registry_auth=with_registry_auth,
            update_order=update_order,
            update_parallelism=update_parallelism,
            update_delay=update_delay,
            pull_timeout=pull_timeout,
            update_timeout=update_timeout,
            extra_flags=extra_flags,
            env=env_overrides,
            deployment_mode=deployment_mode,
            container_name=container_name,
            container_args=container_args,
        )


async def _run_subprocess(
    args: Sequence[str],
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandOutcome:
    start = time.perf_counter()
    log.debug("Running command: %s", " ".join(shlex.quote(arg) for arg in args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=(os.environ | dict(env)) if env else None,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment specific
        raise DockerCommandError(
            command=args,
            exit_code=-1,
            stdout="",
            stderr="",
            message=f"{args[0]!r} is not available on PATH",
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await proc.communicate()
        finally:
            pass
        raise DockerCommandError(
            command=args,
            exit_code=-1,
            stdout="",
            stderr="",
            message=f"Command timed out after {timeout:.1f}s" if timeout else "Command timed out",
        ) from exc

    duration = time.perf_counter() - start
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise DockerCommandError(
            command=args,
            exit_code=proc.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )

    log.debug("Command succeeded in %.2fs: %s", duration, " ".join(shlex.quote(arg) for arg in args))
    return CommandOutcome(
        command=tuple(args),
        stdout=stdout_text,
        stderr=stderr_text,
        duration=duration,
        exit_code=proc.returncode,
    )


class DockerUpdateManager:
    def __init__(
        self,
        config: DockerUpdateConfig,
        *,
        executor: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self._executor = executor or _run_subprocess

    async def run(self) -> UpdateReport:
        pull_command = [self.config.docker_binary, "pull", self.config.image]
        env = self.config.env or None
        pull_result = await self._executor(pull_command, self.config.pull_timeout, env)

        if self.config.deployment_mode == "container":
            service_results = await self._run_container_update(env)
        else:
            service_results = await self._run_service_update(env)

        rollout_mode = (
            f"{self.config.update_order}, "
            f"parallelism={self.config.update_parallelism}, "
            f"delay={self.config.update_delay}"
        )
        return UpdateReport(
            image=self.config.image,
            pull=pull_result,
            services=service_results,
            rollout_mode=rollout_mode,
        )

    def _build_service_command(self, service: str) -> list[str]:
        command = [
            self.config.docker_binary,
            "service",
            "update",
            f"--image={self.config.image}",
            f"--update-order={self.config.update_order}",
            f"--update-parallelism={self.config.update_parallelism}",
            f"--update-delay={self.config.update_delay}",
        ]
        if self.config.with_registry_auth:
            command.append("--with-registry-auth")
        command.extend(self.config.extra_flags)
        command.append(service)
        return command

    async def _run_service_update(self, env: Mapping[str, str] | None) -> list[ServiceUpdateResult]:
        service_results: list[ServiceUpdateResult] = []
        for service in self.config.services:
            command = self._build_service_command(service)
            outcome = await self._executor(command, self.config.update_timeout, env)
            service_results.append(ServiceUpdateResult(service=service, outcome=outcome))
        return service_results

    async def _run_container_update(self, env: Mapping[str, str] | None) -> list[ServiceUpdateResult]:
        container = self.config.container_name
        results: list[ServiceUpdateResult] = []

        stop_command = [self.config.docker_binary, "stop", container]
        stop_outcome = await self._best_effort(stop_command, env)
        results.append(ServiceUpdateResult(service=f"{container}:stop", outcome=stop_outcome))

        rm_command = [self.config.docker_binary, "rm", container]
        rm_outcome = await self._best_effort(rm_command, env)
        results.append(ServiceUpdateResult(service=f"{container}:rm", outcome=rm_outcome))

        run_command = [
            self.config.docker_binary,
            "run",
            "-d",
            "--name",
            container,
            *self.config.container_args,
            self.config.image,
        ]
        run_outcome = await self._executor(run_command, self.config.update_timeout, env)
        results.append(ServiceUpdateResult(service=f"{container}:run", outcome=run_outcome))
        return results

    async def _best_effort(self, command: list[str], env: Mapping[str, str] | None) -> CommandOutcome:
        try:
            return await self._executor(command, self.config.update_timeout, env)
        except DockerCommandError as exc:
            log.debug("Command %s failed but will be ignored: %s", " ".join(command), exc)
            return CommandOutcome(
                command=tuple(command),
                stdout=exc.stdout,
                stderr=exc.stderr,
                duration=0.0,
                exit_code=exc.exit_code,
            )


def format_update_report(report: UpdateReport) -> str:
    lines = [
        f"Update complete in {report.total_duration:.1f}s",
        f"Image: `{report.image}`",
        f"Rollout: {report.rollout_mode}",
        "Services:",
    ]
    for result in report.services:
        lines.append(f"- `{result.service}` ({result.outcome.duration:.1f}s)")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_IMAGE",
    "CommandOutcome",
    "DockerCommandError",
    "DockerUpdateConfig",
    "DockerUpdateManager",
    "ServiceUpdateResult",
    "UpdateConfigError",
    "UpdateReport",
    "format_update_report",
]
