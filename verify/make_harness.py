"""Verify the decoder against the switch's own web UI (read-only).

Captures every section of one switch once (one GET each, throttled), then builds an offline page that runs the switch's
own web UI (its index.html + engine.js) on that data through a fake XMLHttpRequest: the page never talks to the switch.
The page also carries the decoder's expected value for every UI element; compare.js renders each tab and compares.

Usage:
    python verify/make_harness.py <switch ip or name> [--offline]
    python -m http.server 8765 --bind 127.0.0.1 --directory verify/out
    open http://127.0.0.1:8765/<ip>/index.html, wait a few seconds, read the line at the bottom of the page
    (details: window.RESULT in the browser console)

--offline rebuilds the expected values from the last capture without touching the switch.
verify/out/ holds your switch data and is git-ignored.
"""
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import swos  # noqa: E402

TAG = '<script src="data.js"></script><script src="../fakexhr.js"></script><script src="engine.js"></script>'

key = sys.argv[1].lower()
cfg = next((c for c in swos.load_switches() if key in (c['ip'], c['name'].lower())), None)
if cfg is None:
    sys.exit(f'unknown switch {sys.argv[1]!r} (see switches.json)')
ip = cfg['ip']
out = HERE / 'out' / ip
out.mkdir(parents=True, exist_ok=True)
for f in ('fakexhr.js', 'compare.js'):
    (HERE / 'out' / f).write_bytes((HERE / f).read_bytes())

sw = swos.Switch(cfg)
OFFLINE = '--offline' in sys.argv
if OFFLINE:
    js = (out / 'engine.js').read_bytes().decode('latin-1')
    index = (out / 'index.html').read_bytes().decode('latin-1').split('<script src="expected.js">')[0]
    index = index.replace(TAG, '<script src=engine.js></script>').replace('<title>[offline copy] ', '<title>', 1)
    data = json.loads((out / 'data.json').read_text(encoding='utf-8'))
else:
    js = sw._get_text('engine.js')                 # the same engine.js drives the page and the decoder
    index = sw._get_text('index.html')
    data = None
sch = swos.Schema(js)
if data is None:
    paths = []
    for f in sch.forms:
        if f.path not in paths:
            paths.append(f.path)
    data = {p: sw._get_text(p) for p in paths}     # one GET per section
raw = {p.lstrip('!'): swos.parse_js_value(t) for p, t in data.items()}
(out / 'data.json').write_text(json.dumps(data, indent=1), encoding='utf-8')
(out / 'engine.js').write_bytes(js.encode('latin-1'))

names = [swos._hexstr(x) for x in raw['link.b'].get(sch.forms_for('link.b')[0].field('Name').id, [])]


def disp(f, v):
    if v is None:
        return None
    if f.opts is not None and isinstance(f.opts, swos.Ident) and f.kind in ('select', 'ro'):   # port-name lists
        if v < len(names):
            return names[v]
        return f'Trunk{v - 128}' if v > 128 else 'none'
    return swos.show(f, v)


def entry(f, v):
    if f.kind == 'other' or (f.kind == 'check' and f.ro):    # read-only checkbox shows a port name: not compared
        return [None, None, None, f.kind]
    return [disp(f, v), f.unit, None if f.default is None else disp(f, f.default), f.kind]


tabs = []
for form in sch.forms:
    tab = form.tab.lower().replace(' ', '', 1)
    if not tabs or tabs[-1]['tab'] != tab:
        tabs.append({'tab': tab, 'forms': []})
    obj = raw.get(form.url)
    fe = {'label': form.label, 'kind': form.kind}
    if form.kind == 'list':
        rows = []
        for rec in obj or []:
            r = {}
            for f in form.fields:
                for p in range(f.count):
                    v = rec.get(f.id)
                    if f.kind == 'check' and f.count > 1:
                        v = None if v is None else (int(v) >> p & 1)
                    r[f'{f.id}{p}'] = entry(f, v)
            rows.append(r)
        fe['rows'] = rows
    else:
        fl = {}
        for f in form.fields:
            if form.kind == 'ports':
                vals = swos.per_port(f, obj, f.count)
            elif f.kind == 'check' and f.count > 1:
                vals = [None if obj.get(f.id) is None else int(obj[f.id]) >> p & 1 for p in range(f.count)]
            else:
                v = obj.get(f.id)
                vals = [int(v) & 1 if f.bitpack and v is not None else v]
            for p, v in enumerate(vals):
                fl[f'{f.id}{p}'] = entry(f, v)
        fe['fields'] = fl
    tabs[-1]['forms'].append(fe)

# keys in the data that no UI field displays
used = {}
for f in sch.forms:
    for fl in f.fields:
        used.setdefault(f.url, set()).update(x for x in (fl.id, fl.bithi, fl.hiword) if x)
unused = {}
for url, obj in raw.items():
    keys = set().union(*[set(r) for r in obj]) if isinstance(obj, list) else set(obj or {})
    extra = sorted(keys - used.get(url, set()))
    if extra:
        unused[url] = {k: (obj[k] if isinstance(obj, dict) else '(list)') for k in extra}

(out / 'expected.js').write_text('window.EXPECTED=' + json.dumps({'ip': ip, 'tabs': tabs, 'unused': unused}) + ';',
                                 encoding='utf-8')
(out / 'data.js').write_text('window.SWOS_DATA=' + json.dumps(data) + ';', encoding='utf-8')
page = index.replace('<script src=engine.js></script>', TAG)
if page == index:
    sys.exit('index.html: engine.js script tag not found')
page = page.replace('<title>', '<title>[offline copy] ', 1)
page += '<script src="expected.js"></script><script src="../compare.js"></script>'
(out / 'index.html').write_bytes(page.encode('latin-1'))
print(f'{ip}: {len(data)} sections, {len(tabs)} tabs -> {out}')
