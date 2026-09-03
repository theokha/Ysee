from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

from yc_monitor.models import Alert, AlertKind, CanonicalItem, Classification, Source

FIRST_PERSON = re.compile(r"\b(i|i'm|i’ve|i've|we|we're|we’ve|we've|our|my)\b", re.IGNORECASE)
ACCEPTANCE = re.compile(
    r"\b(got into|accepted (?:in)?to|backed by|joining|selected for|part of)\b", re.IGNORECASE
)
PROGRAM = re.compile(r"\b(yc|y combinator|speedrun)\b", re.IGNORECASE)
BATCH = re.compile(r"\b(?:YC\s*)?(S|W|F)\s?\d{2}\b|\bspeedrun(?:\s+batch)?\b", re.IGNORECASE)
REJECT = re.compile(
    r"\b(applied|applying|application|interview|rejected|wish me luck|hiring|we're hiring|"
    r"is hiring|inspired by|congratulations to|reports that)\b",
    re.IGNORECASE,
)
COMPANY_PATTERNS = (
    re.compile(r"(?:building|launching|working on|company is)\s+([A-Z][\w.-]*(?:\s+[A-Z][\w.-]*){0,3})"),
    re.compile(r"(?:at|with)\s+([A-Z][\w.-]*(?:\s+[A-Z][\w.-]*){0,3})"),
)
STRONG_SIMILARITY = 0.92
MIN_FUZZY_CHARS = 8


