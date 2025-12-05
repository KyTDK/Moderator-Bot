from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from modules.devops.docker_update import (  # noqa: E402
    CommandOutcome,
    DEFAULT_IMAGE,
    DEFAULT_SERVICES,
    DEFAULT_CONTAINER_ARGS,
    DockerUpdateConfig,
    DockerUpdateManager,
    ServiceUpdateResult,
    UpdateConfigError,
    UpdateReport,
    format_update_report,
    DockerCommandError,
)

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_config_uses_defaults_when_missing() -> None:
    env = {}
    config = DockerUpdateConfig.from_env(env)
    assert config.image == DEFAULT_IMAGE
    assert config.services == DEFAULT_SERVICES
    assert config.deployment_mode == "auto"
    assert config.container_name == "moderator-bot"
    assert config.container_args == DEFAULT_CONTAINER_ARGS


def test_config_falls_back_to_default_services_when_env_blank() -> None:
    env = {"MODBOT_DOCKER_SERVICES": "   "}
    config = DockerUpdateConfig.from_env(env)
    assert config.services == DEFAULT_SERVICES


def test_config_parsing_defaults() -> None:
    env = {
        "MODBOT_DOCKER_IMAGE": "ghcr.io/example/modbot:latest",
        "MODBOT_DOCKER_SERVICES": "alpha, beta\n gamma",
        "MODBOT_DOCKER_WITH_REGISTRY_AUTH": "yes",
        "MODBOT_DOCKER_UPDATE_PARALLELISM": "2",
        "MODBOT_DOCKER_UPDATE_DELAY": "5s",
        "MODBOT_DOCKER_UPDATE_ORDER": "START-FIRST",
        "MODBOT_DOCKER_EXTRA_FLAGS": "--label-add stack=dev --env ROLLING=1",
        "MODBOT_DOCKER_PULL_TIMEOUT": "120",
        "MODBOT_DOCKER_UPDATE_TIMEOUT": "240",
    }

    config = DockerUpdateConfig.from_env(env)

    assert config.image == env["MODBOT_DOCKER_IMAGE"]
    assert config.services == ("alpha", "beta", "gamma")
    assert config.with_registry_auth is True
    assert config.update_parallelism == 2
    assert config.update_delay == "5s"
    assert config.update_order == "start-first"
    assert config.extra_flags == ("--label-add", "stack=dev", "--env", "ROLLING=1")
    assert config.pull_timeout == 120.0
    assert config.update_timeout == 240.0


class _FakeExecutor:
    def __init__(
        self,
        fail_on: set[tuple[str, ...]] | None = None,
        scripted: dict[tuple[str, ...], CommandOutcome] | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], float | None, dict[str, str] | None]] = []
        self._fail_on = fail_on or set()
        self._scripted = scripted or {}

    async def __call__(
        self,
        args: list[str],
        timeout: float | None,
        env: Mapping[str, str] | None,
    ) -> CommandOutcome:
        captured_env = dict(env) if env else None
        cmd_tuple = tuple(args)
        self.calls.append((cmd_tuple, timeout, captured_env))
        if cmd_tuple in self._fail_on:
            raise DockerCommandError(command=cmd_tuple, exit_code=1, stdout="", stderr="boom")
        outcome = self._scripted.get(cmd_tuple)
        if outcome is not None:
            return outcome
        return CommandOutcome(
            command=cmd_tuple,
            stdout="ok",
            stderr="",
            duration=0.5,
            exit_code=0,
        )


@pytest.mark.anyio
async def test_manager_runs_pull_and_updates() -> None:
    env = {
        "MODBOT_DOCKER_IMAGE": "ghcr.io/example/modbot:main",
        "MODBOT_DOCKER_SERVICES": "alpha,beta",
        "MODBOT_DOCKER_WITH_REGISTRY_AUTH": "1",
        "MODBOT_DOCKER_EXTRA_FLAGS": "--label-add stack=dev",
        "MODBOT_DOCKER_DEPLOYMENT": "service",
    }
    config = DockerUpdateConfig.from_env(env)
    fake = _FakeExecutor()
    manager = DockerUpdateManager(config, executor=fake)

    report = await manager.run()

    expected = [
        (("docker", "pull", env["MODBOT_DOCKER_IMAGE"]), 300.0, None),
        (
            (
                "docker",
                "service",
                "update",
                f"--image={env['MODBOT_DOCKER_IMAGE']}",
                "--update-order=start-first",
                "--update-parallelism=1",
                "--update-delay=0s",
                "--with-registry-auth",
                "--label-add",
                "stack=dev",
                "alpha",
            ),
            600.0,
            None,
        ),
        (
            (
                "docker",
                "service",
                "update",
                f"--image={env['MODBOT_DOCKER_IMAGE']}",
                "--update-order=start-first",
                "--update-parallelism=1",
                "--update-delay=0s",
                "--with-registry-auth",
                "--label-add",
                "stack=dev",
                "beta",
            ),
            600.0,
            None,
        ),
    ]
    assert fake.calls == expected
    assert [result.service for result in report.services] == ["alpha", "beta"]
    assert report.rollout_mode.startswith("service | start-first")


def test_config_supports_docker_host_env() -> None:
    env = {
        "MODBOT_DOCKER_SERVICES": "alpha",
        "MODBOT_DOCKER_HOST": "ssh://deploy@example",
        "MODBOT_DOCKER_CONTEXT": "prod",
        "MODBOT_DOCKER_TLS_VERIFY": "1",
        "MODBOT_DOCKER_CERT_PATH": "/certs",
        "MODBOT_DOCKER_CONFIG": "/config",
    }
    config = DockerUpdateConfig.from_env(env)
    assert config.env == {
        "DOCKER_HOST": "ssh://deploy@example",
        "DOCKER_CONTEXT": "prod",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": "/certs",
        "DOCKER_CONFIG": "/config",
    }


