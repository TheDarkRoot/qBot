from __future__ import annotations

from typing import Optional

from telegram.client import escape_html


def format_size(num_bytes: int | float) -> str:
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def format_speed(bytes_per_sec: int | float) -> str:
    return f"{format_size(bytes_per_sec)}/s"


def format_eta(seconds: int) -> str:
    if seconds < 0 or seconds >= 8_640_000:
        return "∞"
    if seconds == 0:
        return "0s"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def progress_bar(progress: float, width: int = 10) -> str:
    clamped = max(0.0, min(1.0, float(progress)))
    filled = int(round(clamped * width))
    return "▓" * filled + "░" * (width - filled)


def truncate(text: str, max_len: int = 52) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


_STATE_LABELS = {
    "downloading": "⬇️ Downloading",
    "forcedDL": "⏬ Forced DL",
    "metaDL": "🔎 Metadata",
    "stalledDL": "⏳ Stalled DL",
    "queuedDL": "🕒 Queued DL",
    "checkingDL": "🔍 Checking",
    "allocating": "💾 Allocating",
    "uploading": "⬆️ Seeding",
    "forcedUP": "⏫ Forced UP",
    "stalledUP": "🌱 Seeding (idle)",
    "queuedUP": "🕒 Queued UP",
    "checkingUP": "🔍 Checking",
    "pausedDL": "⏸ Paused",
    "stoppedDL": "⏸ Stopped",
    "pausedUP": "✅ Completed",
    "stoppedUP": "✅ Completed",
    "error": "❌ Error",
    "missingFiles": "⚠️ Missing Files",
    "moving": "📦 Moving",
    "checkingResumeData": "🔍 Checking",
}


def format_state(state: str) -> str:
    return _STATE_LABELS.get(state, f"ℹ️ {escape_html(state)}")


def is_paused_state(state: str) -> bool:
    return state in ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP")


def safe_category(category: Optional[str]) -> str:
    return escape_html(category) if category else "—"