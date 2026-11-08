"""Content-defined chunking. 16 KB average chunks beat 4 KB end to end in Phase 0: 4 KB packs were 0.7 MB
smaller on 700 MB of transcripts, but the chunk index grew by 3.8 MB. Boundaries depend on content only,
so a transcript that grew by a few lines re-chunks to the same chunks plus a new tail."""

from collections.abc import Iterator

import blake3
from fastcdc import fastcdc

AVG = 16_384


def digest(data: bytes) -> bytes:
    return blake3.blake3(data).digest()


def split(data: bytes, avg: int = AVG) -> Iterator[tuple[bytes, bytes]]:
    """(blake3 digest, chunk bytes) in order."""
    if not data:
        return
    for c in fastcdc(data, min_size=avg // 4, avg_size=avg, max_size=avg * 4, fat=True):
        yield digest(c.data), c.data