@pytest.mark.anyio
async def test_manager_container_mode_sequence() -> None:
    env = {
        "MODBOT_DOCKER_DEPLOYMENT": "container",
        "MODBOT_DOCKER_CONTAINER_NAME": "moderator-bot",
        "MODBOT_DOCKER_CONTAINER_ARGS": "--env-file /srv/modbot/.env --network host --restart always",
    }
    config = DockerUpdateConfig.from_env(env)
    fake = _FakeExecutor()
    manager = DockerUpdateManager(config, executor=fake)

    report = await manager.run()

    expected_commands = [
        ("docker", "pull", config.image),
        ("docker", "stop", "moderator-bot"),
        ("docker", "rm", "moderator-bot"),
        (
            "docker",
            "run",
            "-d",
            "--name",
            "moderator-bot",
            "--env-file",
            "/srv/modbot/.env",
            "--network",
            "host",
            "--restart",
            "always",
            config.image,
        ),
    ]
    assert [call[0] for call in fake.calls] == expected_commands
    assert [result.service for result in report.services] == [
        "moderator-bot:stop",
        "moderator-bot:rm",
        "moderator-bot:run",
    ]


@pytest.mark.anyio
async def test_manager_container_mode_ignores_stop_failures() -> None:
    env = {
        "MODBOT_DOCKER_DEPLOYMENT": "container",
        "MODBOT_DOCKER_CONTAINER_NAME": "moderator-bot",
    }
    config = DockerUpdateConfig.from_env(env)
    fail_on = {
        ("docker", "stop", "moderator-bot"),
        ("docker", "rm", "moderator-bot"),
    }
    fake = _FakeExecutor(fail_on=fail_on)
    manager = DockerUpdateManager(config, executor=fake)

    report = await manager.run()

    stop_result, rm_result, run_result = report.services
    assert stop_result.outcome.exit_code == 1
    assert rm_result.outcome.exit_code == 1
    assert run_result.outcome.exit_code == 0


@pytest.mark.anyio
async def test_manager_auto_detects_service_mode() -> None:
    env: dict[str, str] = {
        "MODBOT_DOCKER_IMAGE": "ghcr.io/example/modbot:main",
        "MODBOT_DOCKER_SERVICES": "alpha",
    }
    config = DockerUpdateConfig.from_env(env)
    info_command = (
        "docker",
        "info",
        "--format",
        "{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}",
    )
    scripted = {
        info_command: CommandOutcome(command=info_command, stdout="active true", stderr="", duration=0.1, exit_code=0)
    }
    fake = _FakeExecutor(scripted=scripted)
    manager = DockerUpdateManager(config, executor=fake)

    await manager.run()

    commands = [call[0] for call in fake.calls]
    assert commands[0] == info_command
    assert any(cmd[:2] == ("docker", "service") for cmd in commands)


@pytest.mark.anyio
async def test_manager_auto_detects_container_mode_when_info_fails() -> None:
    env: dict[str, str] = {
        "MODBOT_DOCKER_IMAGE": "ghcr.io/example/modbot:main",
    }
    config = DockerUpdateConfig.from_env(env)
    info_command = (
        "docker",
        "info",
        "--format",
        "{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}",
    )
    fake = _FakeExecutor(fail_on={info_command})
    manager = DockerUpdateManager(config, executor=fake)

    await manager.run()

    commands = [call[0] for call in fake.calls]
    assert commands[0] == info_command
    assert any(cmd[:2] == ("docker", "run") for cmd in commands)


@pytest.mark.anyio
async def test_manager_auto_switches_to_service_when_inside_container(monkeypatch) -> None:
    env: dict[str, str] = {
        "MODBOT_DOCKER_IMAGE": "ghcr.io/example/modbot:main",
    }
    config = DockerUpdateConfig.from_env(env)
    info_command = (
        "docker",
        "info",
        "--format",
        "{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}",
    )
    scripted = {
        info_command: CommandOutcome(
            command=info_command,
            stdout="inactive false",
            stderr="",
            duration=0.1,
            exit_code=0,
        )
    }
    fake = _FakeExecutor(scripted=scripted)
    manager = DockerUpdateManager(config, executor=fake)
    monkeypatch.setattr("modules.devops.docker_update._is_running_in_container", lambda: True)

    await manager.run()

    commands = [call[0] for call in fake.calls]
    assert commands[0] == info_command
    assert any(cmd[:3] == ("docker", "service", "update") for cmd in commands)
    assert not any(cmd[:2] == ("docker", "run") for cmd in commands)


def test_format_update_report() -> None:
    pull = CommandOutcome(command=("docker", "pull", "img"), stdout="", stderr="", duration=1.25, exit_code=0)
    service_outcome = CommandOutcome(
        command=("docker", "service", "update", "alpha"),
        stdout="",
        stderr="",
        duration=2.0,
        exit_code=0,
    )
    report = UpdateReport(
        image="img",
        pull=pull,
        services=[ServiceUpdateResult(service="alpha", outcome=service_outcome)],
        rollout_mode="start-first, parallelism=1, delay=0s",
    )

    text = format_update_report(report)
    assert "Update complete" in text
    assert "alpha" in text
    assert "start-first" in text
