"""PNG graph output for IntelliPage console experiments.

Pillow is used so the packaged executable can create image graphs without a
browser or a separate plotting runtime. Files are overwritten on each update,
so the same image acts as a live chart during a running experiment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont


_BACKGROUND = (18, 24, 38)
_PANEL = (30, 41, 59)
_GRID = (71, 85, 105)
_TEXT = (226, 232, 240)
_MUTED = (148, 163, 184)
_BLUE = (56, 189, 248)
_GREEN = (74, 222, 128)
_ORANGE = (251, 146, 60)
_PURPLE = (192, 132, 252)
_RED = (248, 113, 113)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("segoeui.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _canvas(title: str, width: int = 1100, height: int = 620) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=18, fill=_PANEL)
    draw.text((52, 42), title, fill=_TEXT, font=_font(28))
    return image, draw


def _save(image: Image.Image, path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG", optimize=True)
    return target


def open_graph(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    try:
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
    except OSError:
        pass
    return target


def save_latency_graph(
    pairs: Sequence[tuple[float, float]], path: str | Path, *, title: str = "Live latency"
) -> Path:
    image, draw = _canvas(title)
    left, top, right, bottom = 100, 130, 1040, 530
    maximum = max(1.0, *(value for pair in pairs for value in pair)) if pairs else 1.0
    draw.line((left, top, left, bottom), fill=_GRID, width=2)
    draw.line((left, bottom, right, bottom), fill=_GRID, width=2)
    for index in range(5):
        y = bottom - (bottom - top) * index / 4
        draw.line((left, y, right, y), fill=_GRID, width=1)
        draw.text((42, y - 9), f"{maximum * index / 4:.1f}", fill=_MUTED, font=_font(14))
    count = max(1, len(pairs))
    group = (right - left) / count
    for index, (baseline, post) in enumerate(pairs, start=1):
        x = left + group * (index - 0.5)
        base_y = bottom - (baseline / maximum) * (bottom - top)
        post_y = bottom - (post / maximum) * (bottom - top)
        draw.line((x - 12, bottom, x - 12, base_y), fill=_ORANGE, width=14)
        draw.line((x + 12, bottom, x + 12, post_y), fill=_GREEN, width=14)
        draw.text((x - 18, bottom + 12), str(index), fill=_MUTED, font=_font(14))
    draw.text((left, 555), "Trial", fill=_MUTED, font=_font(15))
    draw.text((right - 210, 555), "orange=baseline  green=post", fill=_MUTED, font=_font(15))
    return _save(image, path)


def save_policy_graph(
    rows: Sequence[tuple[str, float, float]], path: str | Path, *, title: str = "Policy comparison"
) -> Path:
    image, draw = _canvas(title)
    left, top, right, bottom = 150, 130, 1040, 530
    maximum = max(1.0, *(float(row[1]) for row in rows)) if rows else 1.0
    group = (bottom - top) / max(1, len(rows))
    draw.line((left, top, left, bottom), fill=_GRID, width=2)
    for index, (name, rate, duration) in enumerate(rows):
        y = top + group * (index + 0.5)
        bar = (right - left) * min(1.0, max(0.0, float(rate)) / maximum)
        draw.text((54, y - 15), str(name), fill=_TEXT, font=_font(20))
        draw.rounded_rectangle((left, y - 18, left + bar, y + 18), radius=8, fill=_BLUE if name != "DQN" else _PURPLE)
        draw.text((left + bar + 12, y - 14), f"{float(rate):.1%}   {float(duration):.2f} ms", fill=_TEXT, font=_font(17))
    draw.text((left, 555), "bar=hit/success rate; label=rate and mean I/O/action time", fill=_MUTED, font=_font(15))
    return _save(image, path)


def save_dqn_graph(records: Sequence[dict[str, Any]], path: str | Path, *, title: str = "DQN training progress") -> Path:
    image, draw = _canvas(title)
    left, top, right, bottom = 90, 130, 1040, 530
    rewards = [float(row.get("reward", 0.0)) for row in records]
    if not rewards:
        draw.text((left, 250), "No DQN training records yet", fill=_MUTED, font=_font(22))
        return _save(image, path)
    low, high = min(-1.0, min(rewards)), max(1.0, max(rewards))
    draw.line((left, top, left, bottom), fill=_GRID, width=2)
    draw.line((left, bottom, right, bottom), fill=_GRID, width=2)
    points = []
    for index, reward in enumerate(rewards, start=1):
        x = left + (right - left) * (index - 1) / max(1, len(rewards) - 1)
        y = bottom - (reward - low) / (high - low) * (bottom - top)
        points.append((x, y))
    if len(points) > 1:
        draw.line(points, fill=_PURPLE, width=4)
    for x, y in points:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=_PURPLE)
    draw.text((42, top - 8), f"{high:.2f}", fill=_MUTED, font=_font(14))
    draw.text((42, bottom - 8), f"{low:.2f}", fill=_MUTED, font=_font(14))
    draw.text((left, 555), "step", fill=_MUTED, font=_font(15))
    return _save(image, path)


def save_query_graph(
    rows: Iterable[dict[str, Any]], path: str | Path, *, title: str = "Oracle query history"
) -> Path:
    values = list(rows)
    image, draw = _canvas(title)
    left, top, right, bottom = 230, 130, 1040, 530
    maximum = max(1.0, *(float(row.get("duration_ms", 0.0)) for row in values)) if values else 1.0
    group = (bottom - top) / max(1, len(values))
    for index, row in enumerate(values):
        duration = float(row.get("duration_ms", 0.0))
        y = top + group * (index + 0.5)
        bar = (right - left) * duration / maximum
        label = str(row.get("statement_name", "query"))[:28]
        draw.text((42, y - 13), label, fill=_TEXT, font=_font(15))
        draw.rounded_rectangle((left, y - 14, left + bar, y + 14), radius=6, fill=_BLUE)
        draw.text((left + bar + 10, y - 12), f"{duration:.3f} ms", fill=_TEXT, font=_font(15))
    return _save(image, path)
