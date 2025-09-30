from modules.utils import time
from modules.utils.localization import LocalizedError


class TimeString(str):
    def __new__(cls, value):
        if value is None or value == "":
            return super().__new__(cls, "")
        if time.parse_duration(value) is None:
            raise LocalizedError(
                "modules.variables.time_string.invalid_format",
                "Invalid duration format. Use formats like 20s, 30m, 2h, 30d, 2w, 5mo, 1y. Seconds, minutes, hours, days, weeks, months and years respectively.",
            )
        return super().__new__(cls, value)
