from modules.utils import time

class TimeString(str):
    def __new__(cls, value):
        if value is None or value == "":
            return super().__new__(cls, "")
        if time.parse_duration(value) is None:
            raise ValueError("Invalid duration format. Use formats like 20s, 30m, 2h, 30d, 2w, 5mo, 1y. Seconds, minutes, hours, days, weeks, months and years respectively.")
        return super().__new__(cls, value)