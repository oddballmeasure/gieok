"""The compression ladder, as measured in Phase 0 (see NOTES.md):

- notes:    zstd-19 with a trained dictionary per kind (4.7x on held-out notes with 64 KB)
- edits:    zstd-19 with the parent version as a raw-content prefix (20.6x; 8x smaller than whole versions)
- packs:    zstd-19 long-window over a whole pack of unique chunks (14.1x Claude, 20.8x Codex transcripts)

Every encoded body starts with nothing extra: the codec name and dictionary id live next to it in the
database, so a body is exactly one zstd frame."""

import zstandard as zstd

LEVEL = 19
DICT_SIZE = 65_536
MIN_TRAIN = 24  # samples before a dictionary is worth training
MAX_DELTA_CHAIN = 8  # every 8th version is stored whole, so a read never decodes a long chain
_PLAIN = zstd.ZstdCompressionParameters.from_level(LEVEL)
_LONG = zstd.ZstdCompressionParameters.from_level(LEVEL, enable_ldm=True, window_log=27, threads=-1)


def plain(data: bytes) -> bytes:
    return zstd.ZstdCompressor(compression_params=_PLAIN).compress(data)


def with_dict(data: bytes, dict_bytes: bytes) -> bytes:
    d = zstd.ZstdCompressionDict(dict_bytes)
    return zstd.ZstdCompressor(compression_params=_PLAIN, dict_data=d).compress(data)


def delta(data: bytes, parent: bytes) -> bytes:
    d = zstd.ZstdCompressionDict(parent, dict_type=zstd.DICT_TYPE_RAWCONTENT)
    return zstd.ZstdCompressor(compression_params=_PLAIN, dict_data=d).compress(data)


def long_pack(data: bytes) -> bytes:
    return zstd.ZstdCompressor(compression_params=_LONG).compress(data)


def decode(body: bytes, codec: str, dict_bytes: bytes | None = None, parent: bytes | None = None) -> bytes:
    if codec == "zstd":
        return zstd.ZstdDecompressor().decompress(body)
    if codec == "dict":
        return zstd.ZstdDecompressor(dict_data=zstd.ZstdCompressionDict(dict_bytes)).decompress(body)
    if codec == "delta":
        d = zstd.ZstdCompressionDict(parent, dict_type=zstd.DICT_TYPE_RAWCONTENT)
        return zstd.ZstdDecompressor(dict_data=d).decompress(body)
    if codec == "pack":
        return zstd.ZstdDecompressor(max_window_size=2**27).decompress(body)
    raise ValueError(f"unknown codec {codec}")


def train(samples: list[bytes], size: int = DICT_SIZE) -> bytes | None:
    """A dictionary from note bodies, or None when there are too few samples to beat plain zstd."""
    if len(samples) < MIN_TRAIN:
        return None
    total = sum(map(len, samples))
    try:
        return zstd.train_dictionary(min(size, max(4096, total // 4)), samples).as_bytes()
    except zstd.ZstdError:
        return None
