from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
import re


class Serializer(ABC):

    @abstractmethod
    def dump(self, value): pass

    @abstractmethod
    def load(self, serialized): pass


class DateTimeSerializer(Serializer):
    """
    Serializer for DateTime objects
    """

    DATETIME_REGEX = "(?P<Y>\d{2,4})-(?P<m>\d{2})-(?P<d>\d{2})" + \
                     "[T ]" + \
                     "(?P<H>\d{2}):(?P<M>\d{2})(:(?P<S>\d{2}))?(\.(?P<f>\d+))?" + \
                     "(?P<tz>([\+-]\d{2}:?\d{2})|[Zz])?"

    DATETIME_RE = re.compile(DATETIME_REGEX)


    def dump(self, value):
        return value.isoformat() + 'Z'

    def load(self, serialized):
        match = self.DATETIME_RE.match(serialized)
        if not match:
            raise ValueError("Could not parse DateTime: '{}'".format(serialized))
        parts = match.groupdict()
        dt = datetime(
            int(parts["Y"]), int(parts["m"]), int(parts["d"]),
            int(parts["H"]), int(parts["M"]), int(parts.get("S") or 0), int(parts.get("f") or 0),
            tzinfo=self._parse_tzinfo(parts["tz"])
        )
        return dt

    def _parse_tzinfo(self, offset_str):
        if offset_str and offset_str.upper() == 'Z':
            return timezone.utc
        elif offset_str:
            hours = int(offset_str[:3])
            minutes = int(offset_str[-2:])
            # Invert minutes sign if hours == 0
            if offset_str[0] == "-" and hours == 0:
                minutes = -minutes
            return timezone(timedelta(hours=hours, minutes=minutes))
        else:
            return None