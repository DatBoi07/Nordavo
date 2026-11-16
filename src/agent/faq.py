"""Parse faq.md into structured entries with allowlists for verification."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

FAQ_PATH = os.path.join("data", "faq.md")


@dataclass
class FAQEntry:
    id: str          # e.g. "F01"
    title: str       # e.g. "Forgotten password"
    text: str        # full text of the entry
    urls: list[str] = field(default_factory=list)
    euro_amounts: list[str] = field(default_factory=list)
    menu_paths: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)


def _extract_urls(text: str) -> list[str]:
    return re.findall(r"(?:https?://)?[\w.-]+\.(?:nl|com|de|eu)(?:/[\w.-]*)*", text)


def _extract_euro_amounts(text: str) -> list[str]:
    return re.findall(r"€\d+(?:[.,]\d+)?", text)


def _extract_menu_paths(text: str) -> list[str]:
    """Extract menu paths like 'Team → Medewerker toevoegen (Add employee)'."""
    # Match sequences with → arrows
    paths = re.findall(r"[\w\s]+(?:\s*→\s*[\w\s]+)+(?:\s*\([^)]+\))?", text)
    return [p.strip() for p in paths if "→" in p]


def _extract_numbers(text: str) -> list[str]:
    """Extract standalone numbers (times, counts, etc.)."""
    return re.findall(r"\b\d+\b", text)


class FAQStore:
    """Parsed FAQ entries with lookup and allowlist support."""

    def __init__(self, faq_path: str | None = None):
        self.path = faq_path or FAQ_PATH
        self.entries: dict[str, FAQEntry] = {}
        self.rules_text: str = ""
        self._full_text: str = ""
        self._parse()

    def _parse(self):
        with open(self.path, "r", encoding="utf-8") as f:
            content = f.read()
        self._full_text = content

        # Split by ## headers
        sections = re.split(r"^## ", content, flags=re.MULTILINE)

        for section in sections[1:]:  # skip preamble
            lines = section.strip().split("\n", 1)
            header = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""

            # Check if it's an FAQ entry (F01, F02, etc.)
            faq_match = re.match(r"(F\d{2})\s*[·:]\s*(.*)", header)
            if faq_match:
                faq_id = faq_match.group(1)
                title = faq_match.group(2).strip()
                entry = FAQEntry(
                    id=faq_id,
                    title=title,
                    text=body,
                    urls=_extract_urls(body),
                    euro_amounts=_extract_euro_amounts(body),
                    menu_paths=_extract_menu_paths(body),
                    numbers=_extract_numbers(body),
                )
                self.entries[faq_id] = entry
            elif "rules for the support team" in header.lower():
                self.rules_text = body

    def get(self, faq_ids: list[str]) -> list[FAQEntry]:
        """Return the entries for the given IDs."""
        return [self.entries[fid] for fid in faq_ids if fid in self.entries]

    def get_text(self, faq_ids: list[str]) -> str:
        """Return combined text of the given FAQ entries."""
        entries = self.get(faq_ids)
        parts = []
        for e in entries:
            parts.append(f"## {e.id} · {e.title}\n{e.text}")
        return "\n\n".join(parts)

    def full_faq_text(self) -> str:
        """Return the full FAQ text for the classifier prompt."""
        return self._full_text

    def all_ids(self) -> list[str]:
        return list(self.entries.keys())

    def build_allowlist(self, faq_ids: list[str]) -> dict[str, set[str]]:
        """Build allowlists of URLs, amounts, menu paths from cited entries."""
        entries = self.get(faq_ids)
        allowlist: dict[str, set[str]] = {
            "urls": set(),
            "euro_amounts": set(),
            "menu_paths": set(),
        }
        for e in entries:
            allowlist["urls"].update(e.urls)
            allowlist["euro_amounts"].update(e.euro_amounts)
            allowlist["menu_paths"].update(e.menu_paths)
        return allowlist