def normalize_company(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\b(inc|incorporated|llc|ltd|labs?|ai|corp|company)\b", "", value, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()


def company_aliases(name: str | None, payload: dict[str, Any] | None = None) -> set[str]:
    aliases: set[str] = set()
    primary = normalize_company(name)
    if primary:
        aliases.add(primary)
    if not payload:
        return aliases
    former = payload.get("former_names") or payload.get("formerNames") or []
    if isinstance(former, str):
        former = [former]
    if isinstance(former, list):
        for value in former:
            if isinstance(value, str):
                normalized = normalize_company(value)
                if _usable_alias(normalized, primary):
                    aliases.add(normalized)
    slug = payload.get("slug")
    if isinstance(slug, str):
        normalized = normalize_company(slug.replace("-", " "))
        if _usable_alias(normalized, primary):
            aliases.add(normalized)
    return aliases


def matches_official_name(company: str | None, official_names: set[str]) -> bool:
    """Exact normalized match, plus token similarity only when both names are strong."""
    normalized = normalize_company(company)
    if not normalized:
        return False
    if normalized in official_names:
        return True
    tokens = normalized.split()
    if len(tokens) < 2 or len(normalized) < MIN_FUZZY_CHARS:
        return False
    for official in official_names:
        official_tokens = official.split()
        if len(official_tokens) >= 2 and len(official) >= MIN_FUZZY_CHARS:
            if _strong_token_match(tokens, official_tokens, normalized, official):
                return True
        # A one-word official name is usually a distinctive brand ("nori",
        # "stripe"), so its appearance as a whole token of a longer candidate
        # ("nori robotics") is strong evidence of the same company. Generic
        # words are excluded; short official names must clear a higher bar.
        elif official_tokens and _distinctive_token(official) and official in tokens:
            return True
    return False


def _distinctive_token(word: str) -> bool:
    """A one-word official name distinctive enough to anchor a subset match."""
    return len(word) >= 4 and word not in NON_DISTINCTIVE_TOKENS


# Words too generic to identify a company when they appear inside a longer
# candidate name. Kept lowercase; matched against normalized tokens.
NON_DISTINCTIVE_TOKENS = frozenset({
    "about", "access", "applied", "apps", "bank", "base", "basic", "beta",
    "block", "brand", "build", "business", "capital", "care", "cash", "center",
    "chief", "clean", "cloud", "code", "commerce", "common", "connect",
    "contact", "content", "control", "core", "craft", "create", "credit",
    "data", "deal", "deals", "design", "desk", "dev", "digital", "direct",
    "domain", "drive", "dynamic", "earth", "engine", "energy", "events",
    "every", "exchange", "express", "factory", "finance", "find", "first",
    "flow", "focus", "food", "forge", "found", "free", "fresh", "front",
    "future", "global", "goal", "goes", "graph", "green", "group", "growth",
    "guide", "hack", "hand", "health", "hello", "help", "home", "house",
    "ideas", "index", "inner", "input", "inside", "insight", "invest",
    "issue", "items", "joint", "journey", "keep", "know", "labs", "land",
    "legal", "level", "light", "lines", "links", "local", "logic", "long",
    "look", "love", "made", "magic", "mail", "make", "market", "master",
    "matter", "media", "meet", "member", "metrics", "minds", "mobile",
    "model", "money", "month", "music", "nature", "network", "next", "note",
    "notes", "number", "offer", "office", "one", "online", "open", "order",
    "output", "pace", "page", "parts", "path", "pay", "people", "phone",
    "photo", "place", "plan", "point", "power", "press", "price", "prime",
    "print", "private", "product", "profile", "project", "public", "pure",
    "quality", "quantum", "quest", "quick", "ready", "real", "record",
    "red", "rent", "research", "resort", "retail", "right", "rise", "robot",
    "rocket", "root", "route", "safe", "sales", "sample", "scale", "scan",
    "science", "search", "secure", "seed", "send", "server", "service",
    "share", "shift", "ship", "shop", "signal", "simple", "site", "smart",
    "social", "soft", "solar", "sound", "space", "spark", "special", "speed",
    "spot", "stack", "stage", "stand", "start", "state", "store", "story",
    "study", "style", "suite", "super", "supply", "support", "sure", "switch",
    "sync", "system", "talk", "task", "team", "tech", "terms", "test", "text",
    "think", "time", "tools", "top", "total", "touch", "track", "trade",
    "trail", "train", "travel", "true", "trust", "type", "unit", "value",
    "venture", "video", "view", "vision", "voice", "wallet", "want", "watch",
    "water", "wave", "ways", "week", "white", "works", "world", "write",
    "yield", "young", "zone",
})


def extract_company(text: str) -> str | None:
    for pattern in COMPANY_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip(" .,!")
    return None


def classify_social(
    item: CanonicalItem,
    official_names: set[str],
    official_hosts: set[str],
    official_handles: set[str],
) -> Classification:
    text = item.content_text.strip()
    if item.item_id.startswith("company:"):
        return Classification(None, "linkedin_company_page_seen_without_acceptance_post", 0.0)
    if not text:
        return Classification(None, "empty_content", 0.0)
    if item.founder_handle and item.founder_handle.lower() in official_handles:
        return Classification(None, "founder_already_official", 0.0)
    if REJECT.search(text):
        return Classification(None, "excluded_intent", 0.0)
    if not FIRST_PERSON.search(text) or not ACCEPTANCE.search(text) or not PROGRAM.search(text):
        return Classification(None, "missing_first_person_acceptance_signal", 0.2)

    company = item.company_name or extract_company(text)
    item.company_name = company
    if matches_official_name(company, official_names):
        return Classification(None, "company_already_official", 0.0)
    host = _host(item.company_url)
    if host and host in official_hosts:
        return Classification(None, "website_already_official", 0.0)

    confidence = 0.75
    if BATCH.search(text):
        confidence += 0.1
    if company:
        confidence += 0.1
    normalized = normalize_company(company) if company else ""
    if not normalized:
        normalized = f"founder-{item.founder_handle or item.item_id}"
    alert = Alert(
        AlertKind.EARLY_FOUNDER,
        item,
        f"early:{normalized}",
        min(confidence, 1.0),
        reason="founder_self_announcement",
    )
    return Classification(alert, "founder_self_announcement", alert.confidence)


def official_alert(
    item: CanonicalItem,
    *,
    upgrade_from: str | None = None,
    upgrade_note: str | None = None,
) -> Alert:
    if item.source == Source.YC_SPEEDRUN:
        kind = AlertKind.OFFICIAL_SPEEDRUN
        prefix = "speedrun"
        key_value = normalize_company(item.company_name)
    elif item.source == Source.YC_LAUNCHES:
        kind = AlertKind.OFFICIAL_YC
        prefix = "launch"
        key_value = item.item_id
    else:
        kind = AlertKind.OFFICIAL_YC
        prefix = "yc"
        key_value = item.item_id
    return Alert(
        kind,
        item,
        f"{prefix}:{key_value}",
        upgrade_from=upgrade_from,
        upgrade_note=upgrade_note,
    )


def _host(value: str | None) -> str | None:
    if not value:
        return None
    host = urlparse(value if "://" in value else f"https://{value}").hostname
    return host.lower().removeprefix("www.") if host else None


def _usable_alias(alias: str, primary: str) -> bool:
    if not alias or alias == primary:
        return False
    # Extra aliases must be multi-token so generic one-word names never join the set.
    return len(alias.split()) >= 2


def _strong_token_match(
    candidate_tokens: list[str],
    official_tokens: list[str],
    candidate: str,
    official: str,
) -> bool:
    shorter, longer = (
        (candidate_tokens, official_tokens)
        if len(candidate_tokens) <= len(official_tokens)
        else (official_tokens, candidate_tokens)
    )
    if len(shorter) >= 2 and set(shorter) <= set(longer) and any(len(token) >= 4 for token in shorter):
        return True
    return SequenceMatcher(None, candidate, official).ratio() >= STRONG_SIMILARITY
