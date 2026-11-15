"""Small dependency-free console charts for live backend measurements."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .console_ui import color, supports_color


def _bar(value: float, maximum: float, width: int = 28) -> str:
    if maximum <= 0:
        return "-" * width
    filled = max(0, min(width, round((value / maximum) * width)))
    filled_text = "#" * filled
    if supports_color():
        filled_text = color(filled_text, "\x1b[96m")
    return filled_text + "." * (width - filled)


def latency_graph(
    pairs: Sequence[tuple[float, float]], *, title: str = "LIVE LATENCY GRAPH"
) -> str:
    """Render before/after latency pairs as a stable-width ASCII chart."""
    if not pairs:
        return ""
    maximum = max(0.001, *(value for pair in pairs for value in pair))
    lines = [f"\n{title} (ms)", "                 baseline                       post-action"]
    for index, (baseline, post) in enumerate(pairs, start=1):
        lines.append(
            f"trial {index:>2}  {_bar(baseline, maximum)} {baseline:>8.3f}  "
            f"{_bar(post, maximum)} {post:>8.3f}"
        )
    return "\n".join(lines)


def policy_graph(rows: Sequence[tuple[str, float, float]]) -> str:
    """Render policy hit/success-rate and I/O-duration comparison rows."""
    if not rows:
        return ""
    maximum_io = max(0.001, *(duration for _name, _hit_rate, duration in rows))
    lines = ["\nLIVE POLICY COMPARISON", "policy  hit/success rate                I/O time (ms)"]
    for name, hit_rate, duration in rows:
        lines.append(
            f"{name:<6} {_bar(hit_rate, 1.0, 20)} {hit_rate:>5.1%}  "
            f"{_bar(duration, maximum_io, 20)} {duration:>8.3f}"
        )
    return "\n".join(lines)


def dqn_progress_graph(records: Sequence[dict[str, Any]], *, width: int = 36) -> str:
    """Show real per-step rewards collected while DQN training runs."""
    if not records:
        return ""
    rewards = [float(record["reward"]) for record in records]
    window = rewards[-width:]
    low, high = min(window), max(window)
    if high == low:
        chart = "=" * len(window)
    else:
        alphabet = " .:-=+*#%@"
        chart = "".join(
            alphabet[round((reward - low) / (high - low) * (len(alphabet) - 1))]
            for reward in window
        )
    latest = records[-1]
    return (
        "\nLIVE DQN REWARD GRAPH\n"
        f"steps {max(1, len(records) - len(window) + 1)}-{len(records)}: {chart}\n"
        f"latest action={latest['action_name']} reward={float(latest['reward']):+.4f} "
        f"loss={_format_loss(latest.get('loss'))}"
    )


def query_history_graph(rows: Iterable[dict[str, Any]], *, limit: int = 9) -> str:
    """Render recent stored query durations, newest measurement first."""
    recent = list(rows)[:limit]
    if not recent:
        return ""
    maximum = max(0.001, *(float(row["duration_ms"]) for row in recent))
    lines = ["\nORACLE QUERY HISTORY (latest first)"]
    for row in recent:
        duration = float(row["duration_ms"])
        lines.append(
            f"{str(row['statement_name'])[:24]:<24} "
            f"{_bar(duration, maximum, 24)} {duration:>8.3f} ms "
            f"rows={int(row['rows_returned'])}"
        )
    return "\n".join(lines)


def _format_loss(loss: object) -> str:
    return "n/a" if loss is None else f"{float(loss):.5f}"
