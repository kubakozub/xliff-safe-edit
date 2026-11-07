'use strict';
const $ = s => document.querySelector(s);
let FID = null, UNITS = new Map(), SEL = null, TOTAL = 0, NEXT = null, BUSY = false;
// The target language the loaded file declares, used for the spell checker.
let TARGET_LANG = '';
const LIM = 300;

function banner(msg) {
  const b = $('#banner');
  if (!msg) { b.className = ''; b.textContent = ''; return; }
  b.textContent = msg; b.className = 'on';
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ 'X-XLIFF-Edit': '1' }, opts.headers || {});
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (!r.ok) {
    let m = r.status + ' ' + r.statusText;
    if (ct.includes('json')) { try { m = (await r.json()).error || m; } catch (e) { /* puste */ } }
    throw new Error(m);
  }
  return ct.includes('json') ? r.json() : r;
}

// ---------- znaczniki i tokeny ----------

function tagChip(t) {
  const s = document.createElement('span');
  s.className = 'tag';
  s.contentEditable = 'false';
  s.textContent = t.label || 'tag';          // textContent, nigdy innerHTML
  s.dataset.ref = String(t.ref);
  s.dataset.name = t.name || '';
  s.title = (t.name || '') + (t.id ? ' id=' + t.id : '');
  return s;
}

function renderTokens(toks, el) {
  el.textContent = '';
  for (const t of toks) {
    if (t.kind === 'tag') el.appendChild(tagChip(t));
    else for (const [i, part] of t.text.split('\n').entries()) {
      if (i) el.appendChild(document.createElement('br'));
      if (part) el.appendChild(document.createTextNode(part));
    }
  }
}

const BLOCKS = new Set(['DIV', 'P', 'LI', 'TR', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'PRE']);

function readTokens(el) {
  const out = [];
  const push = (t) => {
    const p = out[out.length - 1];
    if (t.kind === 'text' && p && p.kind === 'text') p.text += t.text; else out.push(t);
  };
  const walk = (n, top) => {
    let first = true;
    for (const c of n.childNodes) {
      if (c.nodeType === 3) { if (c.nodeValue) push({ kind: 'text', text: c.nodeValue }); }
      else if (c.nodeType === 1 && c.classList && c.classList.contains('tag'))
        push({ kind: 'tag', ref: isNaN(Number(c.dataset.ref)) ? c.dataset.ref : Number(c.dataset.ref) });
      else if (c.nodeType === 1) {
        if (c.tagName === 'BR') push({ kind: 'text', text: '\n' });
        else {
          if (BLOCKS.has(c.tagName) && !first && out.length) push({ kind: 'text', text: '\n' });
          walk(c, false);
        }
      }
      first = false;
    }
  };
  walk(el, true);
  return out;
}

function sameTokens(a, b) {
  const norm = ts => ts.filter(t => t.kind !== 'text' || t.text !== '')
    .map(t => t.kind === 'tag' ? 'T' + t.ref : 'X' + t.text).join('\u0001');
  return norm(a) === norm(b);
}

// ---------- siatka ----------

function rowFor(u) {
  const tr = document.createElement('tr');
  tr.dataset.key = u.key;
  if (u.translate === 'no') tr.className = 'nt';
  if (u.dirty) tr.classList.add('edited');

  const tdId = document.createElement('td');
  tdId.className = 'id';
  tdId.textContent = u.id || ('#' + u.key);
  const tdSt = document.createElement('td');
  tdSt.className = 'st';
  const dot = document.createElement('span');
  dot.className = 'dot ' + (u.dirty ? 'e' : (u.target_text.trim() ? 't' : 'u'));
  dot.title = u.dirty ? 'edited' : (u.target_text.trim() ? 'translated' : 'empty');
  tdSt.appendChild(dot);

  const tdS = document.createElement('td');
  tdS.className = 'src';
  renderTokens(u.source, tdS);
  const tdT = document.createElement('td');
  tdT.className = 'tgt';
  const ed = document.createElement('div');
  ed.contentEditable = (u.translate === 'no') ? 'false' : 'true';
  ed.spellcheck = true;
  // The target language comes from the file's own <file target-language>. It was
  // hardcoded to one language, which gave every other target the wrong spell
  // checker. Left unset when the file does not declare one, because a wrong lang
  // is worse than none.
  if (TARGET_LANG) ed.lang = TARGET_LANG;
  renderTokens(u.target, ed);
  ed.addEventListener('focus', () => select(u.key));
  ed.addEventListener('blur', () => commit(u.key, ed));
  ed.addEventListener('paste', onPaste);
  tdT.appendChild(ed);

  tr.append(tdId, tdSt, tdS, tdT);
  return tr;
}

