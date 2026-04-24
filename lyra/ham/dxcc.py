"""DXCC prefix → country lookup from cty.dat.

cty.dat is the standard ham-radio country-prefix database maintained by
country-files.com. Each record is:

    Country Name: CQzone: ITUzone: Continent: Lat: Lon: UTCoff: Primary:
        prefix,prefix,=fullcall,(zoneOverride),[ituOverride],<lat/lon>;

Modifiers: `=CALL` = exact-match whole callsign (e.g., DXpeditions).
`(N)`, `[N]`, `<lat/lon>`, `{Continent}` override metadata — we strip
them.

Usage:
    dxcc = DxccLookup(Path("data/cty.dat"))
    country = dxcc.country_of("JA1XYZ")      # "Japan"
    iso     = dxcc.iso_of("JA1XYZ")          # "JP"
    flag    = dxcc.flag_of("JA1XYZ")         # "🇯🇵"
    enriched = dxcc.enrich("N8SDR")          # "🇺🇸 N8SDR"
"""
from __future__ import annotations

import re
from pathlib import Path

from .country_iso import country_to_iso, iso_to_flag


_MOD_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|<[^>]*>|\{[^}]*\}|~[^~]*~")


class DxccLookup:
    def __init__(self, cty_dat_path: str | Path):
        self._path = Path(cty_dat_path)
        self._prefix_to_country: dict[str, str] = {}
        self._exact_to_country: dict[str, str] = {}
        self._loaded = False
        if self._path.exists():
            self._load()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ── Parsing ───────────────────────────────────────────────────────
    def _load(self):
        try:
            text = self._path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        # Records are separated by `;` (each spans multiple lines)
        for raw in text.split(";"):
            record = raw.strip()
            if not record:
                continue
            # Header: name + 7 colon-delimited fields, then prefixes section
            lines = record.split("\n")
            first = lines[0]
            fields = [f.strip() for f in first.split(":")]
            if len(fields) < 8:
                continue
            country = fields[0]
            # Everything after the header's 7 colon-separated fields may
            # contain prefixes on subsequent lines.
            # Join remaining lines and tokenize.
            body = " ".join(lines[1:]) if len(lines) > 1 else ""
            # First line also has the primary prefix in fields[7]
            all_prefixes = [fields[7]] + [
                p.strip() for p in body.split(",") if p.strip()
            ]
            for raw_pfx in all_prefixes:
                # Strip modifiers like (5), [8], <lat/lon>, {NA}
                cleaned = _MOD_RE.sub("", raw_pfx).strip()
                if not cleaned:
                    continue
                if cleaned.startswith("="):
                    # Exact-match entry
                    call = cleaned[1:].split("/")[0].strip().upper()
                    if call:
                        self._exact_to_country[call] = country
                else:
                    # Prefix entry — normalize
                    pfx = cleaned.strip(",").upper()
                    if pfx:
                        self._prefix_to_country[pfx] = country
        self._loaded = bool(self._prefix_to_country)

    # ── Lookup ────────────────────────────────────────────────────────
    def country_of(self, callsign: str) -> str:
        """Return the DXCC country name for `callsign`, or '' if unknown."""
        if not self._loaded or not callsign:
            return ""
        call = callsign.strip().upper()
        # Some spots have ops like "W1/N8SDR" — base is the part on the
        # "weirder" side (shorter after /, or the one with a digit in it).
        if "/" in call:
            parts = call.split("/")
            # Take the part that looks like a country override prefix:
            # if one part is 1-3 chars with no digits at the end, it's
            # usually the country prefix (e.g., "W1/VK3ABC" → VK3).
            call = max(parts, key=lambda s: (sum(c.isdigit() for c in s), len(s)))

        # Exact match first
        if call in self._exact_to_country:
            return self._exact_to_country[call]
        # Longest-prefix match
        for length in range(min(len(call), 6), 0, -1):
            pfx = call[:length]
            if pfx in self._prefix_to_country:
                return self._prefix_to_country[pfx]
        return ""

    def iso_of(self, callsign: str) -> str:
        return country_to_iso(self.country_of(callsign))

    def flag_of(self, callsign: str) -> str:
        return iso_to_flag(self.iso_of(callsign))

    def enrich(self, callsign: str) -> str:
        """Return the callsign prefixed with its flag, or unchanged if
        no country match. Uses a non-breaking space to keep flag + call
        as a single label."""
        flag = self.flag_of(callsign)
        if flag:
            return f"{flag} {callsign}"
        return callsign
