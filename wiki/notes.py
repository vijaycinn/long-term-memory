"""LTM wiki builder — note generation stage."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from vaultlib import (ARTIFACT_DIRS, BUILD, DRY, F, MAX_IMPORT_BYTES, MEMORY_ONLY,
                      NOW, ROOT, STAMP, VAULT, classify, clear_generated, fm,
                      link, load_memory, load_session_store, load_state,
                      read_text, scan, slugify, split_frontmatter, title_from,
                      write_note, WRITTEN)

# ---------------------------------------------------------------------------
# Alias registry — drives automatic cross-linking
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {}   # lowercase phrase -> note name
NOTE_TAGS: dict[str, set] = defaultdict(set)
BACKLINKS: dict[str, set] = defaultdict(set)

STOP_ALIASES = {"me", "self", "vijay", "microsoft", "azure", "core", "agents", "preflight"}
MAX_ALIAS_WORDS = 6

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'&.+-]*")


def register(alias: str, note: str):
    a = alias.strip().lower()
    if len(a) < 4 or a in STOP_ALIASES:
        return
    ALIASES.setdefault(a, note)


def find_related(text: str, self_note: str, limit: int = 24) -> list[str]:
    """Find aliases mentioned in `text`.

    Scanning one regex per alias is O(aliases x text) and dominated the build
    at ~320 aliases over ~470 notes. Instead the text is tokenised once and its
    n-grams are intersected with the alias table, which is O(text) and gives
    identical word-boundary semantics.
    """
    words = _WORD_RE.findall(text.lower())
    if not words:
        return []
    hits = Counter()
    n = len(words)
    for size in range(1, MAX_ALIAS_WORDS + 1):
        if size > n:
            break
        for i in range(n - size + 1):
            gram = " ".join(words[i:i + size])
            note = ALIASES.get(gram)
            if note is not None and note != self_note:
                hits[note] += 1
    return [n_ for n_, _ in hits.most_common(limit)]


def source_callout(rel: str, full: Path, extra: str = "") -> str:
    return (f"> [!info]- Source\n"
            f"> Workspace path: `{rel}`\n"
            f"> Absolute: `{full}`\n"
            f"> Last modified: {datetime.fromtimestamp(full.stat().st_mtime):%Y-%m-%d}"
            f"{extra}\n\n")


def clean_body(body: str) -> str:
    """Demote H1 so the note title is the only H1; neutralise stray wikilinks.

    Imported source files sometimes contain `[[...]]` inside code fences or
    inline code (template literals, regex, JSON). Left alone those become
    broken graph edges, so they are escaped back to literal text.
    """
    lines = body.splitlines()
    seen_h1 = False
    in_fence = False
    out = []
    for ln in lines:
        if re.match(r"^\s*(```|~~~)", ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if ln.startswith("# ") and not seen_h1 and not in_fence:
            seen_h1 = True
            continue  # first H1 becomes the note title
        if in_fence:
            ln = ln.replace("[[", "\u200b[\u200b[")
        else:
            # escape wikilinks inside inline code spans only
            ln = re.sub(r"`([^`]*?)\[\[([^`]*?)`",
                        lambda m: f"`{m.group(1)}\u200b[\u200b[{m.group(2)}`", ln)
        out.append(ln)
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
def build():
    mem = load_memory()
    state = load_state()
    if MEMORY_ONLY:
        md, artifacts, skillfiles = [], [], []
        removed = clear_generated(origin="memory")
        print(f"[reset] memory-only: removed {removed} memory-derived notes; "
              f"{len(state.get('notes', []))} workspace notes reused from cache")
        if not state.get("notes"):
            print("[warn] no cached inventory — run a full build first "
                  "(python build.py) or links to workspace notes will be dropped")
    else:
        md, artifacts, skillfiles = scan()
        removed = clear_generated()
        print(f"[reset] removed {removed} previously generated notes")
        print(f"[scan]  md={len(md)} artifacts={len(artifacts)} skills={len(skillfiles)}")

    topics = {t["id"]: t for t in mem["topics"]}
    facts_by_topic = defaultdict(list)
    for f_ in mem["facts"]:
        facts_by_topic[f_.get("topic_id")].append(f_)

    # ------------------------------------------------ pass 0: register aliases
    entity_notes, topic_notes, skill_notes = {}, {}, {}

    # Entity note names must be globally unique — Obsidian resolves [[links]] by
    # basename, so two entities sharing a name would create ambiguous edges.
    used_names: dict[str, str] = {}
    ACCOUNTISH = ("account", "customer", "org", "organization")
    for e in sorted(mem["entities"],
                    key=lambda x: (0 if (x.get("entity_type") or "").lower() in ACCOUNTISH else 1,
                                   x.get("name") or "")):
        nm = (e.get("name") or "").strip()
        if not nm or e.get("is_self"):
            continue
        etype = (e.get("entity_type") or "other").lower()
        base = slugify(nm)
        note = base
        if base in used_names:
            note = slugify(f"{nm} ({etype})")
            n = 2
            while note in used_names:
                note = slugify(f"{nm} ({etype} {n})")
                n += 1
        used_names[note] = nm
        entity_notes[e["id"]] = note
        if base == note:
            register(nm, note)

    # Subjects that documents are filed under. Account subjects get a real hub
    # note (reusing an existing entity note when one already covers them);
    # library subjects resolve to their generated library index.
    account_subjects: dict[str, list] = defaultdict(list)
    library_subjects: set[str] = set()
    for rel, full in md:
        domain, ntype, subj = classify(rel)
        if not subj:
            continue
        if domain == "accounts":
            account_subjects[subj].append((rel, full))
        elif domain == "reference":
            library_subjects.add(subj)

    doc_subjects: dict[str, str] = {}
    for subj in account_subjects:
        existing = next((n for n in used_names if n.lower() == slugify(subj).lower()), None)
        note = existing or slugify(subj)
        doc_subjects[subj] = note
        register(subj, note)
    for subj in library_subjects:
        register(subj, slugify(f"Library - {subj}"))

    if MEMORY_ONLY:
        # Reuse the subject and skill maps the last full build cached, so
        # memory-derived notes still link into workspace-derived ones.
        for subj, note in (state.get("doc_subjects") or {}).items():
            doc_subjects.setdefault(subj, note)
            register(subj, note)
        for alias, note in (state.get("aliases") or {}).items():
            register(alias, note)

    for t in mem["topics"]:
        nm = (t.get("title") or t.get("slug") or "").strip()
        if not nm:
            continue
        note = slugify(f"Topic - {nm}")
        topic_notes[t["id"]] = note
        register(nm, note)
        if t.get("slug"):
            register(t["slug"].replace("-", " "), note)

    if MEMORY_ONLY:
        skill_notes = dict(state.get("skill_notes") or {})
    for rel, full in skillfiles:
        sk = Path(rel).parent.name
        note = slugify(f"Skill - {sk}")
        skill_notes[sk] = note
        register(sk, note)

    # Link density is balanced globally at flush time by capping inbound
    # Related edges per note, so no stopword tuning is needed here.
    print(f"[alias] {len(ALIASES)} cross-link aliases registered")

    counts = Counter()

    # ------------------------------------------------ 1. entity notes
    for e in mem["entities"]:
        nm = (e.get("name") or "").strip()
        if not nm or e.get("is_self"):
            continue
        etype = (e.get("entity_type") or "other").lower()
        if etype in ("account", "customer", "org", "organization"):
            folder = "accounts"
        elif etype in ("product", "service", "tool"):
            folder = "tech"
        else:
            folder = "internal"
        attrs = {}
        try:
            attrs = json.loads(e.get("attributes") or "{}")
        except Exception:
            attrs = {}
        note = entity_notes[e["id"]]

        body = [fm(title=nm, type="entity", entity_type=etype,
                   tags=[f"entity/{etype}", "people"], generated=True,
                   created=(e.get("created_at") or "")[:10],
                   updated=(e.get("updated_at") or "")[:10],
                   source="memory.db:entities")]
        body.append(f"# {nm}\n")
        body.append(f"> [!abstract] {etype.title()} record from long-term memory\n")
        if attrs:
            body.append("\n| Attribute | Value |\n|---|---|")
            for k, v in attrs.items():
                body.append(f"| {k} | {str(v)[:200]} |")
            body.append("")
        if e.get("notes"):
            body.append(f"\n## Notes\n\n{e['notes']}\n")

        # facts mentioning this entity
        rel_facts = [f_ for f_ in mem["facts"]
                     if re.search(r"(?<![\w-])" + re.escape(nm.lower()) + r"(?![\w-])",
                                  (f_.get("content") or "").lower())]
        if rel_facts:
            body.append(f"\n## Known facts ({len(rel_facts)})\n")
            for f_ in sorted(rel_facts, key=lambda x: -(x.get("importance") or 0))[:30]:
                tp = f_.get("fact_type") or "note"
                imp = f_.get("importance") or 0
                src = f" _(src: {f_['source']})_" if f_.get("source") else ""
                tnote = topic_notes.get(f_.get("topic_id"))
                tref = f" — {link(tnote)}" if tnote else ""
                body.append(f"- **[{tp}]** (imp {imp}) {f_['content']}{src}{tref}")
            body.append("")

        txt = "\n".join(body)
        related = find_related(txt, note)
        esum = (e.get("notes") or "").strip().split("\n")[0][:160]
        if not esum:
            esum = f"{etype.title()} — {len(rel_facts)} known fact(s) in memory."
        write_note(folder, note, txt, related=related, origin="memory", summary=esum)
        for r in related:
            BACKLINKS[r].add(note)
        counts["entity"] += 1

    # ------------------------------------------------ 1b. identity note
    selves = [e for e in mem["entities"] if e.get("is_self")]
    if selves:
        e = selves[0]
        nm = (e.get("name") or "Me").strip()
        note = slugify(nm)
        try:
            attrs = json.loads(e.get("attributes") or "{}")
        except Exception:
            attrs = {}
        body = [fm(title=nm, type="identity", tags=["meta", "identity"],
                   generated=True, updated=(e.get("updated_at") or "")[:10],
                   source="memory.db:entities(is_self)")]
        body.append(f"# {nm}\n")
        body.append("> [!abstract] Who the vault belongs to. Every account, quota and "
                    "cadence note in this vault is scoped to this territory.\n")
        if attrs:
            body.append("\n| Attribute | Value |\n|---|---|")
            for k, v in attrs.items():
                body.append(f"| {k} | {str(v)[:300]} |")
            body.append("")
        if e.get("notes"):
            body.append(f"\n## Profile\n\n{e['notes']}\n")
        own = [f_ for f_ in mem["facts"]
               if (f_.get("fact_type") or "") in ("preference", "constraint")
               and (f_.get("importance") or 0) >= 5]
        if own:
            body.append(f"\n## Operating preferences & constraints  <small>({len(own)})</small>\n")
            for f_ in own[:40]:
                t = topic_notes.get(f_.get("topic_id"))
                body.append(f"- **[{f_.get('fact_type')}]** {f_['content']}"
                            + (f" — {link(t)}" if t else ""))
        txt = "\n".join(body)
        related = find_related(txt, note)
        write_note("meta", note, txt, related=related, origin="memory")
        counts["identity"] += 1

    # ------------------------------------------------ 2. topic notes
    for t in mem["topics"]:
        tid = t["id"]
        note = topic_notes[tid]
        tfacts = facts_by_topic.get(tid, [])
        snaps = [s for s in mem["snapshots"] if s.get("topic_id") == tid]
        cat = t.get("category") or "general"

        body = [fm(title=t.get("title") or t.get("slug"), type="topic",
                   category=cat, slug=t.get("slug"),
                   status=t.get("status") or "active",
                   fact_count=len(tfacts),
                   tags=[f"topic/{cat}", "knowledge"], generated=True,
                   updated=(t.get("last_accessed_at") or t.get("updated_at") or "")[:10],
                   source="memory.db:topics")]
        body.append(f"# {t.get('title') or t.get('slug')}\n")
        if t.get("description"):
            body.append(f"> [!abstract] {t['description']}\n")
        body.append(f"\n**Category:** `{cat}` · **Facts:** {len(tfacts)} · "
                    f"**Slug:** `{t.get('slug')}`\n")

        if tfacts:
            body.append(f"\n## Facts\n")
            by_type = defaultdict(list)
            for f_ in tfacts:
                by_type[f_.get("fact_type") or "note"].append(f_)
            for ft in sorted(by_type):
                body.append(f"\n### {ft}\n")
                for f_ in sorted(by_type[ft], key=lambda x: -(x.get("importance") or 0)):
                    imp = f_.get("importance") or 0
                    star = "⭐ " if imp >= 5 else ""
                    src = f"\n  _source: {f_['source']}_" if f_.get("source") else ""
                    body.append(f"- {star}{f_['content']}{src}")
        if snaps:
            body.append(f"\n## Snapshots\n")
            for s in sorted(snaps, key=lambda x: x.get("seq_number") or 0):
                body.append(f"\n### {s.get('title') or 'Snapshot'} "
                            f"({(s.get('created_at') or '')[:10]})\n")
                for fld in ("summary", "findings", "decisions", "open_questions", "next_steps"):
                    if s.get(fld):
                        body.append(f"**{fld.replace('_',' ').title()}**\n\n{s[fld]}\n")

        txt = "\n".join(body)
        related = find_related(txt, note)
        tsum = (t.get("description") or "").strip()[:160]
        if not tsum:
            tsum = f"{cat.title()} topic — {len(tfacts)} fact(s), {len(snaps)} snapshot(s)."
        write_note("topics", note, txt, related=related, origin="memory", summary=tsum)
        for r in related:
            BACKLINKS[r].add(note)
        counts["topic"] += 1

    # ------------------------------------------------ 3. learnings
    high = [f_ for f_ in mem["facts"] if (f_.get("importance") or 0) >= 4]
    by_type = defaultdict(list)
    for f_ in high:
        by_type[(f_.get("fact_type") or "note").lower()].append(f_)
    for ft, items in sorted(by_type.items()):
        note = slugify(f"Learnings - {ft}")
        body = [fm(title=f"Learnings — {ft}", type="learning", fact_type=ft,
                   count=len(items), tags=["knowledge/learning", f"fact/{ft}"],
                   generated=True, updated=NOW.strftime("%Y-%m-%d"),
                   source="memory.db:facts")]
        body.append(f"# Learnings — {ft}\n")
        body.append(f"> [!tip] {len(items)} high-importance `{ft}` facts "
                    f"(importance ≥ 4) distilled from long-term memory.\n")
        for f_ in sorted(items, key=lambda x: (-(x.get("importance") or 0),
                                               x.get("created_at") or "")):
            tnote = topic_notes.get(f_.get("topic_id"))
            tref = f" · {link(tnote)}" if tnote else ""
            src = f"\n  _source: {f_['source']}_" if f_.get("source") else ""
            body.append(f"\n- **imp {f_.get('importance')}**{tref}\n  {f_['content']}{src}")
        txt = "\n".join(body)
        related = find_related(txt, note)
        write_note("learnings", note, txt, related=related, origin="memory",
                   summary=f"{len(items)} high-importance \'{ft}\' facts (importance 4+).")
        for r in related:
            BACKLINKS[r].add(note)
        counts["learning"] += 1

    # ------------------------------------------------ 4. imported documents
    imported_by_domain = defaultdict(list)
    index_by_group = defaultdict(list)
    subject_docs = defaultdict(list)

    for rel, full in md:
        domain, ntype, subject = classify(rel)
        size = full.stat().st_size
        if domain == "reference" or domain == "meta" and ntype == "config":
            index_by_group[subject or domain].append((rel, full))
            continue
        if size > MAX_IMPORT_BYTES:
            index_by_group[f"Oversized ({domain})"].append((rel, full))
            continue

        raw = read_text(full)
        if not raw.strip():
            continue
        src_meta, body_txt = split_frontmatter(raw)
        title = src_meta.get("title") or title_from(full, body_txt)
        note = slugify(title if len(title) > 8 else f"{title} - {full.stem}")

        tags = [f"{domain}", f"type/{ntype}"]
        if subject:
            tags.append("account/" + slugify(subject).lower().replace(" ", "-"))

        head = fm(title=title, type=ntype, domain=domain, subject=subject,
                  tags=tags, generated=True, source=rel,
                  source_abs=str(full),
                  modified=datetime.fromtimestamp(full.stat().st_mtime).strftime("%Y-%m-%d"),
                  size_kb=round(size / 1024, 1))
        content = head + f"\n# {title}\n\n" + source_callout(rel, full) + clean_body(body_txt)

        related = find_related(content, note)
        if subject and subject in doc_subjects:
            related = sorted(set(related) | {doc_subjects[subject]})
        final = write_note(domain, note, content, related=related, origin="workspace")
        for r in related:
            BACKLINKS[r].add(final)
        imported_by_domain[domain].append((final, title, rel))
        if subject:
            subject_docs[subject].append((final, title, rel))
        counts["imported"] += 1

    # -------------------------------------- 4b. account hub notes for subjects
    # Subjects that documents are filed under but which have no entity record
    # would otherwise be dangling link targets. Give each a real hub note.
    if MEMORY_ONLY:
        for subj, docs in (state.get("subject_docs") or {}).items():
            subject_docs.setdefault(subj, [tuple(d) for d in docs])
    existing_notes = {p.stem for p in WRITTEN}
    for subj, note in sorted(doc_subjects.items()):
        if note in existing_notes:
            continue
        docs = subject_docs.get(subj, [])
        rel_facts = [f_ for f_ in mem["facts"]
                     if re.search(r"(?<![\w-])" + re.escape(subj.lower()) + r"(?![\w-])",
                                  (f_.get("content") or "").lower())]
        head = fm(title=subj, type="entity", entity_type="account",
                  tags=["entity/account", "people"], generated=True,
                  doc_count=len(docs), updated=NOW.strftime("%Y-%m-%d"),
                  source="derived:document-subject")
        parts = [head, f"\n# {subj}\n",
                 f"> [!abstract] Account hub. Derived from {len(docs)} document(s) filed "
                 f"under this account in the workspace.\n"]
        if docs:
            parts.append(f"\n## Documents  <small>({len(docs)})</small>\n")
            for n_, t_, r_ in sorted(docs, key=lambda x: x[1].lower()):
                parts.append(f"- {link(n_)} — `{r_}`")
        if rel_facts:
            parts.append(f"\n## Known facts  <small>({len(rel_facts)})</small>\n")
            for f_ in sorted(rel_facts, key=lambda x: -(x.get("importance") or 0))[:30]:
                t = topic_notes.get(f_.get("topic_id"))
                src = f" _(src: {f_['source']})_" if f_.get("source") else ""
                parts.append(f"- **[{f_.get('fact_type')}]** {f_['content']}{src}"
                             + (f" — {link(t)}" if t else ""))
        content = "\n".join(parts)
        related = find_related(content, note)
        write_note("accounts", note, content, related=related, origin="memory",
                   summary=f"Account hub — {len(docs)} document(s), "
                           f"{len(rel_facts)} known fact(s).")
        counts["account-hub"] += 1

    # ------------------------------------------------ 5. skills
    for rel, full in sorted(skillfiles):
        sk = Path(rel).parent.name
        note = skill_notes[sk]
        raw = read_text(full)
        meta, body_txt = split_frontmatter(raw)
        desc = meta.get("description", "")
        title = meta.get("name") or sk
        sk_dir = full.parent
        assets = sorted(p.relative_to(sk_dir).as_posix()
                        for p in sk_dir.rglob("*") if p.is_file() and p.name != "SKILL.md")

        head = fm(title=f"Skill — {title}", type="skill", skill=sk,
                  tags=["skill", f"skill/{slugify(sk).lower()}"], generated=True,
                  source=rel, source_abs=str(full),
                  modified=datetime.fromtimestamp(full.stat().st_mtime).strftime("%Y-%m-%d"),
                  asset_count=len(assets))
        parts = [head, f"\n# Skill — {title}\n"]
        if desc:
            parts.append(f"> [!abstract] Trigger\n> {desc[:1500]}\n")
        parts.append(f"\n**Invoke:** `/{sk}` · **Source:** `{rel}`\n")
        if assets:
            parts.append(f"\n## Bundled assets ({len(assets)})\n")
            for a in assets[:60]:
                parts.append(f"- `{a}`")
            if len(assets) > 60:
                parts.append(f"- _…and {len(assets)-60} more_")
            parts.append("")
        parts.append("\n## Definition\n")
        parts.append(clean_body(body_txt)[:40000])
        content = "\n".join(parts)
        related = find_related(content, note)
        write_note("skills", note, content, related=related, origin="workspace",
                   summary=(re.sub(r"\s+", " ", desc).strip()[:160]
                            or f"Skill /{sk} — {len(assets)} bundled asset(s)."))
        for r in related:
            BACKLINKS[r].add(note)
        counts["skill"] += 1

    # ------------------------------------------------ 6. artifacts index
    art_by_dir = defaultdict(list)
    for rel, full in artifacts:
        art_by_dir[rel.split(os.sep)[0]].append((rel, full))

    ART_TARGET = {"msx": "territory", "work-week": "territory", "career": "career",
                  "drafts": "briefs", "files": "briefs", "customer-draft": "briefs",
                  "docs": "briefs"}
    for group, items in sorted(art_by_dir.items()):
        folder = ART_TARGET.get(group, "briefs")
        note = slugify(f"Artifacts - {group}")
        by_ext = defaultdict(list)
        for rel, full in items:
            by_ext[full.suffix.lower()].append((rel, full))
        head = fm(title=f"Artifacts — {group}", type="artifact-index", group=group,
                  tags=["artifact", f"artifact/{group}"], generated=True,
                  count=len(items), source=group, updated=NOW.strftime("%Y-%m-%d"))
        parts = [head, f"\n# Artifacts — {group}\n",
                 f"> [!note] {len(items)} non-markdown artifacts in `{group}\\`. "
                 f"These are reports, datasets and decks referenced by the wiki but "
                 f"stored in the workspace, not the vault.\n"]
        for ext in sorted(by_ext):
            parts.append(f"\n## `{ext}` ({len(by_ext[ext])})\n")
            parts.append("| Artifact | Modified | Size |")
            parts.append("|---|---|---|")
            for rel, full in sorted(by_ext[ext], key=lambda x: -x[1].stat().st_mtime):
                st = full.stat()
                parts.append(f"| `{rel}` | {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d} "
                             f"| {st.st_size/1024:.0f} KB |")
        content = "\n".join(parts)
        related = find_related(content, note)
        write_note(folder, note, content, related=related, origin="workspace",
                   summary=f"{len(items)} non-markdown artifact(s) in {group}\\ — "
                           f"reports, datasets and decks kept in the workspace.")
        counts["artifact-index"] += 1

    # ------------------------------------------------ 7. reference libraries
    for group, items in sorted(index_by_group.items(), key=lambda x: -len(x[1])):
        note = slugify(f"Library - {group}")
        head = fm(title=f"Library — {group}", type="library-index",
                  tags=["reference", "library"], generated=True,
                  count=len(items), updated=NOW.strftime("%Y-%m-%d"))
        parts = [head, f"\n# Library — {group}\n",
                 f"> [!note] {len(items)} markdown files indexed by reference. "
                 f"Content lives in the workspace and is intentionally not duplicated "
                 f"into the vault to keep the graph signal-dense.\n",
                 "\n| File | Modified | Size |", "|---|---|---|"]
        for rel, full in sorted(items, key=lambda x: x[0]):
            st = full.stat()
            parts.append(f"| `{rel}` | {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d} "
                         f"| {st.st_size/1024:.0f} KB |")
        write_note("reference", note, "\n".join(parts), origin="workspace",
                   summary=f"{len(items)} markdown file(s) indexed by reference, "
                           f"not copied into the vault.")
        counts["library"] += 1

    # ------------------------------------------------ 8. sessions & work log
    store = load_session_store()
    files_by_session = defaultdict(list)
    for f_ in store["files"]:
        files_by_session[f_["session_id"]].append(f_)
    first_ask = {t["session_id"]: (t.get("user_message") or "").strip()
                 for t in store["turns"]}

    # Merge both records: the session store is the wider view; memory.db adds
    # extraction counts for the sessions that produced durable facts.
    ltm_by_id = {s.get("session_id"): s for s in mem["sessions"]}
    merged = []
    for s in store["sessions"]:
        merged.append({
            "id": s["id"], "when": (s.get("created_at") or "")[:16].replace("T", " "),
            "month": (s.get("created_at") or "unknown")[:7],
            "title": (s.get("summary") or "").strip() or "(untitled session)",
            "cwd": s.get("cwd") or "", "repo": s.get("repository") or "",
            "ltm": ltm_by_id.get(s["id"]),
            "files": files_by_session.get(s["id"], []),
            "ask": first_ask.get(s["id"], ""),
        })
    seen_ids = {m["id"] for m in merged}
    for s in mem["sessions"]:
        if s.get("session_id") in seen_ids:
            continue
        merged.append({
            "id": s.get("session_id"), "when": (s.get("processed_at") or "")[:16],
            "month": (s.get("processed_at") or "unknown")[:7],
            "title": (s.get("summary") or "").strip() or "(untitled session)",
            "cwd": s.get("cwd") or "", "repo": s.get("repository") or "",
            "ltm": s, "files": [], "ask": "",
        })

    by_month = defaultdict(list)
    for m in merged:
        by_month[m["month"] or "unknown"].append(m)

    for month, items in sorted(by_month.items(), reverse=True):
        note = slugify(f"Sessions {month}")
        touched = Counter()
        for it in items:
            for f_ in it["files"]:
                p = (f_.get("file_path") or "").replace(str(ROOT) + os.sep, "")
                top = p.split(os.sep)[0] if p else ""
                if top:
                    touched[top] += 1
        head = fm(title=f"Sessions — {month}", type="session-log", month=month,
                  count=len(items), tags=["activity", "session"], generated=True,
                  source="session-store.db:sessions + memory.db:ltm_session_log")
        parts = [head, f"\n# Sessions — {month}\n",
                 f"> [!info] {len(items)} working sessions recorded this month.\n"]
        if touched:
            parts.append("\n## Areas worked\n\n| Area | File touches |\n|---|---|")
            for k, v in touched.most_common(15):
                parts.append(f"| `{k}` | {v} |")
        parts.append("\n## Sessions\n")
        for it in sorted(items, key=lambda x: x["when"], reverse=True):
            parts.append(f"\n### {it['title']}")
            meta = [f"`{it['when']}`"]
            if it["repo"]:
                meta.append(f"repo `{it['repo']}`")
            if it["files"]:
                meta.append(f"{len(it['files'])} file(s) touched")
            if it["ltm"]:
                l = it["ltm"]
                meta.append(f"facts {l.get('facts_extracted', 0)} · "
                            f"entities {l.get('entities_extracted', 0)}")
            parts.append("_" + " · ".join(meta) + "_\n")
            if it["ask"]:
                parts.append(f"> {re.sub(r'\\s+', ' ', it['ask'])[:400]}\n")
            if it["files"]:
                uniq = sorted({(f_.get("file_path") or "").replace(str(ROOT) + os.sep, "")
                               for f_ in it["files"]})[:12]
                parts.append("Files: " + ", ".join(f"`{u}`" for u in uniq))
        content = "\n".join(parts)
        write_note("sessions", note, content, related=find_related(content, note), origin="memory")
        counts["session"] += 1

    # Checkpoints are the richest narrative record of what was actually built.
    if store["checkpoints"]:
        cp_month = defaultdict(list)
        for cp in store["checkpoints"]:
            cp_month[(cp.get("created_at") or "unknown")[:7]].append(cp)
        sess_title = {s["id"]: (s.get("summary") or "") for s in store["sessions"]}
        for month, items in sorted(cp_month.items(), reverse=True):
            note = slugify(f"Work Log {month}")
            head = fm(title=f"Work Log — {month}", type="work-log", month=month,
                      count=len(items), tags=["activity", "work-log"], generated=True,
                      source="session-store.db:checkpoints")
            parts = [head, f"\n# Work Log — {month}\n",
                     f"> [!abstract] {len(items)} checkpointed pieces of work — what was "
                     f"done, how, and what came next.\n"]
            for cp in sorted(items, key=lambda x: x.get("created_at") or "", reverse=True):
                t = cp.get("title") or sess_title.get(cp["session_id"], "Checkpoint")
                parts.append(f"\n## {t}\n")
                parts.append(f"_{(cp.get('created_at') or '')[:16].replace('T', ' ')} · "
                             f"checkpoint {cp.get('checkpoint_number')}_\n")
                for label, field in (("Overview", "overview"), ("Work done", "work_done"),
                                     ("Technical detail", "technical_details"),
                                     ("Key files", "important_files"),
                                     ("Next steps", "next_steps")):
                    v = (cp.get(field) or "").strip()
                    if v:
                        parts.append(f"\n**{label}**\n\n{v[:6000]}\n")
            content = "\n".join(parts)
            write_note("sessions", note, content, related=find_related(content, note), origin="memory")
            counts["work-log"] += 1

    if mem["patterns"]:
        pc = Counter(p.get("pattern_type") or "unknown" for p in mem["patterns"])
        note = "Working Patterns"
        head = fm(title="Working Patterns", type="pattern-index",
                  tags=["activity", "pattern"], generated=True,
                  count=len(mem["patterns"]), source="memory.db:ltm_patterns")
        parts = [head, "\n# Working Patterns\n",
                 f"> [!tip] {len(mem['patterns'])} detected patterns across "
                 f"{len(set(p.get('session_id') for p in mem['patterns']))} sessions.\n",
                 "\n| Pattern | Occurrences |", "|---|---|"]
        for k, v in pc.most_common():
            parts.append(f"| `{k}` | {v} |")
        parts.append("\n## Recent detail\n")
        for p in mem["patterns"][:60]:
            parts.append(f"- **{p.get('pattern_type')}** — {(p.get('description') or '')[:220]}")
        content = "\n".join(parts)
        write_note("sessions", note, content, related=find_related(content, note), origin="memory")
        counts["pattern"] += 1

    return (counts, imported_by_domain, skill_notes, topic_notes, entity_notes,
            mem, {"aliases": dict(ALIASES), "doc_subjects": doc_subjects,
                  "skill_notes": skill_notes,
                  "subject_docs": {k: [list(x) for x in v]
                                   for k, v in subject_docs.items()}},
            state)
