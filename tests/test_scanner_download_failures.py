import asyncio
import sys
from pathlib import Path

import aiohttp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

import tests.test_scanner_text  # noqa: F401 - ensures dependency stubs are registered
import modules.nsfw_scanner.scanner as scanner_mod


def test_timeout_errors_are_suppressed():
    should_suppress = scanner_mod.NSFWScanner._should_suppress_download_failure
    assert should_suppress(asyncio.TimeoutError())


def test_404_client_response_errors_are_suppressed():
    should_suppress = scanner_mod.NSFWScanner._should_suppress_download_failure
    exc = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=404,
        message="Not Found",
        headers=None,
    )
    assert should_suppress(exc)


def test_non_timeout_network_errors_are_not_suppressed():
    should_suppress = scanner_mod.NSFWScanner._should_suppress_download_failure
    exc = aiohttp.ClientConnectorError(connection_key=None, os_error=OSError("boom"))
    assert not should_suppress(exc)

