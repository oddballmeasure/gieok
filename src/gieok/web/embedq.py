"""Query embeddings for hybrid search, without keeping the ~750 MB model in the site's process.

The first search starts a worker (`python -m gieok.web.embedq`) that loads the model and answers
one query per line on stdin with a JSON vector. It exits after IDLE_S without a query. Without
the `embed` extra installed, search falls back to keyword only and says so."""

import asyncio
import importlib.util
import json
import sys

import numpy as np

IDLE_S = 300


def available() -> bool:
    return importlib.util.find_spec("fastembed") is not None


class QueryEmbedder:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._idle: asyncio.TimerHandle | None = None

    async def _start(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "gieok.web.embedq", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 20)
        return self._proc

    async def embed(self, text: str) -> np.ndarray | None:
        if not available():
            return None
        async with self._lock:
            proc = await self._start()
            proc.stdin.write(json.dumps(text).encode() + b"\n")
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), 120)
            if self._idle:
                self._idle.cancel()
            self._idle = asyncio.get_running_loop().call_later(IDLE_S, self.close)
        return np.array(json.loads(line), dtype=np.float32) if line else None

    def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
        self._proc = None


def _worker() -> None:
    from gieok.embed import Embedder

    model = Embedder()
    for line in sys.stdin:
        vec = model([json.loads(line)])[0]
        sys.stdout.write(json.dumps([round(float(x), 6) for x in vec]) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    _worker()
