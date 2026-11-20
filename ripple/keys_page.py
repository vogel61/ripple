"""The key page at `/keys` (FA-23).

Same rules as the dashboard: no build step, no CDN, no framework — it has to work
with the internet cable pulled. It is the only page that writes anything, and it
writes only through `/admin/keys`, which demands the admin key on every call. The
admin key lives in `sessionStorage` for the duration of the tab; it is never put
in the URL and never rendered into the page.

A key value is shown exactly once, at creation. After that only the mask
(`rip-abc…wxyz`) is visible — a page that can show a stored secret is a page that
can leak it.
"""

from __future__ import annotations


def render_keys_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>RIPPLE — Keys</title>
<style>
  body { font: 13px/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif;
         margin: 0; padding: 16px 20px; background: #0f1115; color: #d7dae0; }
  h1 { font-size: 16px; margin: 0 0 2px; letter-spacing: .08em; }
  .sub { color: #7c8390; margin-bottom: 16px; }
  a { color: #7aa2f7; text-decoration: none; }
  section { margin-bottom: 22px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .1em;
       color: #9aa3b2; margin: 0 0 8px; font-weight: 600; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #1e222a;
           font-weight: 400; }
  th { color: #6a7280; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; }
  td.num, th.num { text-align: right; }
  .muted { color: #6a7280; }
  .off { color: #b06a6a; }
  .on { color: #79b37a; }
  input, select, button { font: inherit; background: #171a21; color: #d7dae0;
                          border: 1px solid #2a2f3a; border-radius: 4px; padding: 4px 7px; }
  button { cursor: pointer; }
  button:hover { border-color: #3d4552; }
  button.danger:hover { border-color: #b06a6a; color: #e0a0a0; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .newkey { margin-top: 10px; padding: 10px; border: 1px solid #3d4552;
            border-radius: 6px; background: #141821; }
  .newkey code { font-family: ui-monospace, Menlo, monospace; font-size: 13px;
                 color: #e6c07b; word-break: break-all; }
  .hint { color: #6a7280; font-size: 12px; }
  .err { color: #e0a0a0; }
</style>
</head>
<body>
<h1>RIPPLE — KEYS</h1>
<div class="sub"><a href="/">← dashboard</a> · client keys for <code>:8010</code></div>

<section>
  <h2>Admin key</h2>
  <div class="row">
    <input id="admin" type="password" size="28" placeholder="admin key"
           oninput="sessionStorage.setItem('ripple_admin', this.value)">
    <button onclick="load()">unlock</button>
    <span id="state" class="hint"></span>
  </div>
  <div class="hint">Kept in this tab only (sessionStorage), sent as Bearer on each call.</div>
</section>

<section>
  <h2>Keys</h2>
  <table>
    <thead><tr>
      <th>name</th><th>key</th><th>kind</th><th>models</th><th>created</th>
      <th>state</th><th class="num">actions</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="7" class="muted">locked — enter the admin key</td></tr></tbody>
  </table>
  <div id="newkey"></div>
</section>

<section>
  <h2>New key</h2>
  <div class="row">
    <input id="name" size="18" placeholder="name (e.g. alice)">
    <select id="kind">
      <option value="interactive">interactive</option>
      <option value="batch">batch</option>
      <option value="one-shot">one-shot</option>
    </select>
    <input id="models" size="42" placeholder="models (comma separated, empty = all)">
    <button onclick="create()">create</button>
    <span id="msg" class="hint"></span>
  </div>
</section>

<script>
const $ = id => document.getElementById(id);
const admin = () => sessionStorage.getItem('ripple_admin') || '';
$('admin').value = admin();

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: {'Authorization': 'Bearer ' + admin(), 'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { data = {error: {message: text}}; }
  if (!r.ok) throw new Error((data.error && data.error.message) || r.status);
  return data;
}

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function load() {
  $('state').textContent = '';
  try {
    const d = await api('GET', '/admin/keys');
    render(d.keys);
    $('state').textContent = d.keys.length + ' key(s)';
  } catch (e) {
    $('rows').innerHTML = '<tr><td colspan="7" class="err">' + esc(e.message) + '</td></tr>';
    $('state').textContent = 'locked';
  }
}

function render(keys) {
  if (!keys.length) {
    $('rows').innerHTML = '<tr><td colspan="7" class="muted">no keys yet</td></tr>';
    return;
  }
  $('rows').innerHTML = keys.map(k => `
    <tr>
      <td>${esc(k.name)}</td>
      <td><code>${esc(k.key)}</code></td>
      <td>${esc(k.kind)}</td>
      <td class="muted">${k.models && k.models.length ? esc(k.models.join(', ')) : 'all'}</td>
      <td class="muted">${esc((k.created_at || '').slice(0, 16).replace('T', ' '))}</td>
      <td class="${k.enabled ? 'on' : 'off'}">${k.enabled ? 'active' : 'disabled'}</td>
      <td class="num">
        <button onclick="act('${esc(k.name)}','rotate')">rotate</button>
        <button onclick="act('${esc(k.name)}','${k.enabled ? 'disable' : 'enable'}')">
          ${k.enabled ? 'disable' : 'enable'}</button>
        <button class="danger" onclick="act('${esc(k.name)}','revoke')">revoke</button>
      </td>
    </tr>`).join('');
}

async function act(name, action) {
  if (action === 'revoke' && !confirm('revoke ' + name + '?')) return;
  try {
    const d = await api('POST', '/admin/keys/' + encodeURIComponent(name) + '/' + action);
    if (d.key) showNew(d);
    load();
  } catch (e) { $('msg').textContent = e.message; $('msg').className = 'err'; }
}

async function create() {
  $('msg').textContent = '';
  const models = $('models').value.split(',').map(s => s.trim()).filter(Boolean);
  try {
    const d = await api('POST', '/admin/keys',
                        {name: $('name').value.trim(), kind: $('kind').value, models});
    $('name').value = ''; $('models').value = '';
    showNew(d);
    load();
  } catch (e) { $('msg').textContent = e.message; $('msg').className = 'err'; }
}

function showNew(d) {
  $('newkey').innerHTML = '<div class="newkey">' +
    '<div class="hint">key for <b>' + esc(d.name) + '</b> — shown once, copy it now:</div>' +
    '<div class="row" style="margin-top:6px"><code>' + esc(d.key) + '</code>' +
    '<button onclick="navigator.clipboard.writeText(\\'' + esc(d.key) + '\\')">copy</button></div>' +
    '</div>';
}
</script>
</body>
</html>"""
