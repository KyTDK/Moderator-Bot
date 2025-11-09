import logging

from modules.nsfw_scanner.settings_keys import NSFW_IMAGE_CATEGORY_SETTING
from modules.utils import mysql

from .context import ImageProcessingContext, build_image_processing_context
from .image_batch import process_image_batch
from .image_io import (
    _PNG_PASSTHROUGH_EXTS,
    _PNG_PASSTHROUGH_FORMATS,
    _TRUNCATED_ERROR_MARKERS,
    _encode_image_to_png_bytes,
    _is_truncated_image_error,
    _open_image_from_bytes,
    _open_image_from_path,
    _prepare_loaded_image,
    _temporary_truncated_loading,
)
from .image_logging import (
    _format_image_log_details,
    _format_metadata_value,
    _get_file_size,
    _notify_image_open_failure,
    log as logging_log,
    log_developer_issue,
)
from .image_pipeline import _run_image_pipeline
from .image_processing import NSFW_CATEGORY_SETTING, process_image

log = logging.getLogger(__name__)

__all__ = [
    "NSFW_CATEGORY_SETTING",
    "ImageProcessingContext",
    "build_image_processing_context",
    "process_image",
    "process_image_batch",
    "log",
    "log_developer_issue",
    "_PNG_PASSTHROUGH_EXTS",
    "_PNG_PASSTHROUGH_FORMATS",
    "_TRUNCATED_ERROR_MARKERS",
    "_temporary_truncated_loading",
    "_prepare_loaded_image",
    "_is_truncated_image_error",
    "_open_image_from_path",
    "_open_image_from_bytes",
    "_encode_image_to_png_bytes",
    "_run_image_pipeline",
    "_get_file_size",
    "_format_metadata_value",
    "_format_image_log_details",
    "_notify_image_open_failure",
    "mysql",
]

# Maintain compatibility by exposing mysql dependency for existing test doubles.
mysql = mysql

# Expose logger from image_logging for callers expecting the previous module-level logger.
logging_log  # keep import for side effects / lint
