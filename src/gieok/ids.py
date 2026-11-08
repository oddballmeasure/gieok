"""ULIDs: 48-bit millisecond time + 80 random bits, Crockford base32. Sortable by creation time."""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_ALPHABET[(value >> (5 * i)) & 31] for i in reversed(range(26)))
