from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class Translator:
    """Lightweight JSON-based i18n loader with automatic English fallback."""

    def __init__(self, locales_dir: Path, language: str = "en") -> None:
        self._locales_dir = locales_dir
        self._fallback: Dict[str, str] = self._load_file("en")
        self._language = language.lower().strip()
        self._messages: Dict[str, str] = (
            self._fallback
            if self._language == "en"
            else self._load_file(self._language)
        )

    def _load_file(self, lang: str) -> Dict[str, str]:
        path = self._locales_dir / f"{lang}.json"
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()}

    def t(self, key: str, **kwargs: Any) -> str:
        template = self._messages.get(key) or self._fallback.get(key) or key
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template