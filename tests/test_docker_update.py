from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from modules.devops.docker_update import (  # noqa: E402
    CommandOutcome,
    DEFAULT_IMAGE,
    DockerUpdateConfig,
    DockerUpdateManager,
    ServiceUpdateResult,
    UpdateReport,
    format_update_report,
)

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_config_uses_default_image_when_missing() -> None:
    env = {"MODBOT_DOCKER_SERVICES": "alpha"}
    config = DockerUpdateConfig.from_env(env)
    assert config.image == DEFAULT_IMAGE


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
    def __init__(self) -> None:
        self.commands: list[tuple[tuple[str, ...], float | None]] = []

    async def __call__(self, args: list[str], timeout: float | None) -> CommandOutcome:
        self.commands.append((tuple(args), timeout))
        return CommandOutcome(
            command=tuple(args),
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
    }
    config = DockerUpdateConfig.from_env(env)
    fake = _FakeExecutor()
    manager = DockerUpdateManager(config, executor=fake)

    report = await manager.run()

    expected = [
        (("docker", "pull", env["MODBOT_DOCKER_IMAGE"]), 300.0),
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
        ),
    ]
    assert fake.commands == expected
    assert [result.service for result in report.services] == ["alpha", "beta"]
    assert report.rollout_mode.startswith("start-first")


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
