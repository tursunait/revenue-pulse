"""Claim checks: the assertions a summary makes that numbers alone cannot test.

The numeric verifier (verifier.py) answers "is every figure in this text present
in the source aggregates". A summary can pass that completely and still be
wrong, because a true number can carry a false claim. The case that prompted
this module:

    "SMB account ACC00008 fell $13,991.95 (-95.8%) to $606.41 and is worth a
     churn check."

Every figure there is correct and present in the JSON. The claim is not:
ACC00008 runs at roughly $550-620 a week, and its prior week read $14,598 only
because a planted unit error multiplied one product by 1000. The fall is the
return to normal. A churn check is the opposite of what the data says.

Four checks, each one a claim type that survived numeric verification:

  direction        a direction word must match the sign of the change
  role             a named account must hold the role the text gives it, and a
                   product or segment called the leader must actually lead
  rebound          a mover whose PRIOR week was under review must not be given
                   a commercial explanation
  unsupported      no superlative the JSON cannot support, and no claim that
                   movement is real merely because nothing was flagged
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

IDENTIFIER = re.compile(r"\b[A-Z]{2,}\d{3,}\b")

UP_WORDS = {"rose", "up", "gained", "added", "increased", "grew", "growth",
            "climbed", "higher", "gains", "rise", "riser", "risers"}
DOWN_WORDS = {"fell", "down", "lost", "declined", "dropped", "decreased",
              "decline", "lower", "fall", "faller", "fallers", "losses",
              "shrank", "contracted"}

#: Commercial readings of a move. Harmless normally; wrong when the move is a
#: data-quality rebound.
COMMERCIAL_WORDS = {"churn", "churning", "cancel", "cancelled", "cancellation",
                    "lost customer", "leaving", "attrition", "downgrade",
                    "upsell", "expansion", "new business", "won", "win",
                    "retention", "at risk", "at-risk"}

#: Words that show the text has already attributed the move to data quality.
REBOUND_WORDS = {"unit_error", "unit error", "data-quality", "data quality",
                 "rebound", "return to normal", "returned to normal",
                 "under review", "reconciliation", "duplicate", "duplicate_load",
                 "artefact", "artifact", "correction", "corrected", "normalis",
                 "normaliz", "inflated", "overstated", "distorted"}

#: Superlatives the aggregates cannot support. The movers lists are top five by
#: DOLLAR change, so no percentage ranking and no global ranking is visible.
UNSUPPORTED_SUPERLATIVES = [
    "sharpest percentage", "biggest percentage", "largest percentage",
    "highest percentage", "greatest percentage", "steepest percentage",
    "fastest growing", "fastest-growing", "fastest growth",
    "sharpest jump", "sharpest rise", "sharpest drop", "sharpest fall",
    "most of any", "best performing", "best-performing",
    "worst performing", "worst-performing",
]

#: Claims that movement is genuine. Only a problem when the evidence offered is
#: the absence of a flag.
REALITY_CLAIMS = [
    "reflects actual consumption", "reflects real consumption",
    "reflects genuine", "is genuine", "are genuine", "genuine demand",
    "real demand", "actual demand", "truly organic", "is organic",
    "reflects true", "reflects actual usage", "real growth", "real movement",
]

SEVERITY_ORDER = {"error": 0, "warning": 1}


@dataclass
class ClaimIssue:
    check: str
    severity: str          # "error" blocks sending; "warning" is advisory
    bullet_index: int
    quote: str
    detail: str


def _known_accounts(payload: dict) -> set[str]:
    """Every account id anywhere in the payload.

    An account can legitimately be named without being a mover - the
    page-tier items name the account a reconciliation break or unit error
    belongs to. Only a MOVER claim about a non-mover is an error.
    """
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            found.update(IDENTIFIER.findall(node))
    walk(payload)
    return found


def _movers(payload: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for direction, key in (("up", "top_movers_up"), ("down", "top_movers_down")):
        for row in payload.get(key, []) or []:
            row = dict(row)
            row["direction"] = direction
            out[str(row.get("account_id"))] = row
    return out


#: How close a direction word has to be to an identifier to be read as a claim
#: about it. "led by ACC00072 ... so treat the headline growth as unconfirmed"
#: put "growth" in the same clause as an account it said nothing about.
PROXIMITY_CHARS = 60

#: Thousands separators are not clause boundaries. Protected before splitting
#: rather than guarded with a lookaround, because "$133,703.16, so treat..."
#: has a digit before the real boundary comma and a lookbehind on \d kept it.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_PLACEHOLDER = "\x00"


def _clauses(text: str) -> list[str]:
    """Split a bullet into clauses so a direction word is attributed to the
    account it actually sits beside, not to another one in the same sentence."""
    protected = _THOUSANDS.sub(_PLACEHOLDER, text)
    parts = re.split(r";|,|\bwhile\b|\bwhereas\b|\bbut\b|\bso\b", protected)
    return [p.replace(_PLACEHOLDER, ",").strip() for p in parts if p and p.strip()]


#: A signed amount sitting just after an account - "ACC00155 (Enterprise,
#: -$873.05)" - is the strongest statement of direction in these sentences, and
#: unlike a direction word it cannot be stolen by a neighbouring clause.
ADJACENT_SIGNED = re.compile(r"\(?\s*[^()]{0,24}?(?P<sign>[-+−])\s*\$")
ADJACENT_WINDOW = 44


def _asserted_direction(text: str, account: str) -> tuple[bool, bool]:
    """(says_up, says_down) for this account, from the text around it.

    Two signals, in order of reliability:

    1. A signed amount immediately after the account. This is how the model
       actually writes movers, and it is unambiguous.
    2. Failing that, a direction word nearby - but only within the segment the
       account sits in, where segments break on "and", sentence punctuation and
       the usual conjunctions.

    An account named bare in a list - "the other four fallers were A, B, C and
    D" - therefore gets no direction check, because the word that governs it
    sits in an earlier segment. That is deliberate: reaching back for it is how
    four of the five false positives in this module's history happened.

    The fallback used to read the whole comma-delimited clause, which split
    "(Enterprise, +$1,638.12)" down the middle and let the "riser" belonging to
    one account attach to the next one in the sentence. A correct summary was
    rejected for it.
    """
    pos = text.find(account)
    if pos < 0:
        return False, False

    after = text[pos + len(account): pos + len(account) + ADJACENT_WINDOW]
    m = ADJACENT_SIGNED.search(after)
    if m:
        negative = m.group("sign") in {"-", "−"}
        return (not negative), negative

    # Fallback: the account's own segment only. Sentence boundaries count -
    # without them, "the other four fallers were ... ACC00193, all Enterprise.
    # The largest riser was ACC00123" put a faller in the riser's segment and
    # the account was reported as rising.
    segments = re.split(
        r"\band\b|;|[.!?]|\bbut\b|\bwhile\b|\bwhereas\b|\bso\b", text)
    for segment in segments:
        if account in segment:
            words = _words(segment)
            return bool(words & UP_WORDS), bool(words & DOWN_WORDS)
    return False, False


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z_]+", text.lower()))


def _contains_any(text: str, phrases) -> str | None:
    low = text.lower()
    for phrase in phrases:
        if phrase in low:
            return phrase
    return None


def check_claims(summary: str, input_json: str | dict) -> list[ClaimIssue]:
    payload = json.loads(input_json) if isinstance(input_json, str) else input_json
    movers = _movers(payload)
    issues: list[ClaimIssue] = []

    known = _known_accounts(payload)
    bullets = [b.strip() for b in summary.splitlines() if b.strip()]
    for i, bullet in enumerate(bullets, start=1):
        _check_direction_and_role(bullet, i, movers, known, payload, issues)
        _check_rebound(bullet, i, movers, issues)
        _check_unsupported(bullet, i, payload, issues)

    issues.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.bullet_index))
    return issues


def _check_direction_and_role(bullet, index, movers, known, payload, issues) -> None:
    for clause in _clauses(bullet):
        for account in IDENTIFIER.findall(clause):
            says_up, says_down = _asserted_direction(bullet, account)
            claims_mover_role = says_up or says_down
            mover = movers.get(account)
            if mover is None:
                if account not in known:
                    issues.append(ClaimIssue(
                        "role", "error", index, account,
                        f"{account} does not appear anywhere in the source "
                        "aggregates.",
                    ))
                elif claims_mover_role:
                    issues.append(ClaimIssue(
                        "role", "error", index, clause.strip(),
                        f"{account} is given a mover role here, but it is not in "
                        "top_movers_up or top_movers_down - it appears in the "
                        "aggregates only as a data-quality item.",
                    ))
                # Otherwise it is named as a data-quality item, which is fine.
                continue
            change = mover.get("change_usd") or 0.0
            if says_up and not says_down and change < 0:
                issues.append(ClaimIssue(
                    "direction", "error", index, clause.strip(),
                    f"{account} is described as rising, but change_usd is "
                    f"{change:,.2f}.",
                ))
            if says_down and not says_up and change > 0:
                issues.append(ClaimIssue(
                    "direction", "error", index, clause.strip(),
                    f"{account} is described as falling, but change_usd is "
                    f"{change:,.2f}.",
                ))

    # Leader claims about a product or segment.
    for field, label in (("revenue_by_product", "product"),
                         ("revenue_by_segment", "segment")):
        table = payload.get(field) or {}
        if not table:
            continue
        leader = max(table, key=table.get)
        pattern = re.compile(
            r"\b([A-Za-z_]+)\b[^.;]{0,40}\b(?:led|leads|leading|largest|top)\b"
            rf"[^.;]{{0,30}}\b{label}\b", re.IGNORECASE)
        for m in pattern.finditer(bullet):
            named = m.group(1)
            if named.lower() in {"the", "by", "and", "at", "was", "is"}:
                continue
            if named.lower() not in {k.lower() for k in table}:
                continue
            if named.lower() != leader.lower():
                issues.append(ClaimIssue(
                    "role", "error", index, m.group(0).strip(),
                    f"{named} is called the leading {label}, but {leader} is "
                    f"the largest in {field}.",
                ))


def _check_rebound(bullet, index, movers, issues) -> None:
    explained = _contains_any(bullet, REBOUND_WORDS)
    for account in IDENTIFIER.findall(bullet):
        mover = movers.get(account)
        if not mover:
            continue
        prior = float(mover.get("prior_week_under_review_usd") or 0.0)
        if prior <= 0:
            continue
        issue_type = mover.get("prior_week_issue_type") or "a data-quality issue"
        commercial = _contains_any(bullet, COMMERCIAL_WORDS)
        if commercial:
            issues.append(ClaimIssue(
                "rebound", "error", index, bullet,
                f"{account} had ${prior:,.2f} under review in its prior week "
                f"({issue_type}), so this move is largely a return to normal - "
                f"but the text gives it a commercial reading ('{commercial}').",
            ))
        elif not explained:
            issues.append(ClaimIssue(
                "rebound", "warning", index, bullet,
                f"{account} had ${prior:,.2f} under review in its prior week "
                f"({issue_type}). The move should be described as a "
                "data-quality rebound; the text does not say so.",
            ))


def _check_unsupported(bullet, index, payload, issues) -> None:
    hit = _contains_any(bullet, UNSUPPORTED_SUPERLATIVES)
    if hit:
        issues.append(ClaimIssue(
            "unsupported", "error", index, hit,
            f"'{hit}' is a ranking the aggregates cannot support: the movers "
            "lists are the top five by dollar change, with no percentage "
            "ranking and no view of accounts outside them.",
        ))

    claim = _contains_any(bullet, REALITY_CLAIMS)
    if claim:
        nothing_flagged = (
            float(payload.get("under_review_usd") or 0.0) == 0.0
            and not (payload.get("page_tier_items") or [])
        )
        if nothing_flagged:
            issues.append(ClaimIssue(
                "unsupported", "error", index, claim,
                f"'{claim}' treats the absence of a flag as proof the movement "
                "is real. The checks cover specific known failure modes, not "
                "every possible fault.",
            ))
