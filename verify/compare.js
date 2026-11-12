// Render every tab of the real SwOS UI (offline data) and compare each element with the decoder's expected value.
(async function () {
  const tick = () => new Promise(r => { const ch = new MessageChannel(); ch.port1.onmessage = () => r(); ch.port2.postMessage(0); });
  const sleep = async n => { for (let i = 0; i < n; i++) await tick(); };
  const norm = s => (s == null ? '' : String(s)).replace(/[\s  ]+/g, '').replace(/days?/, 'd');
  const val = el => {
    if (el.tagName === 'INPUT') return (el.type === 'checkbox' || el.type === 'radio') ? (el.checked ? 'yes' : 'no') : el.value;
    if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return o ? o.text : ''; }
    return el.innerText !== undefined ? el.innerText : el.textContent;
  };
  const grab = root => { const m = {}; root.querySelectorAll('[id]').forEach(el => { if (/^i[0-9a-f]{2}\d+$/.test(el.id)) m[el.id] = val(el); }); return m; };
  const ZERO = ['', '0', '-', '0.0.0.0', '00:00:00:00:00:00', '000000000000'];
  function cmp(ui, e) {
    if (e[0] === null) return 'unverified';
    const u = norm(ui), x = norm(e[0]), unit = norm(e[1] || '');
    if (u === x || u === x + unit) return 'ok';
    if (e[3] === 'mac' && u.replace(/:/g, '') === x.replace(/:/g, '')) return 'ok';
    if (u === '' && (ZERO.includes(x) || (e[2] != null && x === norm(e[2])))) return 'blank';
    return 'mismatch';
  }
  const R = { ip: EXPECTED.ip, ok: 0, blank: 0, mismatch: [], unverified: [], missing: [], extra: [], rows: [], unused: EXPECTED.unused, tabs: [] };
  for (const t of EXPECTED.tabs) {
    location.hash = '#' + t.tab;
    let tables = [];
    for (let i = 0; i < 3000; i++) {
      await tick();
      tables = [...document.querySelector('main').children].filter(x => x.tagName === 'TABLE');
      const st = document.getElementById('status');
      if (tables.length >= t.forms.length && tables[t.forms.length - 1].rows.length > 0 && !/Retrieving/.test(st ? st.textContent : '')) break;
    }
    await sleep(20);
    let n = 0;
    t.forms.forEach((f, fi) => {
      const tb = tables[fi];
      if (!tb) { R.missing.push(`${t.tab}: form ${f.label} not rendered`); return; }
      if (f.kind === 'list') {
        const act = [...tb.querySelectorAll('tbody.interlive')].map(grab);
        const used = new Set();
        f.rows.forEach((er, ri) => {
          let hit = -1;
          act.forEach((ar, ai) => { if (hit < 0 && !used.has(ai) && Object.keys(er).every(k => k in ar && cmp(ar[k], er[k]) !== 'mismatch')) hit = ai; });
          if (hit < 0) { R.rows.push(`${f.label}: expected row ${ri + 1} not found: ` + Object.keys(er).map(k => `${k}=${er[k][0]}`).join(' ')); return; }
          used.add(hit);
          Object.keys(er).forEach(k => { const c = cmp(act[hit][k], er[k]); n++; if (c === 'ok') R.ok++; else if (c === 'blank') R.blank++; else if (c === 'unverified') R.unverified.push(`${f.label} ${k}="${norm(act[hit][k])}"`); });
        });
        if (act.length !== f.rows.length) R.rows.push(`${f.label}: UI rows ${act.length}, decoder rows ${f.rows.length}`);
        act.forEach((ar, ai) => { if (!used.has(ai)) R.rows.push(`${f.label}: UI row ${ai + 1} unmatched: ` + JSON.stringify(ar)); });
      } else {
        const act = grab(tb);
        Object.keys(f.fields).forEach(k => {
          const e = f.fields[k];
          if (!(k in act)) { R.missing.push(`${f.label} ${k} (expected ${e[0]})`); return; }
          const c = cmp(act[k], e); n++;
          if (c === 'ok') R.ok++; else if (c === 'blank') R.blank++;
          else if (c === 'unverified') R.unverified.push(`${f.label} ${k}="${norm(act[k])}"`);
          else R.mismatch.push(`${f.label} ${k}: UI="${act[k]}" decoder="${e[0]}"`);
        });
        Object.keys(act).forEach(k => { if (!(k in f.fields)) R.extra.push(`${f.label} ${k}="${act[k]}"`); });
      }
    });
    R.tabs.push(`${t.tab}:${n}`);
  }
  window.RESULT = R;
  const pre = document.createElement('pre');
  pre.id = 'swos-result';
  pre.textContent = 'OFFLINE COMPARISON ' + R.ip + ': ok ' + R.ok + ', blank=default ' + R.blank + ', mismatch ' + R.mismatch.length +
    ', missing ' + R.missing.length + ', extra ' + R.extra.length + ', row issues ' + R.rows.length;
  document.body.appendChild(pre);
})();