function onPaste(e) {
  e.preventDefault();
  const txt = (e.clipboardData || window.clipboardData).getData('text/plain');
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const r = sel.getRangeAt(0);
  r.deleteContents();
  const frag = document.createDocumentFragment();
  txt.split('\n').forEach((line, i) => {
    if (i) frag.appendChild(document.createElement('br'));
    if (line) frag.appendChild(document.createTextNode(line));
  });
  const last = frag.lastChild;
  r.insertNode(frag);
  if (last) { r.setStartAfter(last); r.collapse(true); sel.removeAllRanges(); sel.addRange(r); }
}

function select(key) {
  SEL = key;
  document.querySelectorAll('#grid tr.sel').forEach(t => t.classList.remove('sel'));
  const tr = document.querySelector('#grid tr[data-key="' + CSS.escape(key) + '"]');
  if (tr) tr.classList.add('sel');
}

async function commit(key, ed) {
  const u = UNITS.get(key);
  if (!u) return;
  const toks = readTokens(ed);
  if (sameTokens(toks, u.target)) return;
  const tr = ed.closest('tr');
  try {
    const res = await api('/api/file/' + FID + '/segment', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ key: key, tokens: toks, state: 'translated' })
    });
    UNITS.set(key, res);
    if (tr) {
      tr.classList.remove('unsaved');
      tr.classList.toggle('edited', res.dirty);
      const d = tr.querySelector('.dot');
      d.className = 'dot ' + (res.dirty ? 'e' : (res.target_text.trim() ? 't' : 'u'));
    }
    banner('');
    refreshStats();
  } catch (e) {
    // Do NOT restore the previous content here: doing that deleted the text the
    // translator had just typed.
    if (tr) tr.classList.add('unsaved');
    banner('Segment ' + (u.id || key) + ' was NOT saved: ' + e.message +
           '. Your text is still in the field. Fix the connection and click the ' +
           'segment again.');
  }
}

async function load(reset) {
  if (!FID || BUSY) return;
  BUSY = true;
  try {
    if (reset) { NEXT = null; UNITS = new Map(); }
    const p = new URLSearchParams({ limit: String(LIM), filter: $('#filter').value, q: $('#q').value });
    if (!reset && NEXT !== null) p.set('after', NEXT);
    const d = await api('/api/file/' + FID + '?' + p);
    TOTAL = d.total;
    NEXT = d.next;
    if (d.target_language) TARGET_LANG = d.target_language;
    const grid = $('#grid');
    if (reset) {
      grid.textContent = '';
      const tbl = document.createElement('table');
      const th = document.createElement('thead');
      const hr = document.createElement('tr');
      for (const t of ['ID', '', 'Source', 'Target']) {
        const c = document.createElement('th'); c.textContent = t; hr.appendChild(c);
      }
      th.appendChild(hr); tbl.appendChild(th);
      tbl.appendChild(document.createElement('tbody'));
      grid.appendChild(tbl);
    }
    const tb = grid.querySelector('tbody');
    for (const u of d.units) { UNITS.set(u.key, u); tb.appendChild(rowFor(u)); }
    statsBox(d.stats);
    banner('');
  } catch (e) {
    banner('Could not load segments: ' + e.message);
  } finally {
    BUSY = false;                       // without this one error locked the editor forever
  }
}

