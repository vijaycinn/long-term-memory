"""lint.py — health-check the vault's *content*, not just its links.

validate.py answers "is the graph well-formed". This answers the harder
question the LLM-wiki pattern actually cares about: is what the vault says
still true, still coherent, and still complete?

Checks:
  contradictions  facts that assert opposing things about the same subject
  superseded      claims a newer source has overtaken
  stale           pages untouched while their subject kept moving
  coverage        concepts referenced often but with no page of their own
  thin            pages carrying almost no information
  unfiled         topics with facts but no synthesis anywhere

Findings are reported, never auto-fixed: resolving a contradiction is a
judgement about which claim is right, and that needs a human.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

_P = config.paths()
VAULT = _P["vault"]
MEMDB = _P["memory_db"]
NOW = datetime.now(timezone.utc)

NEGATION = re.compile(
    r"\b(not|never|no longer|cannot|can't|doesn't|does not|isn't|is not|"
    r"wrong|incorrect|false|superseded|deprecated|removed|reverted|"
    r"correction|corrected|instead of|rather than)\b", re.I)
CORRECTION = re.compile(
    r"\b(correction|corrected|supersedes?|superseded|was wrong|is wrong|"
    r"no longer true|update[sd]?\s+fact|replaces?)\b", re.I)


def read(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def pages():
    out = []
    for p in sorted(VAULT.rglob("*.md")):
        if ".obsidian" in p.parts:
            continue
        out.append((p, read(p)))
    return out


def load_facts():
    if not MEMDB.exists():
        return [], {}
    c = sqlite3.connect(f"file:{MEMDB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    facts = [dict(r) for r in c.execute(
        "select id, content, fact_type, importance, status, source, topic_id, "
        "created_at from facts")]
    topics = {r["id"]: dict(r) for r in c.execute("select * from topics")}
    c.close()
    return facts, topics


def content_words(text: str) -> set:
    stop = {"the", "and", "for", "with", "that", "this", "from", "have", "has",
            "was", "are", "not", "but", "you", "your", "all", "can", "will",
            "into", "when", "what", "which", "they", "them", "then", "than",
            "were", "been", "would", "could", "should", "there", "their"}
    return {w for w in re.findall(r"[a-z][a-z0-9-]{3,}", text.lower())
            if w not in stop}


def check_contradictions(facts, topics):
    """Facts within a topic that appear to disagree.

    Returns two confidence tiers. A *correction* — one fact explicitly saying it
    overturns something, while its near-twin does not — is a strong signal and
    almost always worth acting on. Bare negation asymmetry is weak: two facts
    can both be negative and agree ("GCC is NOT Azure Government" / "GCC !=
    Azure Government"), and symbol forms like `!=` dodge word matching
    entirely. Mixing the two tiers made the report noisy enough to ignore, so
    they are reported separately.
    """
    by_topic = defaultdict(list)
    for f in facts:
        if (f.get("status") or "active") != "active":
            continue
        by_topic[f.get("topic_id")].append(f)

    strong, weak = [], []
    for tid, group in by_topic.items():
        if len(group) < 2 or len(group) > 120:
            continue
        prepped = [(f, content_words(f["content"] or "")) for f in group]
        for i in range(len(prepped)):
            fa, wa = prepped[i]
            if len(wa) < 6:
                continue
            for j in range(i + 1, len(prepped)):
                fb, wb = prepped[j]
                if len(wb) < 6:
                    continue
                overlap = len(wa & wb) / max(1, min(len(wa), len(wb)))
                if overlap < 0.55:
                    continue
                ca = bool(CORRECTION.search(fa["content"]))
                cb = bool(CORRECTION.search(fb["content"]))
                rec = {"topic": (topics.get(tid) or {}).get("title", "(none)"),
                       "a": fa, "b": fb, "overlap": round(overlap, 2)}
                if ca ^ cb:
                    strong.append(rec)
                elif overlap >= 0.70 and (bool(NEGATION.search(fa["content"]))
                                          ^ bool(NEGATION.search(fb["content"]))):
                    weak.append(rec)
    return strong, weak


def check_superseded_language(facts):
    """Active facts that announce they overturn something.

    These are the most valuable lint hits: the correcting fact is live, but the
    claim it corrects is often still active too, so both are being served.
    """
    return [f for f in facts
            if (f.get("status") or "active") == "active"
            and CORRECTION.search(f.get("content") or "")]


def check_stale(pgs, days=120):
    stale = []
    cutoff = NOW - timedelta(days=days)
    for p, text in pgs:
        m = re.search(r'^modified:\s*"?(\d{4}-\d{2}-\d{2})"?\s*$', text[:1200], re.M)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d < cutoff:
            stale.append((p, m.group(1), (NOW - d).days))
    return sorted(stale, key=lambda x: -x[2])


def check_coverage(pgs, min_mentions=12):
    """Capitalised multi-word phrases mentioned often but with no page.

    A concept the vault keeps referring to but never defines is exactly the page
    that should exist next. Phrases that merely extend or contract an existing
    page name ("Kassie Crute" when "Kassie" exists) are dropped — those are the
    same subject, not a gap.
    """
    existing = {p.stem.lower() for p, _ in pgs}
    bare = {re.sub(r"^(topic|skill|library|artifacts) - ", "", e) for e in existing}
    known = existing | bare
    counts = Counter()
    for p, text in pgs:
        body = re.sub(r"\[\[[^\]]+\]\]", " ", text)
        body = re.sub(r"`[^`]+`", " ", body)
        body = re.sub(r"^\s*\|.*$", " ", body, flags=re.M)
        for phrase in re.findall(
                r"\b([A-Z][a-zA-Z0-9]+(?: [A-Z][a-zA-Z0-9]+){1,3})\b", body):
            if len(phrase) < 8:
                continue
            low = phrase.lower()
            if any(low == k or low in k or k in low for k in known):
                continue
            counts[phrase] += 1
    return [(k, v) for k, v in counts.most_common(60) if v >= min_mentions]


def check_thin(pgs, min_chars=320):
    thin = []
    for p, text in pgs:
        body = re.sub(r"^---.*?^---", "", text, flags=re.S | re.M)
        body = re.sub(r"^\s*(\[\[|>|#|-|\|).*$", "", body, flags=re.M)
        if len(body.strip()) < min_chars:
            thin.append((p, len(body.strip())))
    return sorted(thin, key=lambda x: x[1])


def check_unfiled(facts, topics):
    """Topics carrying real weight but never synthesised into an argument."""
    synth = {p.stem.lower() for p in (VAULT / "10-Synthesis").rglob("*.md")} \
        if (VAULT / "10-Synthesis").is_dir() else set()
    weight = Counter()
    for f in facts:
        if (f.get("importance") or 0) >= 5:
            weight[f.get("topic_id")] += 1
    out = []
    for tid, n in weight.most_common(25):
        t = topics.get(tid)
        if not t:
            continue
        title = (t.get("title") or "").lower()
        if n >= 5 and not any(title in s or s in title for s in synth):
            out.append((t.get("title"), n))
    return out


def main() -> int:
    if not VAULT.is_dir():
        print(f"vault not found: {VAULT}", file=sys.stderr)
        return 2
    pgs = pages()
    facts, topics = load_facts()
    print(f"vault: {len(pgs)} pages · memory: {len(facts)} facts / {len(topics)} topics\n")

    issues = 0

    contras, tension = check_contradictions(facts, topics)
    print(f"== contradictions — one fact corrects another ({len(contras)}) ==")
    print("  high confidence: one side explicitly announces a correction")
    for c in contras[:12]:
        print(f"\n  topic: {c['topic']}  (overlap {c['overlap']})")
        print(f"    A[{c['a']['id']}] {c['a']['content'][:200]}")
        print(f"    B[{c['b']['id']}] {c['b']['content'][:200]}")
    if not contras:
        print("  none detected")
    issues += len(contras)

    print(f"\n== possible tension — low confidence ({len(tension)}) ==")
    print("  near-identical facts where only one is phrased negatively; often a")
    print("  restatement rather than a real disagreement — skim, do not act blindly")
    for c in tension[:6]:
        print(f"\n  topic: {c['topic']}  (overlap {c['overlap']})")
        print(f"    A[{c['a']['id']}] {c['a']['content'][:150]}")
        print(f"    B[{c['b']['id']}] {c['b']['content'][:150]}")
    issues += len(tension)

    sup = check_superseded_language(facts)
    print(f"\n== facts announcing a correction ({len(sup)}) ==")
    print("  these overturn an earlier claim — check the claim they replace is "
          "marked superseded, not still active")
    for f in sup[:10]:
        print(f"  [{f['id']}] imp{f.get('importance')} {f['content'][:170]}")
    issues += len(sup)

    stale = check_stale(pgs)
    print(f"\n== stale pages, source older than 120d ({len(stale)}) ==")
    for p, d, age in stale[:10]:
        print(f"  {age:>4}d  {p.stem}  (source {d})")
    issues += len(stale)

    cov = check_coverage(pgs)
    print(f"\n== concepts mentioned often with no page ({len(cov)}) ==")
    for k, v in cov[:15]:
        print(f"  {v:>4}x  {k}")
    issues += len(cov)

    thin = check_thin(pgs)
    print(f"\n== thin pages ({len(thin)}) ==")
    for p, n in thin[:10]:
        print(f"  {n:>5} chars  {p.relative_to(VAULT).as_posix()}")
    issues += len(thin)

    unfiled = check_unfiled(facts, topics)
    print(f"\n== topics with load-bearing facts but no synthesis page ({len(unfiled)}) ==")
    for t, n in unfiled[:12]:
        print(f"  {n:>3} imp-5 facts  {t}")
    issues += len(unfiled)

    print(f"\n{issues} finding(s). Nothing was changed — lint reports, it does not fix.")
    print("File anything worth keeping: python wiki.py file-answer \"...\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
