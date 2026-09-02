"""LTM wiki builder — entry point. Generates notes then maps of content."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import notes as N
from config import paths
from vaultlib import (BUILD, DRY, F, MEMORY_ONLY, NOW, ROOT, STAMP, STATE_DIR,
                      VAULT, flush_notes, fm, link, load_state, read_text,
                      save_state, slugify, write_note, WRITTEN)

HOME = "Home"
CONFIG_PATHS = paths()
REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_MODULE = REPO_ROOT / "memory.ps1"
BUILD_DIR = Path(__file__).resolve().parent
WORKSPACE_DESCRIPTION = str(CONFIG_PATHS["workspace"]) if CONFIG_PATHS["workspace"] else "the configured workspace"


def moc(folder_key: str, name: str, title: str, blurb: str, sections: list[tuple[str, list[str]]],
        tags: list[str], extra: str = ""):
    head = fm(title=title, type="moc", tags=["moc"] + tags, generated=True,
              updated=NOW.strftime("%Y-%m-%d"))
    parts = [head, f"\n# {title}\n", f"> [!abstract] {blurb}\n"]
    if extra:
        parts.append(extra)
    for heading, items in sections:
        if not items:
            continue
        parts.append(f"\n## {heading}  <small>({len(items)})</small>\n")
        parts.extend(items)
    parts.append(f"\n---\n\n_Index generated {STAMP}. Return to {link(HOME)}._\n")
    return write_note(folder_key, name, "\n".join(parts), origin="index",
                      summary=blurb)


def listing(pairs, note_field=0):
    return [f"- {link(p[note_field])}" for p in sorted(pairs, key=lambda x: str(x[1]).lower())]


def seed_agents_md():
    """Ensure the operating contract exists.

    `index.md` and `Home` both link to it, so a build without it leaves dangling
    links — and more importantly an agent arriving at a fresh vault would have
    no instructions. Seeded here rather than only in the installer so that
    `python build.py` alone is sufficient.
    """
    target = VAULT / "AGENTS.md"
    if target.exists() or DRY:
        return False
    template = Path(__file__).resolve().parent / "templates" / "AGENTS.md"
    if not template.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(read_text(template), encoding="utf-8")
    return True


def configure_obsidian():
    """Keep Obsidian itself configured so the vault is legible on open.

    Graph colouring by domain is what turns 460 nodes from a hairball into a
    readable map, so it is treated as part of the build rather than manual setup.
    """
    cfg = VAULT / ".obsidian"
    if DRY:
        return
    cfg.mkdir(parents=True, exist_ok=True)

    palette = [
        ("00-Meta",      "8892a0"), ("01-Activity", "e0a33e"),
        ("02-People",    "2fb8a0"), ("03-Research", "4a8fe7"),
        ("04-Tech",      "9b6dd6"), ("05-Knowledge", "5bc46b"),
        ("06-Outputs",   "ec7a3c"), ("07-Tasks",    "e05c5c"),
        ("08-Skills",    "d96ba8"), ("09-Reference", "6b7280"),
    ]
    graph = {
        "collapse-filter": True, "search": "", "showTags": False,
        "showAttachments": False, "hideUnresolved": True, "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": [
            {"query": f'path:"{folder}"', "color": {"a": 1, "rgb": int(hexc, 16)}}
            for folder, hexc in palette
        ],
        "collapse-display": True, "showArrow": True, "textFadeMultiplier": -0.7,
        "nodeSizeMultiplier": 1.1, "lineSizeMultiplier": 0.8,
        "collapse-forces": True, "centerStrength": 0.42, "repelStrength": 11,
        "linkStrength": 0.65, "linkDistance": 190, "scale": 0.6, "close": False,
    }
    (cfg / "graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")

    (cfg / "app.json").write_text(json.dumps({
        "attachmentFolderPath": "99-Attachments",
        "newLinkFormat": "shortest",
        "useMarkdownLinks": False,
        "alwaysUpdateLinks": True,
        "showUnsupportedFiles": False,
        "defaultViewMode": "preview",
        "livePreview": True,
    }, indent=2), encoding="utf-8")

    (cfg / "appearance.json").write_text(json.dumps({
        "accentColor": "#4a8fe7",
        "theme": "obsidian",
        "showInlineTitle": True,
        "showViewHeader": True,
    }, indent=2), encoding="utf-8")

    bookmarks = {"items": [{"type": "file", "path": f"{p}.md", "title": t} for p, t in [
        (f"{F['meta']}/Home", "🏠 Home"),
        ("02-People/People MOC", "👥 People & Accounts"),
        ("05-Knowledge/Knowledge MOC", "🧠 Knowledge"),
        ("01-Activity/Activity MOC", "📅 Activity"),
        (f"{F['research']}/Research MOC", "🔬 Research"),
        (f"{F['tech']}/Tech MOC", "⚙️ Tech"),
        ("06-Outputs/Outputs MOC", "📤 Outputs"),
        (f"{F['skills']}/Skills MOC", "🛠️ Skills"),
        (f"{F['tasks']}/Open Loops", "✅ Open Loops"),
        (f"{F['meta']}/Vault Guide", "📖 Vault Guide"),
    ]]}
    (cfg / "bookmarks.json").write_text(json.dumps(bookmarks, indent=2), encoding="utf-8")
    print("[config] graph colour groups, app, appearance and bookmarks written")


def main():
    if seed_agents_md():
        print("[seed]  AGENTS.md created from template — the operating contract")
    (counts, imported, skill_notes, topic_notes, entity_notes, mem,
     fresh_state, cached) = N.build()

    # ---- inventory: what exists in the vault, by folder
    # On a memory-only run the workspace notes are still on disk but were not
    # regenerated, so their names come from the cached inventory. Without this
    # the maps of content would silently lose every workspace note.
    inventory = [{"stem": p.stem, "folder": p.relative_to(VAULT).as_posix()
                  .rsplit("/", 1)[0]} for p in WRITTEN]
    if MEMORY_ONLY:
        fresh_stems = {i["stem"] for i in inventory}
        for rec in cached.get("notes", []):
            if rec["stem"] not in fresh_stems and rec.get("origin") != "memory":
                inventory.append(rec)

    by_folder = defaultdict(list)
    for rec in inventory:
        by_folder[rec["folder"]].append(rec["stem"])

    def notes_in(key, sub=""):
        k = F[key] + (f"/{sub}" if sub else "")
        return sorted(set(by_folder.get(k, [])))

    # ---------------------------------------------------------------- People
    accounts = notes_in("accounts")
    internal = notes_in("internal")
    ent_by_type = defaultdict(list)
    for e in mem["entities"]:
        if e.get("is_self") or not (e.get("name") or "").strip():
            continue
        ent_by_type[(e.get("entity_type") or "other").lower()].append(slugify(e["name"]))

    moc("people", "People MOC", "People & Accounts",
        "Every customer account and every internal person the work has touched, "
        "with the facts long-term memory holds on each.",
        [("Accounts", [f"- {link(a)}" for a in accounts]),
         ("Internal & partners", [f"- {link(p)}" for p in internal])],
        ["people"]) if False else None

    # People MOC lives at 02-People root
    _people_head = fm(title="People & Accounts", type="moc",
                      tags=["moc", "people"], generated=True,
                      summary="Every account and person the work has touched, "
                              "with the facts memory holds on each.",
                      updated=NOW.strftime("%Y-%m-%d"))
    pp = [_people_head, "\n# People & Accounts\n",
          "> [!abstract] Every customer account and internal person the work has touched, "
          "with the facts long-term memory holds on each. Account documents live in "
          f"{link('Research MOC')} and link back here.\n",
          f"\n## Accounts  <small>({len(accounts)})</small>\n"]
    pp += [f"- {link(a)}" for a in accounts]
    pp.append(f"\n## Internal & partners  <small>({len(internal)})</small>\n")
    pp += [f"- {link(p)}" for p in internal]
    pp.append(f"\n---\n\n_Index generated {STAMP}. Return to {link(HOME)}._\n")
    if not DRY:
        (VAULT / "02-People").mkdir(parents=True, exist_ok=True)
        (VAULT / "02-People" / "People MOC.md").write_text("\n".join(pp), encoding="utf-8")
    WRITTEN.append(VAULT / "02-People" / "People MOC.md")

    # ------------------------------------------------------------- Knowledge
    topics_notes = notes_in("topics")
    learn_notes = notes_in("learnings")
    tcat = defaultdict(list)
    for t in mem["topics"]:
        nm = topic_notes.get(t["id"])
        if nm:
            tcat[t.get("category") or "general"].append(nm)
    sections = [(f"Topics — {c}", [f"- {link(n)}" for n in sorted(set(v))])
                for c, v in sorted(tcat.items(), key=lambda x: -len(x[1]))]
    sections.insert(0, ("Distilled learnings", [f"- {link(n)}" for n in learn_notes]))
    kh = fm(title="Knowledge", type="moc", tags=["moc", "knowledge"], generated=True,
            summary="Topics and distilled learnings — the durable threads and the "
                    "high-importance cut of what we know.",
            updated=NOW.strftime("%Y-%m-%d"))
    kp = [kh, "\n# Knowledge\n",
          f"> [!abstract] {len(mem['facts'])} facts across {len(mem['topics'])} topics, "
          f"distilled from every recorded session. Topics are the durable threads; "
          f"learnings are the high-importance cut.\n"]
    for heading, items in sections:
        if items:
            kp.append(f"\n## {heading}  <small>({len(items)})</small>\n")
            kp += items
    kp.append(f"\n---\n\n_Index generated {STAMP}. Return to {link(HOME)}._\n")
    if not DRY:
        (VAULT / "05-Knowledge").mkdir(parents=True, exist_ok=True)
        (VAULT / "05-Knowledge" / "Knowledge MOC.md").write_text("\n".join(kp), encoding="utf-8")
    WRITTEN.append(VAULT / "05-Knowledge" / "Knowledge MOC.md")

    # -------------------------------------------------------------- Research
    moc("research", "Research MOC", "Research",
        "Deep-dive technical and customer research. Each note keeps a pointer to its "
        "source file in the workspace so nothing is orphaned from its origin.",
        [("Research notes", [f"- {link(n)}" for n in notes_in("research")])],
        ["research"])

    # ------------------------------------------------------------------ Tech
    moc("tech", "Tech MOC", "Technology & Systems",
        "Architecture, platform behaviour, internal tooling and the systems that keep "
        "this workspace running — memory, MCP, skills infrastructure, demos.",
        [("Notes", [f"- {link(n)}" for n in notes_in("tech")])],
        ["tech"])

    # -------------------------------------------------------------- Activity
    career = notes_in("career")
    terr = notes_in("territory")
    sess_all = notes_in("sessions")
    worklog = [n for n in sess_all if n.startswith("Work Log")]
    sess = [n for n in sess_all if n.startswith("Sessions")]
    other_act = [n for n in sess_all if n not in worklog and n not in sess]
    ah = fm(title="Activity", type="moc", tags=["moc", "activity"], generated=True,
            summary="What happened — work log, session record, territory and quota "
                    "work, career artifacts.",
            updated=NOW.strftime("%Y-%m-%d"))
    ap = [ah, "\n# Activity\n",
          "> [!abstract] What actually happened: territory and quota work, career and "
          "Connect artifacts, the narrative work log of what was built, and the "
          "session-by-session record of how it was done.\n",
          f"\n## Work log — what was built  <small>({len(worklog)})</small>\n"]
    ap += [f"- {link(n)}" for n in sorted(worklog, reverse=True)]
    ap.append(f"\n## Session record  <small>({len(sess)})</small>\n")
    ap += [f"- {link(n)}" for n in sorted(sess, reverse=True)]
    if other_act:
        ap.append(f"\n## Patterns  <small>({len(other_act)})</small>\n")
        ap += [f"- {link(n)}" for n in other_act]
    ap.append(f"\n## Territory & quota  <small>({len(terr)})</small>\n")
    ap += [f"- {link(n)}" for n in terr]
    ap.append(f"\n## Career & Connect  <small>({len(career)})</small>\n")
    ap += [f"- {link(n)}" for n in career]
    ap.append(f"\n---\n\n_Index generated {STAMP}. Return to {link(HOME)}._\n")
    if not DRY:
        (VAULT / "01-Activity").mkdir(parents=True, exist_ok=True)
        (VAULT / "01-Activity" / "Activity MOC.md").write_text("\n".join(ap), encoding="utf-8")
    WRITTEN.append(VAULT / "01-Activity" / "Activity MOC.md")

    # --------------------------------------------------------------- Outputs
    briefs = notes_in("briefs")
    posts = notes_in("posts")
    oh = fm(title="Outputs", type="moc", tags=["moc", "outputs"], generated=True,
            summary="What we shipped — briefs, drafts, posts and the artifact "
                    "indexes pointing at reports in the workspace.",
            updated=NOW.strftime("%Y-%m-%d"))
    op = [oh, "\n# Outputs\n",
          "> [!abstract] Things shipped: customer briefs, drafts, published posts and the "
          "artifact indexes that point at HTML reports and datasets living in the workspace.\n",
          f"\n## Briefs, drafts & artifacts  <small>({len(briefs)})</small>\n"]
    op += [f"- {link(n)}" for n in briefs]
    op.append(f"\n## Published posts  <small>({len(posts)})</small>\n")
    op += [f"- {link(n)}" for n in posts]
    op.append(f"\n---\n\n_Index generated {STAMP}. Return to {link(HOME)}._\n")
    if not DRY:
        (VAULT / "06-Outputs").mkdir(parents=True, exist_ok=True)
        (VAULT / "06-Outputs" / "Outputs MOC.md").write_text("\n".join(op), encoding="utf-8")
    WRITTEN.append(VAULT / "06-Outputs" / "Outputs MOC.md")

    # ---------------------------------------------------------------- Skills
    moc("skills", "Skills MOC", "Skills & Agent Workflows",
        f"{len(skill_notes)} installed skills. Each note carries the trigger description, "
        "bundled assets and the full definition, so an agent can reason about which "
        "capability to reach for without reading the repo.",
        [("Installed skills", [f"- {link(n)}" for n in notes_in("skills")])],
        ["skills"])

    # ------------------------------------------------------------- Reference
    moc("reference", "Reference MOC", "Reference Libraries",
        "External and mirrored material indexed but deliberately not duplicated into the "
        "vault — vendored skill libraries, rule sets, evaluation workspaces and backups. "
        "Listed so the wiki knows they exist and where to find them.",
        [("Libraries", [f"- {link(n)}" for n in notes_in("reference")])],
        ["reference"])

    # ------------------------------------------------------------------ Tasks
    def is_noise(txt: str) -> bool:
        """Some captured facts are truncated context fragments, not tasks."""
        t = (txt or "").strip()
        return (not t or t.startswith("Pending: ...") or t.startswith("...")
                or len(t) < 12)

    todos = [f for f in mem["facts"]
             if (f.get("fact_type") or "") == "todo" and not is_noise(f.get("content"))]
    actions = [f for f in mem["facts"]
               if (f.get("fact_type") or "") == "action" and not is_noise(f.get("content"))]
    # Facts recorded as questions are open work by definition — an unanswered
    # question blocking a deliverable belongs here, not only on its topic page.
    questions = [f for f in mem["facts"]
                 if (f.get("fact_type") or "") == "question" and not is_noise(f.get("content"))]
    open_q, next_steps = [], []
    for s in mem["snapshots"]:
        if s.get("open_questions"):
            open_q.append((s.get("title") or "Snapshot", s["open_questions"],
                           topic_notes.get(s.get("topic_id"))))
        if s.get("next_steps"):
            next_steps.append((s.get("title") or "Snapshot", s["next_steps"],
                               topic_notes.get(s.get("topic_id"))))
    th = fm(title="Open Loops", type="tasks", tags=["tasks"], generated=True,
            updated=NOW.strftime("%Y-%m-%d"), source="memory.db:facts+snapshots")
    tp = [th, "\n# Open Loops\n",
          "> [!todo] Everything memory still considers unfinished — recorded todos, "
          "open questions and next steps captured at the end of prior sessions.\n"]
    if todos:
        tp.append(f"\n## Todos  <small>({len(todos)})</small>\n")
        for f_ in sorted(todos, key=lambda x: -(x.get("importance") or 0)):
            t = topic_notes.get(f_.get("topic_id"))
            tp.append(f"- [ ] {f_['content']}" + (f" · {link(t)}" if t else ""))
    if questions:
        tp.append(f"\n## Unanswered questions  <small>({len(questions)})</small>\n")
        tp.append("> [!question] Recorded as questions in memory and never resolved. "
                  "Each one blocks something downstream.\n")
        for f_ in sorted(questions, key=lambda x: -(x.get("importance") or 0)):
            t = topic_notes.get(f_.get("topic_id"))
            star = "⭐ " if (f_.get("importance") or 0) >= 5 else ""
            tp.append(f"- [ ] {star}{f_['content']}" + (f" · {link(t)}" if t else ""))
    if next_steps:
        tp.append(f"\n## Next steps from snapshots  <small>({len(next_steps)})</small>\n")
        for title, txt, t in next_steps:
            tp.append(f"\n### {title}" + (f" · {link(t)}" if t else "") + "\n")
            tp.append(txt.strip())
    if open_q:
        tp.append(f"\n## Open questions  <small>({len(open_q)})</small>\n")
        for title, txt, t in open_q:
            tp.append(f"\n### {title}" + (f" · {link(t)}" if t else "") + "\n")
            tp.append(txt.strip())
    if actions:
        tp.append(f"\n## Recorded actions  <small>({len(actions)})</small>\n")
        tp.append("> [!note] Captured as actions in memory, but most read as durable "
                  "procedure rather than open work. Kept here for recall.\n")
        for f_ in sorted(actions, key=lambda x: -(x.get("importance") or 0)):
            t = topic_notes.get(f_.get("topic_id"))
            head = f_["content"].split(".")[0][:150]
            tp.append(f"\n- **{head}**" + (f" · {link(t)}" if t else ""))
    tp.append(f"\n---\n\n_Index generated {STAMP}. Return to {link(HOME)}._\n")
    write_note("tasks", "Open Loops", "\n".join(tp), origin="index",
               summary=f"{len(todos)} todo(s), {len(questions)} unanswered question(s), "
                       f"{len(next_steps)} next-step block(s), "
                       f"{len(open_q)} open question(s) from prior sessions.")

    # ------------------------------------------------------------------ Meta
    identity = next((slugify(e["name"]) for e in mem["entities"]
                     if e.get("is_self") and (e.get("name") or "").strip()), None)

    guide = fm(title="Vault Guide", type="meta", tags=["meta"], generated=True,
               updated=NOW.strftime("%Y-%m-%d"))
    gp = [guide, "\n# Vault Guide\n",
          "> [!abstract] How this vault is built, what is safe to edit, and how an agent "
          "should read it.\n",
          "\n## What this is\n",
          f"A generated, cross-referenced wiki over `{WORKSPACE_DESCRIPTION}` "
          "workspace plus the long-term memory database. It exists so that a human or an "
          "LLM agent can answer *what we built, who we work with, what we decided and what "
          "happened* from one place, with citations back to source files.\n",
          "\n## Folder contract\n",
          "| Folder | Holds | Source |",
          "|---|---|---|",
          "| `00-Meta` | This guide, the home map, build log | generated |",
          "| `01-Activity` | Territory & quota, career/Connect, work log, session record | `msx/`, `work-week/`, `career/`, `session-store.db`, memory sessions |",
          "| `02-People` | Account and person notes with their known facts | `memory.db:entities` |",
          "| `03-Research` | Deep-dive technical and customer research | `research/`, `docs/research/` |",
          "| `04-Tech` | Architecture, platform behaviour, internal systems | `docs/`, `long-term-memory/`, tooling repos |",
          "| `05-Knowledge` | Topics and distilled learnings | `memory.db:topics,facts,snapshots` |",
          "| `06-Outputs` | Briefs, drafts, posts, artifact indexes | `drafts/`, `files/`, `research/linkedin-posts/` |",
          "| `07-Tasks` | Open loops, next steps, open questions | `memory.db` |",
          "| `08-Skills` | One note per installed skill | `skills/*/SKILL.md` |",
          "| `09-Reference` | External libraries indexed, not duplicated | `anthropic/`, `.clinerules/`, mirrors |",
          "\n## Editing rules\n",
          "- Every generated note carries `generated: true` in frontmatter. "
          "**The builder deletes and recreates exactly those notes on each run.**\n"
          "- Any note you write by hand — anywhere in the vault — survives a rebuild, "
          "as long as it does not carry `generated: true`.\n"
          "- To keep a generated note permanently, remove the `generated: true` line. "
          "It then becomes yours and the builder leaves it alone.\n",
          "\n## Rebuilding\n",
          f"```powershell\n. {MEMORY_MODULE}\n"
          "Sync-Vault          # incremental — memory + sessions only (~6s)\n"
          "Sync-Vault -Full    # also rescan the workspace (~30s)\n```\n",
          "Or call the builder directly:\n",
          f"```powershell\ncd {BUILD_DIR}\n"
          "python build.py                 # full\n"
          "python build.py --memory-only   # incremental\n"
          "python build.py --dry-run       # report only\n```\n",
          "\n## Staying in sync with memory\n",
          "Writing to long-term memory keeps this vault current automatically:\n",
          "- `Add-Fact`, `Remember-This`, `Add-Topic`, `Add-Entity`, `Add-Snapshot` and "
          "`Set-Identity` mark the vault stale (instant — they only drop a marker).\n"
          "- `Export-MemoryContext` and `Matrix-Load` run an incremental sync.\n"
          "- The **sessionEnd hook** syncs after the session's facts, entities and "
          "checkpoints have all landed, so nothing learned in a session is missed.\n"
          "- Set `$env:LTM_VAULT_AUTOSYNC = '1'` to rebuild in the background on every "
          "single memory write.\n",
          "A sync is skipped when nothing changed, and a lock file prevents a background "
          "sync colliding with a manual one. Deletions propagate: removing a topic from "
          "memory removes its note on the next sync.\n",
          "\n> [!warning] `Sync-Vault` (incremental) does **not** notice new or changed "
          "files in the workspace — it only rebuilds from memory and the session store. "
          "Run `Sync-Vault -Full` after adding research, docs or skills.\n",
          "\n## How an agent should read this vault\n",
          f"1. Start at {link(HOME)} for the shape of everything.\n"
          f"2. For *who*, go to {link('People MOC')} — account notes aggregate every "
          "known fact about that customer.\n"
          f"3. For *what we know*, go to {link('Knowledge MOC')} — topics are the durable "
          "threads, learnings are the high-importance cut.\n"
          f"4. For *what happened*, go to {link('Activity MOC')}.\n"
          f"5. For *what we can do*, go to {link('Skills MOC')}.\n"
          "6. Every imported note has a `source` field in frontmatter pointing at the "
          "originating workspace file — cite that, not the vault path.\n",
          "\n## Conventions\n",
          "- `tags` are hierarchical (`entity/account`, `topic/research`, `type/research`).\n"
          "- A `## Related` section at the foot of a note is auto-generated from name "
          "matches across the corpus. It is a discovery aid, not a curated claim.\n"
          "- Importance on facts runs 3–5; 5 is load-bearing.\n",
          f"\n---\n\n_Guide generated {STAMP}._\n"]
    write_note("meta", "Vault Guide", "\n".join(gp), origin="index",
               summary="How this vault is built, what is safe to edit, and how it stays in sync with memory.")

    # Build log — written last so its counts reflect the whole run.
    total = len(WRITTEN) + 1  # +1 for Home, written after this note
    stats = Counter()
    for p in WRITTEN:
        stats[p.relative_to(VAULT).as_posix().split("/")[0]] += 1
    stats[F["meta"].split("/")[0]] += 1  # Home

    bh = fm(title="Build Log", type="meta", tags=["meta"], generated=True,
            updated=NOW.strftime("%Y-%m-%d"))
    bp = [bh, "\n# Build Log\n", f"> [!info] Last build: {STAMP}\n",
          "\n## Notes written by folder\n", "| Folder | Notes |", "|---|---|"]
    for k, v in sorted(stats.items()):
        bp.append(f"| `{k}` | {v} |")
    bp.append(f"| **Total** | **{total}** |")
    bp.append("\n## Note types generated\n| Type | Count |\n|---|---|")
    for k, v in sorted(counts.items()):
        bp.append(f"| {k} | {v} |")
    bp.append("\n## Memory inputs\n| Table | Rows |\n|---|---|")
    for k in ("topics", "facts", "entities", "snapshots", "sessions", "patterns"):
        bp.append(f"| `{k}` | {len(mem[k])} |")
    bp.append(f"\n---\n\n_See {link('Vault Guide')} for the folder contract._\n")
    write_note("meta", "Build Log", "\n".join(bp), origin="index",
               summary="Counts from the last build — notes per folder, note types, memory inputs.")

    # Home
    hh = fm(title="Home", type="moc", tags=["moc", "home"], generated=True,
            summary="Entry point — maps of content, vault stats, recent accounts "
                    "and live topics.",
            updated=NOW.strftime("%Y-%m-%d"))
    hp = [hh, "\n# VJ-Agency\n",
          "> [!abstract] One source of truth for what we build, who we work with, "
          "what we decided and what happened.\n",
          "\n## Start here\n",
          "> [!important] Agents: read [[index]] first — it catalogues every page "
          "with a one-line gist so you can open only what matters. "
          "[[AGENTS]] is the operating contract. [[log]] is the chronology.\n",
          f"| Map | What it answers |",
          "|---|---|",
          f"| {link('index')} | **Everything, one line each — start here** |",
          f"| {link('AGENTS')} | How to operate this vault |",
          f"| {link('People MOC')} | Who — accounts, internal, partners |",
          f"| {link('Knowledge MOC')} | What we know — topics and learnings |",
          f"| {link('Activity MOC')} | What happened — territory, career, sessions |",
          f"| {link('Research MOC')} | What we investigated |",
          f"| {link('Tech MOC')} | How the systems work |",
          f"| {link('Outputs MOC')} | What we shipped |",
          f"| {link('Skills MOC')} | What we can do |",
          f"| {link('Open Loops')} | What is unfinished |",
          f"| {link('Reference MOC')} | External material, indexed |",
          f"| {link('Vault Guide')} | How this vault works |",
          f"| {link('log')} | What happened to this vault, chronologically |"]
    if identity:
        hp.append(f"| {link(identity)} | Whose territory this is |")
    hp += ["\n## At a glance\n",          f"- **{total}** notes across **{len(stats)}** domains",
          f"- **{len(mem['facts'])}** facts · **{len(mem['topics'])}** topics · "
          f"**{len(mem['entities'])}** entities · **{len(mem['snapshots'])}** snapshots",
          f"- **{len(accounts)}** accounts · **{len(internal)}** people · "
          f"**{len(skill_notes)}** skills",
          f"- **{len(mem['sessions'])}** recorded sessions · "
          f"**{len(mem['patterns'])}** detected working patterns",
          "\n## Recently touched accounts\n"]
    recent = sorted([e for e in mem["entities"]
                     if (e.get("entity_type") or "").lower() in ("account", "org")],
                    key=lambda x: x.get("updated_at") or "", reverse=True)[:12]
    for e in recent:
        hp.append(f"- {link(slugify(e['name']))}")
    hp.append("\n## Live topics\n")
    for t in sorted(mem["topics"], key=lambda x: x.get("last_accessed_at") or "",
                    reverse=True)[:12]:
        n = topic_notes.get(t["id"])
        if n:
            hp.append(f"- {link(n)} — _{t.get('category')}_")

    # Hand-authored notes are never touched by the builder. They are catalogued
    # in index.md (marked as such) rather than enumerated here: index.md can be
    # refreshed on its own in about a second, whereas Home can only be rebuilt
    # by a full run. Listing them in both places meant the fast path left Home
    # stale, pointing at pages that had since been renamed or removed.
    generated_stems = {p.stem for p in WRITTEN}
    handwritten = [
        p.stem for p in VAULT.rglob("*.md")
        if ".obsidian" not in p.parts and p.stem not in generated_stems
        and not re.search(r"^generated:\s*true\s*$", read_text(p)[:400], re.M)]
    if handwritten:
        hp.append(f"\n## Hand-authored notes  <small>({len(handwritten)})</small>\n")
        hp.append(f"> [!note] These are yours — the builder never rewrites them. "
                  f"They are listed with everything else in {link('index')}, "
                  f"tagged _(hand-authored)_.\n")

    hp.append(f"\n---\n\n_Vault generated {STAMP} · see {link('Build Log')}._\n")
    if not DRY:
        (VAULT / F["meta"]).mkdir(parents=True, exist_ok=True)
        (VAULT / F["meta"] / "Home.md").write_text("\n".join(hp), encoding="utf-8")
    WRITTEN.append(VAULT / F["meta"] / "Home.md")

    print(f"\n[write] {len(WRITTEN)} notes")
    for k, v in sorted(stats.items()):
        print(f"   {k:<16} {v}")

    # Cross-links from memory notes may target workspace notes that this run did
    # not regenerate, so they must still count as resolvable.
    known_extra = {rec["stem"] for rec in inventory}
    written = flush_notes(known_extra=known_extra)
    print(f"[flush] {written} buffered notes written to disk")

    # index.md and log.md are what make this usable as an LLM wiki: the index is
    # read first to choose pages, the log records how the vault got here.
    from navfiles import append_log, write_index
    pages = write_index()
    print(f"[index] index.md catalogued {pages} pages")
    from bases import write_bases
    nb = write_bases()
    print(f"[bases] {nb} Bases views written to 00-Meta/Views/")
    append_log(
        "build" if not MEMORY_ONLY else "sync",
        f"{'full rebuild' if not MEMORY_ONLY else 'incremental sync'} — {pages} pages",
        detail=("- memory: {} facts · {} topics · {} entities · {} snapshots\n"
                "- notes written this run: {}\n"
                "- sources: {}".format(
                    len(mem["facts"]), len(mem["topics"]), len(mem["entities"]),
                    len(mem["snapshots"]), written,
                    "memory.db + session-store.db" if MEMORY_ONLY
                    else "workspace + memory.db + session-store.db")))

    configure_obsidian()

    if not DRY:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "last-build.json").write_text(json.dumps({
            "stamp": STAMP, "mode": "memory-only" if MEMORY_ONLY else "full",
            "notes": len(WRITTEN), "by_folder": dict(stats), "by_type": dict(counts),
        }, indent=1), encoding="utf-8")

        # Persist the inventory so the next memory-only run can rebuild without
        # rescanning the workspace. Read back from disk rather than
        # reconstructing from this run's bookkeeping — disk is authoritative,
        # and a note missing from the inventory would have its links escaped as
        # unresolved on the next incremental run.
        note_recs = []
        for p in sorted(VAULT.rglob("*.md")):
            if ".obsidian" in p.parts:
                continue
            head = read_text(p)[:400]
            if not re.search(r"^generated:\s*true\s*$", head, re.M):
                continue
            m = re.search(r"^origin:\s*(\w+)\s*$", head, re.M)
            note_recs.append({
                "stem": p.stem,
                "folder": p.relative_to(VAULT).as_posix().rsplit("/", 1)[0],
                "origin": m.group(1) if m else "workspace",
            })
        state = {
            "stamp": STAMP,
            "mode": "memory-only" if MEMORY_ONLY else "full",
            "notes": note_recs,
            "aliases": (cached.get("aliases") if MEMORY_ONLY else fresh_state["aliases"]),
            "doc_subjects": (cached.get("doc_subjects") if MEMORY_ONLY
                             else fresh_state["doc_subjects"]),
            "skill_notes": (cached.get("skill_notes") if MEMORY_ONLY
                            else fresh_state["skill_notes"]),
            "subject_docs": (cached.get("subject_docs") if MEMORY_ONLY
                             else fresh_state["subject_docs"]),
        }
        save_state(state)


if __name__ == "__main__":
    main()
