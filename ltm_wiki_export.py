#!/usr/bin/env python3
"""
ltm_wiki_export.py — Generate a browsable markdown wiki from the LTM database.

Reads memory.db (read-only), writes markdown pages to ~/.copilot/ltm-wiki/.
Overwrites on each run (idempotent).

Usage:
  echo '{}' | python ltm_wiki_export.py
  echo '{"db_path": "path/to/memory.db"}' | python ltm_wiki_export.py
"""

import json
import sys
import sqlite3
import os
import io
import shutil
import datetime

sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

DEFAULT_DB = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".copilot", "memory.db")
DEFAULT_WIKI = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".copilot", "ltm-wiki")

MAX_FACTS_PER_TOPIC = 20


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_conn(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _stars(n):
    return "⭐" * max(1, min(5, n))


def _slug_safe(text):
    """Convert text to a filename-safe slug."""
    return text.lower().replace(" ", "-").replace("/", "-").replace("\\", "-")


# ── Query helpers ─────────────────────────────────────────────────────

def fetch_all_topics(conn):
    return conn.execute(
        "SELECT id, slug, title, category, description, status, last_accessed_at, created_at "
        "FROM topics ORDER BY last_accessed_at DESC NULLS LAST, title"
    ).fetchall()


def fetch_facts_for_topic(conn, topic_id):
    return conn.execute(
        "SELECT id, content, fact_type, confidence, importance, status, source, created_at "
        "FROM facts WHERE topic_id = ? "
        "ORDER BY importance DESC, created_at DESC",
        (topic_id,),
    ).fetchall()


def fetch_global_facts(conn):
    return conn.execute(
        "SELECT id, content, fact_type, confidence, importance, status, source, created_at "
        "FROM facts WHERE topic_id IS NULL "
        "ORDER BY importance DESC, created_at DESC"
    ).fetchall()


def fetch_snapshots_for_topic(conn, topic_id):
    return conn.execute(
        "SELECT seq_number, title, summary, findings, decisions, open_questions, next_steps, created_at "
        "FROM snapshots WHERE topic_id = ? ORDER BY seq_number",
        (topic_id,),
    ).fetchall()


def fetch_entities(conn):
    return conn.execute(
        "SELECT id, name, entity_type, is_self, attributes, notes "
        "FROM entities ORDER BY entity_type, name"
    ).fetchall()


def fetch_entity_topic_links(conn, entity_id):
    """Get topic slugs an entity is mentioned in."""
    rows = conn.execute(
        "SELECT DISTINCT t.slug FROM entity_mentions em "
        "JOIN topics t ON em.parent_id = t.id AND em.parent_type = 'topic' "
        "WHERE em.entity_id = ? "
        "UNION "
        "SELECT DISTINCT t.slug FROM entity_mentions em "
        "JOIN facts f ON em.parent_id = f.id AND em.parent_type = 'fact' "
        "JOIN topics t ON f.topic_id = t.id "
        "WHERE em.entity_id = ?",
        (entity_id, entity_id),
    ).fetchall()
    return [r[0] for r in rows]


def fetch_entity_mentions_for_topic(conn, topic_id):
    """Entities mentioned in a topic or its facts."""
    return conn.execute(
        "SELECT DISTINCT e.name, e.entity_type FROM entities e "
        "JOIN entity_mentions em ON em.entity_id = e.id "
        "WHERE (em.parent_type = 'topic' AND em.parent_id = ?) "
        "   OR (em.parent_type = 'fact' AND em.parent_id IN "
        "       (SELECT id FROM facts WHERE topic_id = ?)) "
        "   OR (em.parent_type = 'snapshot' AND em.parent_id IN "
        "       (SELECT id FROM snapshots WHERE topic_id = ?)) "
        "ORDER BY e.entity_type, e.name",
        (topic_id, topic_id, topic_id),
    ).fetchall()


def fetch_todo_facts(conn):
    return conn.execute(
        "SELECT f.id, f.content, f.status, f.created_at, t.slug AS topic_slug "
        "FROM facts f LEFT JOIN topics t ON t.id = f.topic_id "
        "WHERE f.fact_type = 'todo' "
        "ORDER BY f.status ASC, f.created_at DESC"
    ).fetchall()


def fetch_preference_facts(conn):
    return conn.execute(
        "SELECT f.content, t.slug AS topic_slug "
        "FROM facts f LEFT JOIN topics t ON t.id = f.topic_id "
        "WHERE f.fact_type = 'preference' AND f.status = 'active' "
        "ORDER BY t.slug, f.created_at DESC"
    ).fetchall()


def count_rows(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ── Page generators ───────────────────────────────────────────────────

def generate_topic_page(conn, topic):
    facts = fetch_facts_for_topic(conn, topic["id"])
    snapshots = fetch_snapshots_for_topic(conn, topic["id"])
    entities = fetch_entity_mentions_for_topic(conn, topic["id"])

    lines = [
        "---",
        f"type: topic",
        f"slug: {topic['slug']}",
        f"category: {topic['category']}",
        f"status: {topic['status']}",
        f"last_accessed: {topic['last_accessed_at'] or 'never'}",
        "---",
        f"# {topic['title']}",
        "",
    ]

    if topic["description"]:
        lines += [topic["description"], ""]

    # Facts
    lines.append("## Facts")
    if not facts:
        lines.append("_No facts recorded yet._")
    else:
        shown = facts[:MAX_FACTS_PER_TOPIC]
        for f in shown:
            star = _stars(f["importance"])
            src = f" — _Source: {f['source']}_" if f["source"] else ""
            prefix = "~~" if f["status"] in ("superseded", "disproven") else ""
            suffix = "~~" if prefix else ""
            lines.append(f"- {star} **[{f['fact_type']}]** {prefix}{f['content']}{suffix}{src}")
        if len(facts) > MAX_FACTS_PER_TOPIC:
            lines.append(f"- _…and {len(facts) - MAX_FACTS_PER_TOPIC} more facts (showing top {MAX_FACTS_PER_TOPIC} by importance)_")
    lines.append("")

    # Snapshots
    if snapshots:
        lines.append("## Snapshots")
        for s in snapshots:
            date = (s["created_at"] or "")[:10]
            lines.append(f"### Snapshot #{s['seq_number']}: {s['title']} ({date})")
            if s["summary"]:
                lines.append(f"**Summary:** {s['summary']}")
            if s["findings"]:
                lines.append(f"**Findings:** {s['findings']}")
            if s["decisions"]:
                lines.append(f"**Decisions:** {s['decisions']}")
            if s["open_questions"]:
                lines.append(f"**Open Questions:** {s['open_questions']}")
            if s["next_steps"]:
                lines.append(f"**Next Steps:** {s['next_steps']}")
            lines.append("")

    # Related entities
    if entities:
        lines.append("## Related Entities")
        for e in entities:
            lines.append(f"- **{e['entity_type']}:** {e['name']}")
        lines.append("")

    lines.append(f"[← Back to Index](../index.md)")
    return "\n".join(lines)


def generate_entity_page(entities, entity_type, title, conn):
    filtered = [e for e in entities if e["entity_type"] == entity_type]
    lines = [f"# {title}", ""]

    if not filtered:
        lines.append(f"_No {title.lower()} tracked yet._")
    else:
        for e in filtered:
            lines.append(f"## {e['name']}")
            lines.append(f"- **Type:** {e['entity_type']}")
            if e["notes"]:
                lines.append(f"- **Notes:** {e['notes']}")
            if e["attributes"]:
                lines.append(f"- **Attributes:** {e['attributes']}")
            topic_slugs = fetch_entity_topic_links(conn, e["id"])
            if topic_slugs:
                links = ", ".join(f"[{s}](../topics/{s}.md)" for s in topic_slugs)
                lines.append(f"- **Mentioned in:** {links}")
            lines.append("")

    lines.append("[← Back to Index](../index.md)")
    return "\n".join(lines)


def generate_pending_page(conn):
    todos = fetch_todo_facts(conn)
    lines = ["# Pending Work", ""]
    if not todos:
        lines.append("_No pending work items._")
    else:
        for t in todos:
            date = (t["created_at"] or "")[:10]
            slug = t["topic_slug"] or "general"
            check = "x" if t["status"] == "resolved" else " "
            lines.append(f"- [{check}] {date} [{slug}] {t['content']}")
    lines += ["", "[← Back to Index](index.md)"]
    return "\n".join(lines)


def generate_preferences_page(conn):
    prefs = fetch_preference_facts(conn)
    lines = ["# Preferences", ""]
    if not prefs:
        lines.append("_No preferences recorded yet._")
    else:
        for p in prefs:
            slug = p["topic_slug"] or "general"
            lines.append(f"- **[{slug}]** {p['content']}")
    lines += ["", "[← Back to Index](index.md)"]
    return "\n".join(lines)


def generate_index(topics, entities, stats, todo_count, pref_count):
    now = _now()
    lines = [
        "# LTM Knowledge Wiki",
        "",
        f"Generated: {now}",
        f"Stats: {stats['topics']} topics, {stats['facts']} facts, {stats['entities']} entities, {stats['snapshots']} snapshots",
        "",
        "## Topics",
    ]
    if not topics:
        lines.append("_No topics yet._")
    else:
        for t in topics:
            fact_count = t["_fact_count"]
            accessed = t["last_accessed_at"] or "never"
            lines.append(
                f"- [{t['title']}](topics/{t['slug']}.md) — {t['category']} | {fact_count} facts | last accessed: {accessed}"
            )

    # Entity counts by type
    type_map = {}
    for e in entities:
        type_map.setdefault(e["entity_type"], []).append(e)

    person_count = len(type_map.get("person", []))
    account_count = len(type_map.get("account", []))
    product_count = len(type_map.get("product", [])) + len(type_map.get("tool", []))

    lines += [
        "",
        "## Entities",
        f"- [People](entities/people.md) — {person_count} people tracked",
        f"- [Accounts](entities/accounts.md) — {account_count} accounts tracked",
        f"- [Products & Tools](entities/products.md) — {product_count} items",
        "",
        "## Quick Links",
        f"- [Pending Work](pending.md) — {todo_count} open items",
        f"- [Preferences](preferences.md) — {pref_count} preferences recorded",
    ]
    return "\n".join(lines)


def generate_log(stats, topics_exported, entities_exported, facts_exported, wiki_path):
    now = _now()
    return "\n".join([
        "# Export Log",
        "",
        f"## {now}",
        f"- Wiki path: `{wiki_path}`",
        f"- Topics exported: {topics_exported}",
        f"- Entities exported: {entities_exported}",
        f"- Facts exported: {facts_exported}",
        f"- Snapshots in DB: {stats['snapshots']}",
        f"- Schema version: {stats['schema_version']}",
    ])


# ── Main export ───────────────────────────────────────────────────────

def export_wiki(payload):
    db_path = payload.get("db_path") or DEFAULT_DB
    wiki_path = payload.get("wiki_path") or DEFAULT_WIKI

    if not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path}"}

    conn = get_conn(db_path)
    try:
        stats = {
            "topics": count_rows(conn, "topics"),
            "facts": count_rows(conn, "facts"),
            "entities": count_rows(conn, "entities"),
            "snapshots": count_rows(conn, "snapshots"),
            "schema_version": conn.execute("SELECT version FROM schema_version").fetchone()[0],
        }

        topics = fetch_all_topics(conn)
        entities = fetch_entities(conn)

        # Attach fact counts to topics for the index
        topic_dicts = []
        for t in topics:
            d = dict(t)
            d["_fact_count"] = conn.execute(
                "SELECT count(*) FROM facts WHERE topic_id = ?", (t["id"],)
            ).fetchone()[0]
            topic_dicts.append(d)

        todo_facts = fetch_todo_facts(conn)
        todo_open = sum(1 for t in todo_facts if t["status"] != "resolved")
        pref_facts = fetch_preference_facts(conn)

        # Wipe and recreate wiki directory
        if os.path.exists(wiki_path):
            shutil.rmtree(wiki_path)
        os.makedirs(os.path.join(wiki_path, "topics"), exist_ok=True)
        os.makedirs(os.path.join(wiki_path, "entities"), exist_ok=True)

        # Generate topic pages
        topics_exported = 0
        facts_exported = 0
        for td in topic_dicts:
            page = generate_topic_page(conn, td)
            with open(os.path.join(wiki_path, "topics", f"{td['slug']}.md"), "w", encoding="utf-8") as f:
                f.write(page)
            topics_exported += 1
            facts_exported += td["_fact_count"]

        # Count global facts too
        global_facts = fetch_global_facts(conn)
        facts_exported += len(global_facts)

        # Entity pages
        entities_exported = len(entities)
        people_page = generate_entity_page(entities, "person", "People", conn)
        accounts_page = generate_entity_page(entities, "account", "Accounts", conn)
        # Combine product + tool entities
        product_entities = [e for e in entities if e["entity_type"] in ("product", "tool")]
        products_lines = [f"# Products & Tools", ""]
        if not product_entities:
            products_lines.append("_No products or tools tracked yet._")
        else:
            for e in product_entities:
                products_lines.append(f"## {e['name']}")
                products_lines.append(f"- **Type:** {e['entity_type']}")
                if e["notes"]:
                    products_lines.append(f"- **Notes:** {e['notes']}")
                if e["attributes"]:
                    products_lines.append(f"- **Attributes:** {e['attributes']}")
                topic_slugs = fetch_entity_topic_links(conn, e["id"])
                if topic_slugs:
                    links = ", ".join(f"[{s}](../topics/{s}.md)" for s in topic_slugs)
                    products_lines.append(f"- **Mentioned in:** {links}")
                products_lines.append("")
        products_lines.append("[← Back to Index](../index.md)")
        products_page = "\n".join(products_lines)

        with open(os.path.join(wiki_path, "entities", "people.md"), "w", encoding="utf-8") as f:
            f.write(people_page)
        with open(os.path.join(wiki_path, "entities", "accounts.md"), "w", encoding="utf-8") as f:
            f.write(accounts_page)
        with open(os.path.join(wiki_path, "entities", "products.md"), "w", encoding="utf-8") as f:
            f.write(products_page)

        # Singleton pages
        pending_page = generate_pending_page(conn)
        with open(os.path.join(wiki_path, "pending.md"), "w", encoding="utf-8") as f:
            f.write(pending_page)

        prefs_page = generate_preferences_page(conn)
        with open(os.path.join(wiki_path, "preferences.md"), "w", encoding="utf-8") as f:
            f.write(prefs_page)

        # Index
        index_page = generate_index(topic_dicts, entities, stats, todo_open, len(pref_facts))
        with open(os.path.join(wiki_path, "index.md"), "w", encoding="utf-8") as f:
            f.write(index_page)

        # Log
        log_page = generate_log(stats, topics_exported, entities_exported, facts_exported, wiki_path)
        with open(os.path.join(wiki_path, "log.md"), "w", encoding="utf-8") as f:
            f.write(log_page)

        return {
            "wiki_path": wiki_path,
            "topics_exported": topics_exported,
            "entities_exported": entities_exported,
            "facts_exported": facts_exported,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.read())
        result = export_wiki(payload)
        print(json.dumps(result, default=str))
        sys.exit(0 if "error" not in result else 1)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
        sys.exit(1)
