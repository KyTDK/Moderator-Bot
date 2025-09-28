from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import httpx
from crowdin_api import CrowdinClient

logger = logging.getLogger(__name__)

class CrowdinError(RuntimeError):
    """Base exception for Crowdin integration errors."""

class CrowdinConfigurationError(CrowdinError):
    """Raised when the Crowdin integration is misconfigured."""

class CrowdinBuildError(CrowdinError):
    """Raised when Crowdin build process fails or times out."""

@dataclass(frozen=True)
class CrowdinSettings:
    """Configuration container for the Crowdin integration."""

    token: str
    project_id: int
    locales_root: Path
    organization: Optional[str] = None
    branch: Optional[str] = None
    target_locales: Sequence[str] = ()
    poll_interval: float = 2.0
    poll_timeout: float = 300.0

    @classmethod
    def from_env(cls) -> "CrowdinSettings":
        """Build settings from environment variables."""

        token = os.getenv("CROWDIN_API_TOKEN") or os.getenv("CROWDIN_PERSONAL_TOKEN")
        if not token:
            raise CrowdinConfigurationError("Missing Crowdin API token in CROWDIN_API_TOKEN")

        project_id_raw = os.getenv("CROWDIN_PROJECT_ID")
        if not project_id_raw:
            raise CrowdinConfigurationError("Missing Crowdin project id in CROWDIN_PROJECT_ID")
        try:
            project_id = int(project_id_raw)
        except ValueError as exc:
            raise CrowdinConfigurationError("CROWDIN_PROJECT_ID must be an integer") from exc

        locales_root = Path(
            os.getenv("CROWDIN_LOCALES_DIR")
            or os.getenv("LOCALES_DIR")
            or "locales"
        ).resolve()

        organization = os.getenv("CROWDIN_ORGANIZATION")
        branch = os.getenv("CROWDIN_BRANCH")

        target_locales_raw = os.getenv("CROWDIN_TARGET_LOCALES") or os.getenv("CROWDIN_TARGET_LANGS")
        if target_locales_raw:
            target_locales: tuple[str, ...] = tuple(
                locale.strip() for locale in target_locales_raw.split(",") if locale.strip()
            )
        else:
            target_locales = ()

        poll_interval = float(os.getenv("CROWDIN_BUILD_POLL_INTERVAL", "2.0"))
        poll_timeout = float(os.getenv("CROWDIN_BUILD_TIMEOUT", "300.0"))

        return cls(
            token=token,
            project_id=project_id,
            locales_root=locales_root,
            organization=organization,
            branch=branch,
            target_locales=target_locales,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
        )

class CrowdinTranslationService:
    """Fetches, downloads, and unpacks translations from Crowdin."""

    def __init__(self, settings: CrowdinSettings, client: Optional[CrowdinClient] = None) -> None:
        self._settings = settings
        if client is not None:
            self._client = client
        else:
            self._client = CrowdinClient(
                token=settings.token,
                organization=settings.organization,
            )
        self._client.project_id = settings.project_id

    @property
    def settings(self) -> CrowdinSettings:
        return self._settings

    @property
    def client(self) -> CrowdinClient:
        return self._client

    def refresh_locales(self, destination: Optional[Path] = None) -> Path:
        """Synchronously download and unpack translations into *destination*."""

        target_path = Path(destination) if destination else self._settings.locales_root
        target_path = target_path.resolve()
        target_path.mkdir(parents=True, exist_ok=True)

        logger.debug("Refreshing Crowdin locales into %s", target_path)

        with tempfile.TemporaryDirectory(prefix="crowdin-download-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            archive_path = tmpdir_path / "translations.zip"
            extracted_path = tmpdir_path / "extracted"

            download_url = self._obtain_archive_url()
            self._download_archive(download_url, archive_path)
            self._unpack_archive(archive_path, extracted_path)
            self._sync_directory(extracted_path, target_path)

        logger.info("Crowdin locales refreshed at %s", target_path)
        return target_path

    async def refresh_locales_async(self, destination: Optional[Path] = None) -> Path:
        """Asynchronously refresh locales by running the sync variant in a thread."""

        return await asyncio.to_thread(self.refresh_locales, destination)

    def _obtain_archive_url(self) -> str:
        build_id = self._build_project_translations()
        response = self._client.translations.download_project_translations(buildId=build_id)
        download_url = response["data"].get("url")
        if not download_url:
            raise CrowdinError("Crowdin did not return a download url for translations build")
        return download_url

    def _build_project_translations(self) -> int:
        branch_id = self._resolve_branch_id(self._settings.branch)
        request_data: dict[str, object] = {}
        if branch_id is not None:
            request_data["branchId"] = branch_id
        if self._settings.target_locales:
            request_data["targetLanguageIds"] = list(self._settings.target_locales)

        logger.debug(
            "Building Crowdin translations for project %s (branch=%s, targets=%s)",
            self._settings.project_id,
            self._settings.branch,
            self._settings.target_locales,
        )
        response = self._client.translations.build_project_translation(request_data=request_data)
        build_id = int(response["data"]["id"])
        self._wait_for_build(build_id)
        return build_id

    def _wait_for_build(self, build_id: int) -> None:
        deadline = time.monotonic() + self._settings.poll_timeout
        while True:
            status_response = self._client.translations.check_project_build_status(buildId=build_id)
            status = status_response["data"].get("status")
            if status == "finished":
                logger.debug("Crowdin build %s finished", build_id)
                return
            if status == "failed":
                raise CrowdinBuildError(
                    f"Crowdin build {build_id} failed: {status_response['data']}"
                )
            if time.monotonic() >= deadline:
                raise CrowdinBuildError(
                    f"Timed out waiting for Crowdin build {build_id} to finish"
                )
            time.sleep(self._settings.poll_interval)

    def _resolve_branch_id(self, branch_name: Optional[str]) -> Optional[int]:
        if not branch_name:
            return None

        logger.debug("Resolving Crowdin branch id for %s", branch_name)

        resource = self._client.source_files
        fetcher = resource.with_fetch_all()
        for page in fetcher.list_project_branches(name=branch_name):
            data = page["data"]
            if data.get("name") == branch_name:
                return int(data["id"])

        raise CrowdinConfigurationError(f"Crowdin branch '{branch_name}' was not found")

    @staticmethod
    def _download_archive(url: str, destination: Path) -> None:
        logger.debug("Downloading Crowdin archive from %s", url)
        with httpx.Client(follow_redirects=True, timeout=None) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as target_file:
                    for chunk in response.iter_bytes():
                        target_file.write(chunk)

    @staticmethod
    def _unpack_archive(archive_path: Path, destination: Path) -> None:
        import zipfile

        logger.debug("Unpacking Crowdin archive %s", archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(destination)

    @staticmethod
    def _sync_directory(source: Path, destination: Path) -> None:
        logger.debug("Syncing extracted translations from %s to %s", source, destination)
        # Clean destination contents without removing the directory itself
        for existing in destination.iterdir():
            if existing.is_dir():
                shutil.rmtree(existing)
            else:
                existing.unlink()

        for path in source.iterdir():
            target = destination / path.name
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)

__all__ = [
    "CrowdinSettings",
    "CrowdinTranslationService",
    "CrowdinError",
    "CrowdinConfigurationError",
    "CrowdinBuildError",
]
