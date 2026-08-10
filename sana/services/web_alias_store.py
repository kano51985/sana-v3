import json
import os
import time


class WebAliasStore:
    def __init__(self, file_path: str = "sana/data/web_aliases.json"):
        self.file_path = file_path

    def resolve(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        data = self._load()
        for canonical, entry in data.items():
            for alias in entry.get("aliases", []):
                if self._alias_match(alias, text):
                    return canonical
        return None

    def aliases_for(self, canonical: str) -> list[str]:
        data = self._load()
        entry = data.get(canonical, {})
        return list(entry.get("aliases", []))

    def add_learned(
        self,
        canonical: str,
        aliases: list[str],
        confidence: float,
        source_urls: list[str] | None = None,
    ) -> None:
        if not canonical or not aliases:
            return
        data = self._load()
        entry = data.setdefault(canonical, {"aliases": [], "source": "learned"})
        existing = entry.setdefault("aliases", [])
        for alias in aliases:
            alias = alias.strip()
            if alias and alias not in existing:
                existing.append(alias)
        entry["confidence"] = max(float(entry.get("confidence", 0) or 0), float(confidence))
        entry["source_urls"] = list(dict.fromkeys(source_urls or []))
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["learned_by_ai"] = True
        self._save(data)

    def _alias_match(self, alias: str, text: str) -> bool:
        normalized_alias = alias.strip().lower()
        normalized_text = text.lower()
        if not normalized_alias:
            return False
        if len(normalized_alias) == 1:
            return normalized_text == normalized_alias
        return normalized_alias in normalized_text

    def _load(self) -> dict:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.file_path)
