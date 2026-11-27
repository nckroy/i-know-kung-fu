# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Digital rain: a Matrix-style activity indicator for long-running work.

    with rain.activity("Scanning your sources") as r:
        result = run(cfg, plan)
        r.status("still scanning…")   # optional: change the status line

Columns of half-width katakana and digits fall down a small band of the terminal. Each drop
has a bright white-green head, the trail fades through green to black, and lit characters keep
mutating. The band is drawn on stderr, appears only after a short delay (quick operations don't
flicker), and is erased when the work finishes, leaving the terminal as it was.

It stays off when stderr isn't a terminal, or with ``KUNGFU_RAIN=0``. With ``NO_COLOR`` (or a
terminal without color) it falls back to a one-line spinner. ``KUNGFU_RAIN_ASCII=1`` uses
plain ASCII glyphs for fonts without katakana.
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator, TextIO

from kungfu.sensitivity import color_mode

KATAKANA = [chr(c) for c in range(0xFF66, 0xFF9E)]
DIGITS = list("0123456789")
SYMBOLS = list(":.=*+-<>¦|\"")
ASCII = list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$+-*/=%\"'#&_(),.;:?!\\|{}<>[]^~")

# Intensity (1.0 = head) to color, from the head's white-green through green to near black.
TRUECOLOR_STOPS = [
    (1.00, (225, 255, 225)),
    (0.85, (120, 255, 140)),
    (0.65, (0, 230, 65)),
    (0.45, (0, 170, 45)),
    (0.28, (0, 110, 30)),
    (0.14, (0, 60, 15)),
]
XTERM_GREENS = [(0.92, 194), (0.78, 120), (0.6, 46), (0.45, 40), (0.3, 34), (0.18, 28), (0.0, 22)]
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _glyphs() -> list[str]:
    if os.environ.get("KUNGFU_RAIN_ASCII"):
        return ASCII
    return KATAKANA * 3 + DIGITS * 2 + SYMBOLS


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def color_for(intensity: float, mode: str) -> str:
    if mode == "256":
        for threshold, code in XTERM_GREENS:
            if intensity >= threshold:
                return f"\x1b[38;5;{code}m"
        return "\x1b[38;5;22m"
    stops = TRUECOLOR_STOPS
    if intensity >= stops[0][0]:
        rgb = stops[0][1]
    else:
        rgb = stops[-1][1]
        for (hi, c_hi), (lo, c_lo) in zip(stops, stops[1:]):
            if lo <= intensity <= hi:
                rgb = _lerp(c_lo, c_hi, (intensity - lo) / (hi - lo))
                break
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