function statsBox(s) {
  const box = $('#stats');
  box.textContent = '';
  const line = (n, txt) => {
    const d = document.createElement('div'); d.className = 'row';
    const p = document.createElement('span'); p.className = 'pill'; p.textContent = String(n);
    d.appendChild(p); d.appendChild(document.createTextNode(' ' + txt));
    return d;
  };
  box.appendChild(line(s.translatable, 'translatable'));
  box.appendChild(line(s.translated, 'z targetem'));
  box.appendChild(line(s.edited, 'zmienionych w edytorze'));
  const m = document.createElement('div');
  m.className = 'row muted';
  m.textContent = 'showing ' + UNITS.size + ' of ' + TOTAL + (NEXT !== null ? ' (scroll for more)' : '');
  box.appendChild(m);
}

async function refreshStats() {
  try {
    const p = new URLSearchParams({ limit: '1', filter: $('#filter').value });
    const d = await api('/api/file/' + FID + '?' + p);
    TOTAL = d.total;
    statsBox(d.stats);
  } catch (e) { /* statistics are not critical */ }
}

$('#grid').addEventListener('scroll', e => {
  const el = e.target;
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 600 && NEXT !== null && !BUSY) load(false);
});

document.addEventListener('keydown', e => {
  const ed = document.activeElement;
  const inEditor = ed && ed.isContentEditable;
  if (e.ctrlKey && e.key === 'Enter' && inEditor) {
    e.preventDefault(); ed.blur();
    const tr = ed.closest('tr'), nx = tr && tr.nextElementSibling;
    const n2 = nx && nx.querySelector('[contenteditable=true]');
    if (n2) n2.focus();
  }
  if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    e.preventDefault();
    const tr = document.querySelector('#grid tr.sel') || document.querySelector('#grid tbody tr');
    if (!tr) return;
    const nx = e.key === 'ArrowDown' ? tr.nextElementSibling : tr.previousElementSibling;
    const n2 = nx && nx.querySelector('[contenteditable=true]');
    if (n2) n2.focus();
  }
  if (e.ctrlKey && e.shiftKey && (e.key === 'K' || e.key === 'k') && inEditor) {
    e.preventDefault();
    const u = UNITS.get(ed.closest('tr').dataset.key);
    if (u) renderTokens(u.source, ed);
  }
  if (e.ctrlKey && !e.shiftKey && /^[1-9]$/.test(e.key) && inEditor) {
    const u = UNITS.get(ed.closest('tr').dataset.key);
    if (!u) return;
    const tags = u.source.filter(t => t.kind === 'tag');
    const t = tags[parseInt(e.key, 10) - 1];
    if (!t) return;
    e.preventDefault();
    const sel = window.getSelection(), chip = tagChip(t);
    if (sel.rangeCount) {
      const r = sel.getRangeAt(0);
      r.insertNode(chip); r.setStartAfter(chip); r.collapse(true);
      sel.removeAllRanges(); sel.addRange(r);
    } else ed.appendChild(chip);
  }
});

// ---------- pliki ----------

$('#up').onclick = () => $('#file').click();
$('#file').onchange = e => { if (e.target.files[0]) upload(e.target.files[0]); };
$('#gloss').onclick = () => $('#glossfile').click();
$('#glossfile').onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const d = await api('/api/glossary?name=' + encodeURIComponent(f.name),
                        { method: 'POST', body: await f.arrayBuffer() });
    banner(''); alert('Glossary terms loaded: ' + d.terms);
  } catch (err) {
    banner('Glossary not loaded: ' + err.message +
           '. Quality checks will run WITHOUT terminology.');
  }
};

async function upload(f) {
  try {
    const d = await api('/api/upload?name=' + encodeURIComponent(f.name),
                        { method: 'POST', body: await f.arrayBuffer() });
    await listFiles();
    $('#files').value = d.id; FID = d.id;
    await load(true);
  } catch (e) { banner('Nie wczytano pliku: ' + e.message); }
}

const grid = $('#grid');
grid.addEventListener('dragover', e => { e.preventDefault(); const d = $('#drop'); if (d) d.classList.add('over'); });
grid.addEventListener('dragleave', () => { const d = $('#drop'); if (d) d.classList.remove('over'); });
grid.addEventListener('drop', e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f); });

