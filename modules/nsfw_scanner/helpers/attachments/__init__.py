"""Attachment scanning helpers package.

Historically these helpers lived in a single module named
`modules.nsfw_scanner.helpers.attachments`. Tests and production code still
import from that location, so this package re-exports the public API from the
new `scanner` module to remain backwards compatible.
"""

from .scanner import *  # noqa: F401,F403