class Field:
    """The rain itself: a grid of glyphs with fading intensity, independent of any terminal."""

    def __init__(self, width: int, height: int, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.glyphs = _glyphs()
        self.resize(width, height)

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = max(1, width), max(1, height)
        self.chars = [[" "] * self.width for _ in range(self.height)]
        self.light = [[0.0] * self.width for _ in range(self.height)]
        # One drop per column: head row (float, may start above the band), speed in rows per frame.
        self.heads = [self.rng.uniform(-self.height * 2, 0) for _ in range(self.width)]
        self.speeds = [self.rng.uniform(0.25, 0.9) for _ in range(self.width)]
        # Fewer active columns looks more like rain than a curtain.
        self.active = [self.rng.random() < 0.3 for _ in range(self.width)]

    def step(self) -> None:
        rng = self.rng
        for y in range(self.height):
            row_l, row_c = self.light[y], self.chars[y]
            for x in range(self.width):
                if row_l[x] > 0:
                    row_l[x] = max(0.0, row_l[x] * 0.8 - 0.015)
                    # Lit characters keep transforming.
                    if row_l[x] > 0 and rng.random() < 0.06:
                        row_c[x] = rng.choice(self.glyphs)
        for x in range(self.width):
            if not self.active[x]:
                if rng.random() < 0.012:
                    self.active[x] = True
                    self.heads[x] = rng.uniform(-6, 0)
                    self.speeds[x] = rng.uniform(0.25, 0.9)
                continue
            before = int(self.heads[x])
            self.heads[x] += self.speeds[x]
            after = int(self.heads[x])
            for y in range(max(0, before), min(self.height, after + 1)):
                self.chars[y][x] = rng.choice(self.glyphs)
                self.light[y][x] = 1.0
            if self.heads[x] > self.height + 2:
                self.active[x] = rng.random() < 0.35
                self.heads[x] = rng.uniform(-self.height, 0)
                self.speeds[x] = rng.uniform(0.25, 0.9)

    def render(self, mode: str) -> list[str]:
        """One string per row, with color escapes. Cells too faint to see are blank."""
        lines = []
        for y in range(self.height):
            parts: list[str] = []
            current = ""
            for x in range(self.width):
                lvl = self.light[y][x]
                if lvl < 0.08:
                    parts.append(" ")
                    continue
                code = color_for(lvl, mode)
                if code != current:
                    parts.append(code)
                    current = code
                parts.append(self.chars[y][x])
            parts.append("\x1b[0m")
            lines.append("".join(parts))
        return lines


class Rain:
    FPS = 14

    def __init__(self, message: str, stream: TextIO | None = None, delay: float = 0.35,
                 height: int | None = None, force: bool = False) -> None:
        self.stream = stream or sys.stderr
        self.message = message
        self.delay = delay
        enabled = os.environ.get("KUNGFU_RAIN", "1").lower() not in ("0", "false", "no", "off")
        tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.enabled = enabled and (tty or force)
        # Without color the rain can't fade, so it falls back to a one-line spinner.
        self.mode = "truecolor" if force else color_mode(self.stream)
        rows = shutil.get_terminal_size((80, 24)).lines
        self.height = height or (6 if rows >= 20 else 3)
        self._status = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drawn = False
        self._lock = threading.Lock()
        self._start = time.monotonic()

    # --- public ------------------------------------------------------------------------------

    def status(self, text: str) -> None:
        self._status = text

    def __enter__(self) -> "Rain":
        if self.enabled:
            self._thread = threading.Thread(target=self._loop, name="kungfu-rain", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._erase()

    # --- drawing -----------------------------------------------------------------------------

    def _width(self) -> int:
        return max(10, min(shutil.get_terminal_size((80, 24)).columns, 160))

    def _status_line(self) -> str:
        elapsed = time.monotonic() - self._start
        text = self._status or self.message
        spin = SPINNER[int(elapsed * self.FPS) % len(SPINNER)]
        line = f" {spin} {text}  {elapsed:4.1f}s"
        line = line[: self._width() - 1]
        if self.mode == "none":
            return line
        return f"\x1b[1m{color_for(0.8, self.mode)}{line}\x1b[0m"

    def _write(self, s: str) -> None:
        try:
            self.stream.write(s)
            self.stream.flush()
        except (OSError, ValueError):
            self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(self.delay):
            return  # finished before it was worth showing anything
        width = self._width()
        field = Field(width, self.height) if self.mode != "none" else None
        rows = (self.height + 1) if field else 1
        with self._lock:
            # Reserve the band (scrolling if we're at the bottom), then return to its top.
            self._write("\x1b[?25l" + "\n" * rows + f"\x1b[{rows}A\r")
            self._drawn = True
            self._rows = rows
        while not self._stop.is_set():
            frame = []
            if field:
                w = self._width()
                if w != field.width:
                    field.resize(w, self.height)
                field.step()
                frame = [f"\x1b[2K{line}" for line in field.render(self.mode)]
            frame.append(f"\x1b[2K{self._status_line()}")
            with self._lock:
                if self._stop.is_set():
                    break
                self._write("\r" + "\n".join(frame) + (f"\x1b[{rows - 1}A\r" if rows > 1 else "\r"))
            self._stop.wait(1 / self.FPS)

    def _erase(self) -> None:
        with self._lock:
            if self._drawn:
                self._write("\r\x1b[J\x1b[?25h")
                self._drawn = False


@contextmanager
def activity(message: str, stream: TextIO | None = None) -> Iterator[Rain]:
    """Show digital rain while the body runs; also restores the cursor on Ctrl-C."""
    r = Rain(message, stream=stream)
    with r:
        yield r