async function listFiles() {
  const d = await api('/api/files');
  const s = $('#files');
  s.textContent = '';
  for (const m of d.files) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = m.name + '  (' + m.units + ' segm.' + (m.edits ? ', ' + m.edits + ' zm.' : '') + ')';
    s.appendChild(o);
  }
  if (!FID && d.files.length) { FID = d.files[0].id; s.value = FID; await load(true); }
}

$('#files').onchange = async e => { FID = e.target.value; await load(true); };
$('#filter').onchange = () => load(true);
let qt = null;
$('#q').oninput = () => { clearTimeout(qt); qt = setTimeout(() => load(true), 250); };
$('#dl').onclick = () => { if (FID) location.href = '/api/file/' + FID + '/download'; };

// ---------- quality checks ----------

$('#qa').onclick = async () => {
  if (!FID) return;
  $('#issues').textContent = 'Sprawdzam…';
  try {
    const d = await api('/api/file/' + FID + '/qa');
    const box = $('#issues');
    box.textContent = '';
    $('#issuesH').textContent = 'Quality checks: ' + d.count;
    const sum = document.createElement('div');
    sum.className = 'muted';
    sum.style.fontSize = '12px';
    sum.textContent = Object.entries(d.summary).map(([k, v]) => k + ': ' + v).join(' · ');
    box.appendChild(sum);
    if (d.truncated) {
      const t = document.createElement('div');
      t.className = 'muted';
      t.textContent = 'Showing ' + d.issues.length + ' of ' + d.count +
                      ', ' + d.truncated + ' not listed.';
      box.appendChild(t);
    }
    const shown = d.issues.slice(0, 500);
    for (const i of shown) {
      const el = document.createElement('div');
      el.className = 'issue' + (i.severity === 'error' ? ' error' : '');
      const u = document.createElement('span');
      u.className = 'u';
      // textContent, never innerHTML: the id comes from the uploaded file
      u.textContent = i.unit || ('#' + i.key);
      el.appendChild(u);
      el.appendChild(document.createTextNode(' · ' + i.rule));
      el.appendChild(document.createElement('br'));
      el.appendChild(document.createTextNode(i.message));
      el.onclick = () => jump(i.key);
      box.appendChild(el);
    }
    if (d.issues.length > shown.length) {
      const t = document.createElement('div');
      t.className = 'muted';
      t.textContent = 'The list shows the first ' + shown.length + '.';
      box.appendChild(t);
    }
  } catch (e) { $('#issues').textContent = 'Quality check failed: ' + e.message; }
};

async function jump(key) {
  $('#filter').value = 'all';
  $('#q').value = String(key);
  await load(true);
  const tr = document.querySelector('#grid tr[data-key="' + CSS.escape(String(key)) + '"]');
  if (tr) {
    tr.scrollIntoView({ block: 'center' });
    select(String(key));
    const e2 = tr.querySelector('[contenteditable=true]');
    if (e2) e2.focus();
  }
}

// ---------- zamiana ----------

async function repl(dry) {
  if (!FID) return;
  const body = {
    find: $('#rf').value, replace: $('#rr').value,
    case: $('#rc').checked, whole: $('#rw').checked, dry: dry
  };
  const out = $('#rout');
  out.textContent = '';
  try {
    const d = await api('/api/file/' + FID + '/replace', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body)
    });
    const head = document.createElement('div');
    head.textContent = (dry ? 'Preview: ' : 'Changed: ') + d.count + ' segments';
    out.appendChild(head);
    for (const c of d.changes.slice(0, 6)) {
      const line = document.createElement('div');
      line.className = 'chg';
      const b = document.createElement('b');
      b.textContent = (c.id || '#' + c.key) + ' ';
      line.appendChild(b);
      line.appendChild(document.createTextNode(c.after.slice(0, 90)));
      out.appendChild(line);
    }
    if (!dry) load(true);
  } catch (e) { out.textContent = 'Error: ' + e.message; }
}

$('#rprev').onclick = () => repl(true);
$('#rgo').onclick = () => { if (confirm('Replace across every segment?')) repl(false); };

listFiles().catch(e => banner('Could not fetch the file list: ' + e.message));
