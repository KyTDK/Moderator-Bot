from collections import OrderedDict
import asyncio
from typing import Dict

MAX_CACHE = 120
_pending: Dict[int, list[asyncio.Future]] = OrderedDict()
_recent_payloads: OrderedDict[int, dict] = OrderedDict()

def get_pending(): return _pending
def get_recent(): return _recent_payloads

def trim():
    while len(_pending) > MAX_CACHE:
        _pending.popitem(last=False)
    while len(_recent_payloads) > MAX_CACHE:
        _recent_payloads.popitem(last=False)
