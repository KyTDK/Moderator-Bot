from modules.utils import time

class TimeString(str):
    def __new__(cls, value):
        if value is None or value == "":
            return super().__new__(cls, "")
        if time.parse_duration(value) is None:
            raise ValueError(f"Invalid time string: {value}")
        return super().__new__(cls, value)