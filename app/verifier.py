"""Check every number in a generated summary against the JSON it was built from.

The generator constrains the model to numbers present in its input JSON. This
module is what makes that constraint real at read time: it extracts each number
from the prose, matches it against the values in the JSON within a rounding
tolerance, and reports per bullet.

Matching deliberately ignores presentation. "$371,924.01", "371924.01",
"$371.9K" and "-1.4%" all reduce to a float before comparison, because the
model is free to format and only the quantity matters.

Three things this module learned the hard way, all visible in the 2026-10-05
verification table before they were fixed:

* **The markdown list dash is not a minus sign.** "- $134,035.95" parsed as
  -134,035.95. List markers are stripped before extraction.
* **Matching used to ignore sign.** A magnitude fallback existed so "lost
  $2,055.19" could match a change_usd of -2055.19 - but it also let "+34.6%"
  flipped to "-34.6%" pass. Sign is now checked, and a direction word counts
  as a sign: "fell $1,638.12" asserts a negative even with no "-" written.
  A direction word governs only the FIRST number after it, so "down 1.4% from
  $377,378.06" signs the percentage and leaves the dollar figure unsigned.
* **A small integer matches almost anything.** Both "1"s in a bullet matched
  under_review_account_days, though the second was "1 item in the review
  queue". For values <= SMALL_INTEGER the field is chosen by the words around
  the number, and a figure with no contextual support is reported "low
  confidence" rather than "matched".

Tolerance has two parts. A relative tolerance absorbs legitimate rounding -
"$371.9K" against 371924.01 is correct to the precision written. An absolute
floor keeps small values from being rejected for a fraction of a cent. The
tolerance is derived from how precisely the number was WRITTEN: a figure given
to the dollar is held to the dollar, one given as "$371.9K" only to the
hundred. Being stricter than the text would reject correct roundings; being
looser would let a genuinely wrong number pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: A number as it might appear in prose: optional sign and currency, digit
#: groups, optional decimals, optional K/M/B or % suffix.
NUMBER = re.compile(
    r"""
    (?P<sign>[-+−])?\s*
    \$?\s*
    (?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*(?P<suffix>%|[KkMmBb](?![A-Za-z]))?
    """,
    re.VERBOSE,
)

SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

#: Numbers that are labels rather than quantities: a year, or a date fragment
#: inside an ISO date. These are skipped rather than matched.
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: A number used as a unit modifier - "4-week average", "7-day window" - is a
#: label, not a quantity. Reading the 4 as a figure made a correct sentence
#: fail against every week's aggregates.
UNIT_MODIFIER = re.compile(
    r"\d+\s*-\s*(?:week|day|month|year|quarter|hour|minute)s?\b", re.IGNORECASE)

#: Identifiers such as ACC00123. The digits inside one are not a quantity -
#: reading them as 123 produced spurious failures - but the identifier itself
#: still has to be real, so it is checked against the JSON's string values.
IDENTIFIER = re.compile(r"\b[A-Z]{2,}\d{3,}\b")

REL_TOLERANCE = 0.005      # half a percent, for figures written at full precision
ABS_FLOOR = 0.01           # one cent

#: At or below this, a bare integer is so common across the aggregates that a
#: numeric match proves nothing - the field has to be chosen from context.
SMALL_INTEGER = 10

#: How far from a number to read context words when choosing its field.
CONTEXT_CHARS = 55

#: A leading list marker: "- ", "* ", "1. ", bullet glyphs. Stripped so the
#: dash is never read as a minus sign.
LIST_MARKER = re.compile(r"^\s*(?:[-*\u2022\u2013\u2014]+|\d+[.)])\s+")

#: Movement words that assert a sign. Deliberately verbs and nouns of movement
#: only - "above" and "below" are comparatives and are excluded, because in
#: "$348,936.69, which is below the prior week's $358,801.04" the comparative
#: describes a relationship, not the sign of either figure.
SIGN_UP = {"rose", "rise", "rises", "risen", "riser", "risers", "up", "gained",
           "gain", "gains", "added", "adds", "increased", "increase", "grew",
           "growth", "climbed", "higher", "advanced"}
SIGN_DOWN = {"fell", "fall", "fallen", "faller", "fallers", "down", "lost",
             "loss", "losses", "declined", "decline", "dropped", "drop",
             "decreased", "decrease", "lower", "shrank", "contracted", "slipped"}


@dataclass
class NumberCheck:
    text: str
    value: float
    matched: bool
    matched_against: str | None = None
    #: "matched" | "low_confidence" | "unmatched". A low-confidence figure
    #: found a numeric equal in the aggregates but no field whose name fits the
    #: surrounding words - it is reported, not treated as proven.
    confidence: str = "matched"
    asserted_sign: int | None = None


@dataclass
class BulletResult:
    text: str
    numbers: list[NumberCheck] = field(default_factory=list)
    identifiers: list[NumberCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(n.matched for n in self.numbers + self.identifiers)

    @property
    def unmatched(self) -> list[str]:
        return [n.text for n in self.numbers + self.identifiers if not n.matched]

    @property
    def low_confidence(self) -> list[str]:
        return [n.text for n in self.numbers if n.confidence == "low_confidence"]


@dataclass
class VerifyResult:
    bullets: list[BulletResult]

    @property
    def ok(self) -> bool:
        return bool(self.bullets) and all(b.ok for b in self.bullets)

    @property
    def total_numbers(self) -> int:
        return sum(len(b.numbers) for b in self.bullets)

    @property
    def matched_numbers(self) -> int:
        return sum(1 for b in self.bullets for n in b.numbers if n.matched)

    @property
    def low_confidence_numbers(self) -> int:
        return sum(1 for b in self.bullets for n in b.numbers
                   if n.confidence == "low_confidence")

    @property
    def total_identifiers(self) -> int:
        return sum(len(b.identifiers) for b in self.bullets)

    @property
    def matched_identifiers(self) -> int:
        return sum(1 for b in self.bullets for n in b.identifiers if n.matched)


def flatten_strings(payload) -> set[str]:
    """Every string value in the JSON, for identifier checking."""
    out: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            out.add(node)
    walk(payload)
    return out


def flatten(payload) -> list[tuple[str, float]]:
    """Every numeric value in the JSON, with a dotted path for reporting."""
    out: list[tuple[str, float]] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            out.append((path, float(node)))
    walk(payload, "")
    return out


def _written_precision(digits: str, suffix: str | None) -> float:
    """Absolute tolerance implied by how the number was written.

    "371924.01" is written to the cent, so it is held to the cent. "371.9K" is
    written to the hundred, so it is held to the hundred. This is what lets a
    correct rounding pass without also letting a wrong number through.
    """
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    step = 10.0 ** (-decimals)
    if suffix and suffix.lower() in SCALE:
        step *= SCALE[suffix.lower()]
    return step / 2


def strip_list_marker(line: str) -> str:
    """Remove a leading list marker so its dash is not read as a minus."""
    return LIST_MARKER.sub("", line)


def _field_keywords(path: str) -> set[str]:
    """Words in a JSON path, singularised crudely, for context matching."""
    raw = re.split(r"[.\[\]_]+", path.lower())
    out = set()
    for token in raw:
        token = re.sub(r"\d+", "", token).strip()
        if len(token) < 3:
            continue
        out.add(token)
        if token.endswith("s"):
            out.add(token[:-1])
        else:
            out.add(token + "s")
    return out


def _variants(words) -> set[str]:
    out = set(words)
    out |= {w[:-1] for w in words if w.endswith("s")}
    out |= {w + "s" for w in words}
    return out


def _context_words(text: str, start: int, end: int) -> set[str]:
    """Words near a number, for choosing which field it refers to."""
    window = text[max(0, start - CONTEXT_CHARS): end + CONTEXT_CHARS].lower()
    return _variants(re.findall(r"[a-z_]+", window))


def _unit_words(text: str, end: int, count: int = 3) -> set[str]:
    """The first few words AFTER a number - usually its unit.

    "1 account-day" and "1 item in the review queue" are distinguished by what
    immediately follows the digit, not by what is in the sentence. A flat
    window scored both against every word nearby, and in a short sentence the
    earlier "account-day" won the second number too.
    """
    tail = text[end: end + 40].lower()
    return _variants(re.findall(r"[a-z_]+", tail)[:count])


def _context_score(keywords: set[str], context: set[str], units: set[str]) -> int:
    """Unit words weigh far more than merely nearby words."""
    return 4 * len(keywords & units) + len(keywords & context)


def extract_numbers(text: str) -> list[dict]:
    """Every number in the text, with its value, tolerance and asserted sign.

    A number's sign comes from an explicit +/- if one is written, otherwise
    from a movement word that governs it. A movement word governs only the
    FIRST number after it: in "down 1.4% from $377,378.06" the word signs the
    percentage and the dollar figure is left unsigned, because the prior week's
    revenue is not a negative quantity.
    """
    line = strip_list_marker(text)
    offset = len(text) - len(line)

    skip = [m.span() for m in ISO_DATE.finditer(line)]
    skip += [m.span() for m in UNIT_MODIFIER.finditer(line)]

    def inside_skip(start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e in skip)

    # Positions of movement words, so each can govern the next number only.
    governors: list[tuple[int, int]] = []   # (position, sign)
    for m in re.finditer(r"[A-Za-z]+", line):
        word = m.group(0).lower()
        if word in SIGN_UP:
            governors.append((m.start(), 1))
        elif word in SIGN_DOWN:
            governors.append((m.start(), -1))
    used: set[int] = set()

    found: list[dict] = []
    for m in NUMBER.finditer(line):
        d_start, d_end = m.span("digits")
        if inside_skip(d_start, d_end):
            continue
        if d_start > 0 and (line[d_start - 1].isalpha() or line[d_start - 1] == "_"):
            continue

        digits = m.group("digits")
        suffix = m.group("suffix")
        magnitude = float(digits.replace(",", ""))
        if suffix and suffix.lower() in SCALE:
            magnitude *= SCALE[suffix.lower()]

        explicit = m.group("sign")
        if explicit in {"-", "\u2212"}:
            sign: int | None = -1
        elif explicit == "+":
            sign = 1
        else:
            # Nearest unused movement word before this number governs it.
            sign = None
            for pos, direction in reversed(governors):
                if pos < d_start and pos not in used:
                    sign = direction
                    used.add(pos)
                    break

        found.append({
            "text": m.group(0).strip(),
            "magnitude": magnitude,
            "sign": sign,
            "value": magnitude if sign is None else magnitude * sign,
            "tolerance": _written_precision(digits, suffix),
            "start": d_start + offset,
            "end": d_end + offset,
        })
    return found


def verify(summary: str, input_json: str | dict) -> VerifyResult:
    payload = json.loads(input_json) if isinstance(input_json, str) else input_json
    candidates = flatten(payload)
    known_strings = flatten_strings(payload)
    keywords = {path: _field_keywords(path) for path, _ in candidates}

    bullets: list[BulletResult] = []
    for line in summary.splitlines():
        line = line.strip()
        if not line:
            continue
        result = BulletResult(text=line)
        for item in extract_numbers(line):
            result.numbers.append(
                _match_one(item, line, candidates, keywords)
            )
        for ident in IDENTIFIER.findall(line):
            present = any(ident in s for s in known_strings)
            result.identifiers.append(
                NumberCheck(ident, float("nan"), present,
                            "input_json" if present else None,
                            "matched" if present else "unmatched")
            )
        bullets.append(result)
    return VerifyResult(bullets=bullets)


def _match_one(item: dict, line: str, candidates, keywords) -> NumberCheck:
    """Match one extracted number against the aggregates.

    Sign is respected when the text asserts one, explicitly or through a
    movement word. When the text asserts nothing, either sign is accepted -
    "$134,035.95 is under review" says nothing about direction and should not
    be forced to.
    """
    magnitude = item["magnitude"]
    sign = item["sign"]
    tol = max(item["tolerance"], ABS_FLOOR, abs(magnitude) * REL_TOLERANCE)

    hits: list[tuple[str, float]] = []      # (path, distance)
    for path, candidate in candidates:
        if sign is None:
            distance = abs(abs(candidate) - magnitude)
        else:
            distance = abs(candidate - magnitude * sign)
        if distance <= tol:
            hits.append((path, distance))

    if not hits:
        return NumberCheck(item["text"], item["value"], False, None,
                           "unmatched", sign)

    context = _context_words(line, item["start"], item["end"])
    units = _unit_words(line, item["end"])
    scored = [
        (path, distance, _context_score(keywords[path], context, units))
        for path, distance in hits
    ]

    # A small integer equals too many things for a numeric match to mean
    # anything, so context decides the field and distance only breaks ties.
    is_small_int = abs(magnitude) <= SMALL_INTEGER and float(magnitude).is_integer()
    if is_small_int:
        scored.sort(key=lambda row: (-row[2], row[1], row[0]))
        path, _distance, score = scored[0]
        confidence = "low_confidence" if score == 0 else "matched"
        return NumberCheck(item["text"], item["value"], True, path,
                           confidence, sign)

    # For everything else the nearest value wins, with context as the tie-break.
    # The relative tolerance is wide enough for two real figures to collide -
    # $133,703.16 fell inside 0.5% of $134,035.95 - so "first hit wins" named
    # the wrong field while still reporting a correct match.
    scored.sort(key=lambda row: (row[1], -row[2], row[0]))
    path, _distance, _score = scored[0]
    return NumberCheck(item["text"], item["value"], True, path, "matched", sign)
