"""Drive vault_mcp.py over stdio exactly as the harness would."""
import json
import subprocess
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent
SERVER = BUILD / "vault_mcp.py"
sys.path.insert(0, str(BUILD))

requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "vault_search",
                "arguments": {"query": "content understanding cost", "limit": 3}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "vault_read", "arguments": {"page": "SAIF", "max_chars": 300}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "vault_index", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "vault_recent", "arguments": {"count": 3}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "vault_read", "arguments": {"page": "does-not-exist"}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "nope", "arguments": {}}},
]

payload = "\n".join(json.dumps(r) for r in requests) + "\n"
proc = subprocess.run([sys.executable, str(SERVER)], input=payload,
                      capture_output=True, text=True, timeout=180,
                      encoding="utf-8")

if proc.returncode != 0:
    print("SERVER EXITED", proc.returncode)
    print(proc.stderr[-2000:])

ok = fail = 0
for line in proc.stdout.splitlines():
    if not line.strip():
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        print("BAD JSON:", line[:120]); fail += 1; continue
    rid = msg.get("id")
    if "error" in msg:
        print(f"id={rid} ERROR {msg['error']['message']}")
        ok += 1  # id=8 is expected to be a clean error
        continue
    res = msg.get("result", {})
    if rid == 1:
        print(f"id=1 initialize -> {res['serverInfo']['name']} "
              f"proto={res['protocolVersion']}"); ok += 1
    elif rid == 2:
        names = [t["name"] for t in res["tools"]]
        print(f"id=2 tools/list -> {len(names)}: {', '.join(names)}")
        bad = [t["name"] for t in res["tools"]
               if not t.get("description") or "inputSchema" not in t]
        if bad:
            print("   MALFORMED:", bad); fail += 1
        else:
            ok += 1
    else:
        text = res.get("content", [{}])[0].get("text", "")
        first = text.splitlines()[0] if text else "(empty)"
        flag = "ERR" if res.get("isError") else "ok "
        print(f"id={rid} {flag} -> {first[:100]}")
        ok += 1

print(f"\n{ok} response(s) OK, {fail} problem(s)")
sys.exit(1 if fail else 0)
