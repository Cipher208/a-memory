from __future__ import annotations

"""
Dashboard — HTML dashboard for memory visualization
"""

from pathlib import Path
from typing import Any

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ariel Memory Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;padding:20px}
h1{font-size:24px;margin-bottom:20px;color:#58a6ff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px}
.card h2{font-size:14px;color:#8b949e;margin-bottom:12px;text-transform:uppercase;letter-spacing:1px}
.stat{font-size:32px;font-weight:700;color:#58a6ff}
.stat-label{font-size:12px;color:#8b949e;margin-top:4px}
.row{display:flex;gap:8px;margin-top:8px}
.badge{display:inline-block;padding:4px 8px;border-radius:12px;font-size:12px;font-weight:600}
.badge-green{background:#1a3a2a;color:#3fb950}
.badge-blue{background:#1a2a3a;color:#58a6ff}
.badge-yellow{background:#3a3a1a;color:#d29922}
.badge-red{background:#3a1a1a;color:#f85149}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #30363d;font-size:13px}
th{color:#8b949e;font-weight:600}
td{color:#e1e4e8}
.bar{height:8px;border-radius:4px;background:#30363d;overflow:hidden;margin-top:4px}
.bar-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,#238636,#3fb950)}
.refresh{position:fixed;top:20px;right:20px;padding:8px 16px;background:#21262d;border:1px solid #30363d;border-radius:6px;color:#e1e4e8;cursor:pointer;font-size:13px}
.refresh:hover{background:#30363d}
#status{position:fixed;top:20px;right:140px;font-size:13px;color:#8b949e}
</style>
</head>
<body>
<h1>Ariel Memory Dashboard</h1>
<button class="refresh" onclick="load()">Refresh</button>
<span id="status"></span>

<div class="grid" id="stats"></div>

<div class="grid">
<div class="card">
<h2>User Memory (L4 Facts)</h2>
<div id="user-facts"></div>
</div>
<div class="card">
<h2>Agent Memory (L4 Facts)</h2>
<div id="agent-facts"></div>
</div>
</div>

<div class="grid">
<div class="card">
<h2>User Episodes</h2>
<div id="user-episodes"></div>
</div>
<div class="card">
<h2>Agent Episodes</h2>
<div id="agent-episodes"></div>
</div>
</div>

<div class="grid">
<div class="card" style="grid-column:span 2">
<h2>Recent Audit Log</h2>
<div id="audit"></div>
</div>
</div>

<script>
const API = window.location.origin + '/api';

async function load() {
  document.getElementById('status').textContent = 'Loading...';
  try {
    const [stats, userFacts, agentFacts, userEps, agentEps, audit] = await Promise.all([
      fetch(API+'/stats').then(r=>r.json()),
      fetch(API+'/user/facts').then(r=>r.json()),
      fetch(API+'/agent/facts').then(r=>r.json()),
      fetch(API+'/user/episodes').then(r=>r.json()),
      fetch(API+'/agent/episodes').then(r=>r.json()),
      fetch(API+'/audit').then(r=>r.json()),
    ]);
    renderStats(stats);
    renderFacts('user-facts', userFacts);
    renderFacts('agent-facts', agentFacts);
    renderEpisodes('user-episodes', userEps);
    renderEpisodes('agent-episodes', agentEps);
    renderAudit(audit);
    document.getElementById('status').textContent = 'Updated '+new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('status').textContent = 'Error: '+e.message;
  }
}

function renderStats(s) {
  const html = [
    stat('L1 Buffer', s.l1_buffer, 'blue'),
    stat('L2 Sessions', s.l2_sessions, 'green'),
    stat('L3 Episodes', s.l3_episodes, 'yellow'),
    stat('L4 Facts', s.l4_facts, 'blue'),
    stat('Wiki Pages', s.wiki_pages, 'green'),
    stat('Graph Nodes', s.graph_nodes, 'yellow'),
  ].join('');
  document.getElementById('stats').innerHTML = html;
}

function stat(label, value, color) {
  return `<div class="card"><h2>${label}</h2><div class="stat">${value}</div></div>`;
}

function renderFacts(id, items) {
  if (!items.length) { document.getElementById(id).innerHTML='<p style="color:#8b949e">No facts yet</p>'; return; }
  let html = '<table><tr><th>Key</th><th>Value</th><th>Importance</th></tr>';
  items.forEach(f => {
    const pct = Math.round(f.importance*100);
    html += `<tr><td>${esc(f.key)}</td><td>${esc(f.value)}</td><td><div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>${pct}%</td></tr>`;
  });
  html += '</table>';
  document.getElementById(id).innerHTML = html;
}

function renderEpisodes(id, items) {
  if (!items.length) { document.getElementById(id).innerHTML='<p style="color:#8b949e">No episodes yet</p>'; return; }
  let html = '<table><tr><th>Summary</th><th>Weight</th><th>Tags</th></tr>';
  items.forEach(e => {
    const pct = Math.round(e.weight*100);
    const tags = (e.tags||[]).map(t=>`<span class="badge badge-blue">${esc(t)}</span>`).join(' ');
    html += `<tr><td>${esc(e.summary)}</td><td><div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>${pct}%</td><td>${tags}</td></tr>`;
  });
  html += '</table>';
  document.getElementById(id).innerHTML = html;
}

function renderAudit(items) {
  if (!items.length) { document.getElementById('audit').innerHTML='<p style="color:#8b949e">No audit entries</p>'; return; }
  let html = '<table><tr><th>Time</th><th>Action</th><th>User</th></tr>';
  items.slice(0,20).forEach(a => {
    const t = new Date(a.timestamp*1000).toLocaleString();
    html += `<tr><td>${t}</td><td><span class="badge badge-green">${esc(a.action)}</span></td><td>${esc(a.user_id)}</td></tr>`;
  });
  html += '</table>';
  document.getElementById('audit').innerHTML = html;
}

function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
load();
</script>
</body>
</html>"""


class Dashboard:
    def __init__(self, mm: Any | None = None, data_dir: str | None = None) -> None:
        self.data_dir = Path(data_dir or str(Path.home() / ".mcp-ariel-memory"))
        if mm is None:
            import sys

            sys.path.insert(0, str(Path(__file__).parent.parent))
            from core import MemoryManager
            from shared.cache import MemoryCache

            mm = MemoryManager(cache=MemoryCache())
        self.mm = mm

    async def get_stats(self, user_id: str = "default") -> dict[str, Any]:
        from graph.epistemic import EpistemicGraph
        from wiki import WikiManager

        uw = WikiManager(layer="user")
        aw = WikiManager(layer="agent")
        ug = EpistemicGraph(layer="user")

        um = self.mm.user_memory(user_id)
        am = self.mm.agent_memory(user_id)

        return {
            "l1_buffer": um.l1.size(),
            "l2_sessions": await um.l2.count_sessions(user_id),
            "l3_episodes": await um.l3.count(user_id),
            "l4_facts": await um.l4.count(user_id),
            "wiki_pages": await uw.count(user_id),
            "graph_nodes": await ug.count_nodes(user_id),
            "agent_l1": am.l1.size(),
            "agent_l2": await am.l2.count_sessions(user_id),
            "agent_l3": await am.l3.count(user_id),
            "agent_l4": await am.l4.count(user_id),
            "agent_wiki": await aw.count(user_id),
        }

    async def get_user_facts(self, user_id: str = "default") -> list[dict[str, Any]]:
        facts = await self.mm.user_memory(user_id).l4.get_all(user_id, limit=50)
        return [{"key": f.key, "value": f.value, "importance": f.importance} for f in facts]

    async def get_agent_facts(self, user_id: str = "default") -> list[dict[str, Any]]:
        facts = await self.mm.agent_memory(user_id).l4.get_all(user_id, limit=50)
        return [{"key": f.key, "value": f.value, "importance": f.importance} for f in facts]

    async def get_user_episodes(self, user_id: str = "default") -> list[dict[str, Any]]:
        eps = await self.mm.user_memory(user_id).l3.get_episodes(user_id, limit=20)
        return [{"summary": e.summary, "weight": e.emotional_weight, "tags": e.tags} for e in eps]

    async def get_agent_episodes(self, user_id: str = "default") -> list[dict[str, Any]]:
        eps = await self.mm.agent_memory(user_id).l3.get_episodes(user_id, limit=20)
        return [{"summary": e.summary, "weight": e.emotional_weight, "tags": e.tags} for e in eps]

    async def get_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        from features.audit_trail import AuditTrail

        at = AuditTrail()
        return await at.get_history("default", limit=limit)

    def render_html(self) -> str:
        return DASHBOARD_HTML
