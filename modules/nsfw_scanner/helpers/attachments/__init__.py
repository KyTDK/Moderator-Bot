"""Attachment scanning helpers package.

Historically these helpers lived in a single module named
`modules.nsfw_scanner.helpers.attachments`. Tests and production code still
import from that location, so this package re-exports the public API from the
new modular layout to remain backwards compatible.
"""

from .cache import AttachmentSettingsCache
from .ocr import wait_for_async_ocr_tasks
from .scanner import check_attachment

__all__ = ["AttachmentSettingsCache", "check_attachment", "wait_for_async_ocr_tasks"]
