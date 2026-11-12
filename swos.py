"""MikroTik SwOS / SwOS Lite web-API client (tested: CSS610-8G-2S+, SwOS Lite 2.18 and 2.21).

How the switch works (read from its own engine.js, not guessed):
- GET /<section>.b returns a JS literal with keys iNN, e.g. {i01:0x03ff,i0a:['50..',..]}.
  Strings are hex, IPv4 is a little-endian 32-bit number, per-port checkboxes are bit masks.
- The web UI writes one *form* (one tab block) at a time: POST /<section>.b, Content-Type text/plain,
  body = every writable field of that form in the same literal format (several tabs share fwd.b / sys.b,
  each posts only its own fields). This applies live, no reboot.
- Restore Backup = multipart POST /backup.swb (field "file"), then the UI POSTs "*" to /reboot.
- A .swb backup is the same data with letter keys: index NN -> 'A'+NN, after 'Z' continuing 'a'..
- Field ids, names, types and option lists differ between versions and the minified names change per
  build, so everything is learned from each switch's engine.js at runtime.
"""
import datetime
import gzip
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

HERE = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get('SWOS_CONFIG') or HERE / 'switches.json')

WRITABLE_KINDS = {'check', 'select', 'num', 'snum', 'ip', 'mac', 'str'}


class SwosError(Exception):
    pass


# ---------------------------------------------------------------- JS literal parser
class Ident(str):
    """An unresolved JS identifier (type function, variable)."""


class Unparsed(str):
    """An expression we do not evaluate."""


_TOK = re.compile(r"""\s*(?:(?P<num>0x[0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    |(?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    |(?P<id>[A-Za-z_$][\w$]*)
    |(?P<p>[{}\[\]:,()!.+\-]))""", re.X)


class _Parser:
    def __init__(self, text, pos=0):
        self.t, self.i = text, pos

    def _tok(self, advance=True):
        m = _TOK.match(self.t, self.i)
        if not m:
            raise SwosError(f'JS parse error at {self.i}: {self.t[self.i:self.i + 40]!r}')
        if advance:
            self.i = m.end()
        return m

    def peek(self):
        m = self._tok(False)
        return m.group('p') or ('str' if m.group('str') else 'num' if m.group('num') else 'id')

    def expect(self, p):
        m = self._tok()
        if m.group('p') != p:
            raise SwosError(f'JS parse: expected {p!r} at {self.i}: {self.t[self.i - 10:self.i + 30]!r}')

    def skip_expr(self):
        depth = 0
        while self.i < len(self.t):
            p = self.peek()
            if p in '([{':
                depth += 1
            elif p in ')]}' or p == ',':
                if depth == 0:
                    return
                if p != ',':
                    depth -= 1
            self._tok()

    def value(self):
        m = self._tok()
        p = m.group('p')
        if p == '{':
            obj = {}
            while self.peek() != '}':
                k = self._tok()
                key = k.group('id') or k.group('num') or _js_str(k.group('str'))
                self.expect(':')
                obj[key] = self.value()
                if self.peek() == ',':
                    self._tok()
            self._tok()
            v = obj
        elif p == '[':
            arr = []
            while self.peek() != ']':
                arr.append(self.value())
                if self.peek() == ',':
                    self._tok()
            self._tok()
            v = arr
        elif p == '!':
            v = not self.value()
        elif p == '-':
            v = -self.value()
        elif m.group('num'):
            s = m.group('num')
            v = int(s, 16) if s.lower().startswith('0x') else (float(s) if any(c in s for c in '.eE') else int(s))
            if isinstance(v, float) and v.is_integer():
                v = int(v)
        elif m.group('str'):
            v = _js_str(m.group('str'))
        elif m.group('id'):
            v = Ident(m.group('id'))
            if v == 'null':
                v = None
        else:
            raise SwosError(f'JS parse: unexpected {p!r} at {self.i}')
        if self.i < len(self.t) and self.peek() in ('.', '(', '+'):
            self.skip_expr()
            return Unparsed(str(v))
        return v


def _js_str(s):
    inner = s[1:-1]
    if '\\' not in inner:
        return inner
    return inner.encode('latin-1', 'backslashreplace').decode('unicode_escape', 'replace')


def parse_js_value(text):
    return _Parser(text.strip()).value()


# ---------------------------------------------------------------- serializer (same as the UI's)
def js_literal(v, in_array=False):
    if isinstance(v, dict):
        return '{' + ','.join(f'{k}:{js_literal(x)}' for k, x in v.items()) + '}'
    if isinstance(v, list):
        return '[' + ','.join(js_literal(x, True) for x in v) + ']'
    if isinstance(v, str):
        return "'" + v + "'"
    if isinstance(v, bool) or v is None:
        raise SwosError(f'cannot serialize {v!r}')
    h = format(int(v) & 0xffffffff, 'x')
    return '0x' + ('0' + h if len(h) % 2 else h)


def list_literal(rows):
    return '[' + ','.join(js_literal(r) for r in rows) + ']'


# ---------------------------------------------------------------- schema from engine.js
def _owner(js, snippet):
    """Name of the type function whose code contains `snippet`."""
    pos = js.find(snippet)
    if pos < 0:
        return None
    best, name = -1, None
    for rx in (r'([\w$]+)\.[\w$]+=function\(', r'function ([\w$]+)\('):
        for m in re.finditer(rx, js[:pos]):
            if m.start() > best:
                best, name = m.start(), m.group(1)
    return name


def _hexstr(h):
    out = ''
    for i in range(0, len(h or '') - 1, 2):
        c = int(h[i:i + 2], 16)
        if not c:
            break
        out += chr(c)
    return out


def _strip_html(s):
    return re.sub(r'<[^>]+>', '', s) if isinstance(s, str) else s


class Field:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def writable(self):
        return not self.ro and self.kind in WRITABLE_KINDS

    def __repr__(self):
        return f'<{self.id} {self.name} {self.kind} n={self.count}{" ro" if self.ro else ""}>'


class Form:
    """One UI block: a tab part with its own Apply button (or a read-only view)."""

    def __init__(self, tab, url, title, kind, ro, fields, max_entries=None):
        self.tab, self.title, self.kind, self.ro = tab, title, kind, ro
        self.path, self.url = url, url.lstrip('!')     # path: HTTP path ('!stats.b'), url: section key
        self.fields, self.max_entries = fields, max_entries

    @property
    def label(self):
        return self.tab + (f' / {self.title}' if self.title and self.title != self.tab else '')

    @property
    def writable_fields(self):
        return [f for f in self.fields if f.writable] if not self.ro else []

    def field(self, name):
        n = name.strip().lower()
        for f in self.fields:
            if f.name.lower() == n or f.id == n:
                return f
        return None


class Schema:
    def __init__(self, js):
        self.js = js
        m = re.search(r'\.t\|\|\(\w+\.t=([\w$]+)\);null!=\w+\.([\w$]+)\?0==\w+\.\2&&\(\w+\.\2=10\):\w+\.\2=\w+;'
                      r'\w+&&\(\w+\.([\w$]+)=1\);\w+\.([\w$]+)&&[\w$]+\(\w+\.\4,\w+,\w+\.\3\);\w+\.([\w$]+)&&', js)
        if not m:
            raise SwosError('engine.js: field-defaults function not found (unknown SwOS build)')
        self.T_RO, self.P_COUNT, self.P_RO, k1, k2 = m.groups()
        self.P_KIDS = (k1, k2)
        self.types = {self.T_RO: 'ro'}
        for kind, snip in (('check', '>>1&2147483647|('), ('select', '("select")'), ('num', '"k"=='),
                           ('snum', '2147483648'), ('ip', '>>=8'), ('mac', '12!='),
                           ('str', '.replace(/{(\\w+)}/'), ('uptime', ' days ')):
            name = _owner(js, snip)
            if not name:
                raise SwosError(f'engine.js: type {kind} not found')
            self.types[name] = kind

        def prop(rx, default=None, group=1):
            mm = re.search(rx, js)
            return mm.group(group) if mm else default

        self.P_OPTS = prop(r'\("select"\);\w+\.id=\w+\.id\+\w+;for\((?:var|let|const) \w+ in \w+\.([\w$]+)\)')
        self.P_ENUM = prop(r'\w+\.([\w$]+)&&\(\w+=\w+\.([\w$]+)&&void 0==\w+\.\1\[\w+\]\?')
        self.P_BIT = prop(r'if\(\w+\.([\w$]+)\)\{if\(\w+\[\w+\.id\]=\w+>>1,\w+&=1,\w+\.([\w$]+)\)')
        self.P_BITHI = prop(r'if\(\w+\.([\w$]+)\)\{if\(\w+\[\w+\.id\]=\w+>>1,\w+&=1,\w+\.([\w$]+)\)', group=2)
        self.P_HIWORD = prop(r'\w+\.([\w$]+)&&\(\(\w+=\w+\[\w+\.\1\]\)&&"object"==typeof')
        self.P_MAXLEN = prop(r'\.length>\(\w+\.([\w$]+)\|\|15\)')
        self.P_BASE = prop(r'toString\(\w+\.([\w$]+)\|\|10\)')
        self.P_KMG = prop(r'\w+\.([\w$]+)&&\(1E9<\w+\?')
        self.P_MAXENT = prop(r'\w+\.\w+\.([\w$]+)&&\w+\.\w+\.\1<=\w+\?\w+\(\w+\.status,["`]Max entries')
        self.P_UNIT = prop(r'""!=\w+&&\w+\.([\w$]+)&&\(\w+\+=\w+\.\1\)')
        self.version_ids = re.search(r'[\w$]+\(\w+\.(i[0-9a-f]{2})\)\+"\."\+\w+\.(i[0-9a-f]{2})', js)
        self.version_ids = self.version_ids.groups() if self.version_ids else ('i06', 'i0b')
        # form constructors
        self.ctors = {}
        for mm in re.finditer(r'function ([\w$]+)\(\w+,\w+,\w+,\w+\)\{([^{}]*?)return [\w$]+\.apply\(null,arguments\)\}', js):
            body = mm.group(2)
            self.ctors[mm.group(1)] = 'list' if '.list=1' in body else 'ports' if '.ports&&' in body else 'props'
        tabfn = re.search(r'([\w$]+)\("Link",', js)
        self.forms = []
        tabs = [(t.start(), t.group(1)) for t in re.finditer(re.escape(tabfn.group(1)) + r'\("([^"]+)",', js)] if tabfn else []
        for fm in re.finditer(r'([\w$]+)\("(!?[a-z]+\.b)",\s*"([^"]*)",\s*(?=\{)', js):
            if fm.group(1) not in self.ctors:
                continue
            tab = next((n for p, n in reversed(tabs) if p < fm.start()), fm.group(2))
            try:
                self.forms.append(self._form(fm, tab))
            except SwosError:
                continue
        if not any(f.url == 'link.b' for f in self.forms):
            raise SwosError('engine.js: no forms parsed')

    def _form(self, fm, tab):
        kind = self.ctors[fm.group(1)]
        url, title = fm.group(2), fm.group(3)
        p = _Parser(self.js, fm.end())
        opts = p.value()
        args = []
        while p.peek() == ',':
            p._tok()
            args.append(p.value())
        ro = url.startswith('!') or bool(opts.get(self.P_RO))
        cnt = opts.get(self.P_COUNT)
        if kind == 'list':
            default = 1
        elif kind == 'ports':
            default = len(opts['ports']) if isinstance(opts.get('ports'), list) else (cnt or 10)
        else:
            default = cnt or 1
        fields = []
        self._fields(args, default, ro, fields, fm.start())
        maxent = opts.get(self.P_MAXENT) if kind == 'list' and self.P_MAXENT else None
        return Form(tab, url, title, kind, ro, fields, maxent)

    def _loop_fields(self, var, pos):
        """Port Isolation: a=[];for(b=1;10>=b;++b)a.push({n:"From Port"+b,id:"i0"+b.toString(16),t:F,..})"""
        rx = (re.escape(var) + r'=\[\];for\((?:var|let) (\w+)=1;(\d+)>=\1;\+\+\1\)' + re.escape(var) +
              r'\.push\(\{n:"([^"]+)"\+\1,id:"i0"\+\1\.toString\(16\),t:([\w$]+)')
        found = None
        for m in re.finditer(rx, self.js[max(0, pos - 600):pos]):
            found = m
        if not found:
            return []
        return [{'n': f'{found.group(3)}{b}', 'id': f'i0{b:x}', 't': Ident(found.group(4))}
                for b in range(1, int(found.group(2)) + 1)]

    def _fields(self, items, count_default, ro, out, pos, parent=None):
        for it in items:
            if isinstance(it, Ident):
                self._fields(self._loop_fields(it, pos), count_default, ro, out, pos)
            elif isinstance(it, list):
                self._fields(it, count_default, ro, out, pos)
            elif isinstance(it, dict):
                t = it.get('t')
                if isinstance(t, str) and not isinstance(t, Ident):
                    continue                                # "group", "buttons", "span", "hr"
                kids = next((it[k] for k in self.P_KIDS if isinstance(it.get(k), list)), None)
                f_ro = ro or bool(it.get(self.P_RO))
                if kids is not None and 'id' not in it:
                    leaves = []
                    self._fields(kids, count_default, f_ro, leaves, pos, parent=it.get('n', ''))
                    self._name_kids(it.get('n', ''), leaves)
                    out.extend(leaves)
                    continue
                if 'id' not in it:
                    continue
                cnt = it.get(self.P_COUNT)
                cnt = 10 if cnt == 0 else (cnt or count_default)
                kind = self.types.get(t, 'other') if t is not None else 'ro'
                opts = it.get(self.P_OPTS) if kind == 'select' else it.get(self.P_ENUM)
                if isinstance(opts, Ident):          # e.g. a="10M 100M 1G ...".split(" ") just before the tabs
                    hits = re.findall(r'(?<![\w$.])' + re.escape(opts) + r'="([^"]*)"\.split\(" "\)', self.js[:pos])
                    if hits:
                        opts = hits[-1].split(' ')
                out.append(Field(
                    id=it['id'], name=it.get('n') or parent or it.get(self.P_UNIT) or it['id'], kind=kind, count=cnt, ro=f_ro or kind not in WRITABLE_KINDS,
                    opts=opts, bitpack=bool(it.get(self.P_BIT)), bithi=it.get(self.P_BITHI) if it.get(self.P_BIT) else None,
                    hiword=it.get(self.P_HIWORD), maxlen=it.get(self.P_MAXLEN) or 15, base=it.get(self.P_BASE) or 10,
                    scale=it.get('scale'), kmg=it.get(self.P_KMG) == 1, min=it.get('min'), max=it.get('max'), unique=bool(it.get('unique')),
                    _named=bool(it.get('n'))))

    @staticmethod
    def _name_kids(parent, leaves):
        unnamed = [f for f in leaves if not f._named]
        if not unnamed:
            return
        mixed = any(f.ro for f in unnamed) and any(not f.ro for f in unnamed)
        w = [f for f in unnamed if not f.ro]
        r = [f for f in unnamed if f.ro]
        m = re.match(r'(.*?)(\w+)/(\w+)$', parent)
        if m and len(w) == 2:
            w[0].name, w[1].name = m.group(1) + m.group(2), m.group(1) + m.group(3)
        else:
            for i, f in enumerate(w):
                f.name = parent if i == 0 else f'{parent} #{i + 1}'
        if mixed:
            for f in w:
                f.name += ' setting'
        for i, f in enumerate(r):
            f.name = parent if i == 0 else f'{parent} #{i + 1}'

    # ---- lookup
    def forms_for(self, url):
        return [f for f in self.forms if f.url == url]

    def find(self, tab, field):
        """Find (form, field) by tab/section name and field name or id."""
        t = tab.strip().lower().replace('.b', '')
        cands = [f for f in self.forms if not f.ro and (f.tab.lower() == t or f.url[:-2] == t or f.label.lower() == t)]
        for form in cands:
            fl = form.field(field)
            if fl:
                return form, fl
        raise SwosError(f'field {field!r} not found in tab {tab!r}. Writable tabs: ' +
                        ', '.join(sorted({f.label for f in self.forms if f.writable_fields})))


# ---------------------------------------------------------------- value conversion
def port_ranges(ports):
    ports = sorted(set(ports))
    if not ports:
        return 'none'
    out, start, prev = [], ports[0], ports[0]
    for p in ports[1:] + [None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        out.append(str(start) if start == prev else f'{start}-{prev}')
        if p is not None:
            start = prev = p
    return ','.join(out)


def mask_ports(v, n=10):
    return [p + 1 for p in range(n) if int(v) >> p & 1]


def show(f, v):
    """Display one raw item (not a mask)."""
    if v is None:
        return '-'
    k = f.kind
    if k == 'check':
        return 'yes' if int(v) & 1 else 'no'
    if k == 'str':
        return _hexstr(v) if isinstance(v, str) else str(v)
    if k == 'mac':
        return ':'.join(v[i:i + 2] for i in range(0, 12, 2)) if isinstance(v, str) and len(v) == 12 else str(v)
    if k == 'ip':
        v = int(v)
        return '.'.join(str(v >> s & 255) for s in (0, 8, 16, 24))
    if k == 'uptime':
        s = int(v / (f.scale or 1))
        d, s = divmod(s, 86400)
        return (f'{d}d ' if d else '') + f'{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}'
    if k == 'snum':
        v = int(v)
        v = v - 65536 if v >= 32768 else v
        return str(v / f.scale if f.scale else v)
    if isinstance(v, str):
        return v
    if f.opts is not None and k in ('select', 'ro', 'other'):
        if isinstance(f.opts, list):
            if 0 <= v < len(f.opts):
                return _strip_html(f.opts[v])
            return f'?{v}' if k == 'select' else '-'
        return str(v + 1) if v < 10 else 'none'     # option list = port names (filled at runtime)
    if f.base == 16:
        return format(int(v), 'x')
    x = v / f.scale if f.scale else v
    if getattr(f, 'kmg', False):                     # same k/M/G rounding as the web UI
        for lim, suf in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k')):
            if x > lim:
                return f'{round(x / lim, 2):g}{suf}'
        return str(round(x))
    return str(int(x) if float(x).is_integer() else round(x, 2))


def per_port(f, obj, n):
    """Raw per-port items of a field in a per-port form (list of n)."""
    v = obj.get(f.id)
    if f.bitpack and v is not None:
        hi = obj.get(f.bithi, 0) if f.bithi else 0
        return [(int(v) >> p & 1) | ((int(hi) >> p & 1) << 1 if f.bithi else 0) for p in range(n)]
    if isinstance(v, list):
        if f.hiword and isinstance(obj.get(f.hiword), list):
            return [x + 4294967296 * y for x, y in zip(v, obj[f.hiword])]
        return v + [None] * (n - len(v))
    if f.kind == 'check' and v is not None:
        return [int(v) >> p & 1 for p in range(n)]
    return [v] * n


def show_scalar(f, v):
    if f.kind == 'check' and f.count > 1 and v is not None:
        return port_ranges(mask_ports(v, f.count))
    if f.bitpack and v is not None:
        return show(f, int(v) & 1)
    if isinstance(v, list):
        return ' | '.join(show(f, x) for x in v)
    return show(f, v)


def parse_ports(spec, names=None, n=10):
    """'1-4,9' / 'all' / 'none' / [1,2] / port names -> sorted port numbers."""
    if isinstance(spec, (list, tuple)):
        items = spec
    else:
        s = str(spec).strip().lower()
        if s in ('all', '*'):
            return list(range(1, n + 1))
        if s in ('none', '', '-'):
            return []
        items = re.split(r'[,\s]+', s)
    out = set()
    for it in items:
        it = str(it).strip()
        if not it:
            continue
        m = re.fullmatch(r'(\d+)-(\d+)', it)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        else:
            out.add(port_number(it, names, n))
    bad = [p for p in out if not 1 <= p <= n]
    if bad:
        raise SwosError(f'ports out of range 1..{n}: {bad}')
    return sorted(out)


def port_number(p, names=None, n=10):
    s = str(p).strip()
    if s.isdigit():
        v = int(s)
    else:
        low = s.lower()
        m = re.fullmatch(r'sfp\+?(\d)', low)
        hit = [i + 1 for i, nm in enumerate(names or []) if nm.lower() == low]
        if hit:
            v = hit[0]
        elif m:
            v = 8 + int(m.group(1))
        else:
            raise SwosError(f'unknown port {p!r}' + (f' (names: {", ".join(names)})' if names else ''))
    if not 1 <= v <= n:
        raise SwosError(f'port {v} out of range 1..{n}')
    return v


def encode(f, value):
    """User value -> raw item for one port / one scalar."""
    k = f.kind
    s = str(value).strip() if not isinstance(value, bool) else value
    if k == 'check':
        if isinstance(s, bool):
            return int(s)
        if s.lower() in ('1', 'yes', 'true', 'on', 'enabled', 'enable'):
            return 1
        if s.lower() in ('0', 'no', 'false', 'off', 'disabled', 'disable'):
            return 0
        raise SwosError(f'{f.name}: expected yes/no, got {value!r}')
    if k == 'select':
        if isinstance(f.opts, list):
            low = [_strip_html(o).lower() for o in f.opts]
            if s.lower() in low:
                return low.index(s.lower())
            if s.isdigit() and int(s) < len(low):
                return int(s)
            raise SwosError(f'{f.name}: {value!r} not one of {f.opts}')
        return int(s) - 1 if s.isdigit() else 10                     # port-name option lists
    if k in ('num', 'snum'):
        mult = 1
        if getattr(f, 'kmg', False) and s[-1:].lower() in ('k', 'm', 'g'):   # rates: 100M, 1.5G like the UI
            mult, s = {'k': 1e3, 'm': 1e6, 'g': 1e9}[s[-1].lower()], s[:-1]
        try:
            v = int(s, 16) if f.base == 16 else (float(s) if '.' in s or mult != 1 else int(s))
        except ValueError:
            raise SwosError(f'{f.name}: not a number: {value!r}')
        v = v * mult
        if f.scale and v and int(v * f.scale) == 0:
            raise SwosError(f'{f.name}: {value} is below the switch resolution ({1 / f.scale:g}); use k/M/G, e.g. 10M')
        if k == 'num' and f.scale:                     # the UI checks min/max on the scaled (raw) number
            v = v * f.scale
        if (f.max and v > f.max) or (f.min and v < f.min) or (k == 'num' and v < 0):
            lim = f'{f.min or 0}..{f.max or "max"}' + (f' (x{1 / f.scale:g})' if f.scale else '')
            raise SwosError(f'{f.name}: {value} outside {lim}')
        if k == 'snum' and f.scale:
            v = v * f.scale
        v = int(v)
        return v & 0xffff if k == 'snum' and v < 0 else v
    if k == 'ip':
        parts = s.split('.') if s else []
        if s and (len(parts) != 4 or not all(p.isdigit() and int(p) < 256 for p in parts)):
            raise SwosError(f'{f.name}: bad IPv4 {value!r}')
        return sum(int(p) << (8 * i) for i, p in enumerate(parts)) if s else 0
    if k == 'mac':
        h = re.sub(r'[^0-9a-fA-F]', '', s).lower()
        if len(h) != 12:
            raise SwosError(f'{f.name}: bad MAC {value!r}')
        return h
    if k == 'str':
        if len(s) > f.maxlen:
            raise SwosError(f'{f.name}: max {f.maxlen} characters, got {len(s)}')
        if not all(32 <= ord(c) < 127 for c in s):
            raise SwosError(f'{f.name}: printable ASCII only')
        return ''.join(format(ord(c), '02x') for c in s)
    raise SwosError(f'{f.name}: not writable')


# ---------------------------------------------------------------- backups
def letter_to_id(k):
    i = ord(k) - 65 if k.isupper() else ord(k) - 97 + 26
    return f'i{i:02x}'


def parse_swb(text):
    """'.pwd.b:{B:''},sys.b:{F:'..'},vlan.b:[...]' -> {'sys.b': {'i05': '..'}, ...} (iNN keys)."""
    t = text.strip().lstrip('﻿')
    p = _Parser(t, 1 if t.startswith('.') else 0)
    out = {}
    while p.i < len(t):
        m = re.compile(r'\s*([a-z!]+\.b):').match(t, p.i)
        if not m:
            break
        p.i = m.end()
        v = p.value()
        conv = lambda o: {letter_to_id(k) if len(k) == 1 else k: x for k, x in o.items()}
        out[m.group(1)] = [conv(r) for r in v] if isinstance(v, list) else conv(v)
        if p.i < len(t) and p.peek() == ',':
            p._tok()
    return out


class Paths:
    """Backup folders from the config file: <configs_dir>/current (what runs), archive/<date>/, manual-changes/
    (prepared files for restore_backup), and the changelog file. Relative paths are relative to the config file."""

    def __init__(self):
        cfg = read_config_file()
        base = CONFIG_FILE.resolve().parent
        self.configs = (base / cfg.get('configs_dir', 'configs')).resolve()
        self.current = self.configs / 'current'
        self.archive = self.configs / 'archive'
        self.manual = self.configs / 'manual-changes'
        self.changelog = (base / cfg.get('changelog', 'configs/CHANGELOG.md')).resolve()

    def show(self, p):
        p = Path(p)
        return p.relative_to(self.configs).as_posix() if self.configs in p.parents else p.as_posix()


def resolve_config_path(path, base=None):
    P = Paths()
    base = base or P.configs
    p = Path(path)
    if not p.is_absolute():
        for cand in (base / p, P.configs / p, P.current / p, P.manual / p, Path.cwd() / p):
            if cand.exists():
                p = cand
                break
    p = p.resolve()
    if not p.exists():
        raise SwosError(f'file not found: {path}')
    return p


# ---------------------------------------------------------------- config model / diff
def model(schema, raw, include_ro=False):
    """Flatten raw sections into an ordered {key: display} for diffing and printing.
    Returns (rows, extras) where rows = [(form, key, display)]."""
    rows = []
    for form in schema.forms:
        if form.ro and not include_ro:
            continue
        obj = raw.get(form.url)
        if obj is None:
            continue
        fields = form.fields if include_ro else form.writable_fields
        if not fields:
            continue
        if form.kind == 'list':
            if not isinstance(obj, list):
                continue
            keyf = next((f for f in fields if f.unique), None) or next((f for f in fields if f.kind == 'mac'), None)
            for i, rec in enumerate(obj):
                rk = f'{keyf.name} {show_scalar(keyf, rec.get(keyf.id))}' if keyf else f'#{i + 1}'
                for f in fields:
                    rows.append((form, f'{form.label}: {rk} / {f.name}', show_scalar(f, rec.get(f.id))))
        elif form.kind == 'ports':
            n = max([f.count for f in fields] + [1])
            for f in fields:
                vals = per_port(f, obj, f.count)
                for pi in range(f.count):
                    rows.append((form, f'{form.label}: {f.name} [port {pi + 1}]', show(f, vals[pi])))
        else:
            for f in fields:
                rows.append((form, f'{form.label}: {f.name}', show_scalar(f, obj.get(f.id))))
    return rows


def diff_models(rows_a, rows_b):
    a = {k: v for _, k, v in rows_a}
    b = {k: v for _, k, v in rows_b}
    out = []
    for k in list(a) + [k for k in b if k not in a]:
        va, vb = a.get(k, '(absent)'), b.get(k, '(absent)')
        if va != vb:
            out.append((k, va, vb))
    # list tables: a row missing on one side -> one line per row
    gone = {}
    for k, va, vb in out:
        if ' / ' in k and '(absent)' in (va, vb):
            gone.setdefault(k.rsplit(' / ', 1)[0], []).append((k, va, vb))
    for prefix, items in gone.items():
        rowkey = prefix.split(': ', 1)[-1]
        if len(items) > 1 and any(rowkey.startswith(k.rsplit(' / ', 1)[1] + ' ') for k, _, _ in items):
            side = 0 if items[0][2] == '(absent)' else 1
            desc = ', '.join(f"{k.rsplit(' / ', 1)[1]} {(va, vb)[side]}" for k, va, vb in items
                             if not rowkey.startswith(k.rsplit(' / ', 1)[1] + ' '))
            line = (prefix, f'({desc})', '(absent)') if side == 0 else (prefix, '(absent)', f'({desc})')
            first = out.index(items[0])
            out = [x for x in out if x not in items]
            out.insert(first, line)
    return out


def password_state(raw):
    pw = raw.get('pwd.b')
    if not isinstance(pw, dict):
        return None
    v = next(iter(pw.values()), None)
    return 'empty' if v in ('', None) else 'set'


# ---------------------------------------------------------------- switch access
def read_config_file():
    if not CONFIG_FILE.exists():
        raise SwosError(f'{CONFIG_FILE} missing: copy switches.example.json to switches.json (or set SWOS_CONFIG)')
    return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))


def load_switches():
    cfg = read_config_file()
    user = cfg.get('user', 'admin')
    out = []
    for s in cfg.get('switches', []):
        ip = s['ip']
        env = os.environ.get('SWOS_PASSWORD_' + ip.replace('.', '_'))
        if env is None:
            env = os.environ.get('SWOS_PASSWORD')
        pw = env if env is not None else s.get('password', '')
        out.append(dict(ip=ip, name=s.get('name', ip), note=s.get('note', ''), user=s.get('user', user), _pw=pw or ''))
    return out


class Switch:
    """Light on the switch (production devices, small CPU): one request at a time per switch, at least MIN_GAP s
    between requests, one HTTP session per switch kept across calls (digest nonce reused, no extra 401 round trip),
    GETs cached for CACHE_TTL s (dropped after every write), engine.js read once per server run. No background polling."""
    MIN_GAP = 0.2
    CACHE_TTL = 3.0
    _schemas, _locks, _sessions, _last, _cache = {}, {}, {}, {}, {}

    def __init__(self, cfg, timeout=(4, 12)):
        self.ip, self.name, self.note, self.user = cfg['ip'], cfg['name'], cfg['note'], cfg['user']
        self.password_empty = cfg['_pw'] == ''
        key = hashlib.sha256(f"{cfg['user']}|{cfg['_pw']}".encode()).hexdigest()
        sess = Switch._sessions.get(self.ip)
        if not sess or sess[0] != key:
            http = requests.Session()
            http.auth = HTTPDigestAuth(cfg['user'], cfg['_pw'])
            Switch._sessions[self.ip] = sess = (key, http)
        self.http = sess[1]
        self.timeout = timeout
        self.lock = Switch._locks.setdefault(self.ip, threading.Lock())

    @property
    def base(self):
        return f'http://{self.ip}/'

    def _req(self, method, path, **kw):
        try:
            with self.lock:
                wait = Switch._last.get(self.ip, 0) + self.MIN_GAP - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = self.http.request(method, self.base + path.lstrip('/'), timeout=self.timeout, **kw)
                finally:
                    Switch._last[self.ip] = time.monotonic()
        except requests.RequestException as e:
            raise SwosError(f'{self.ip} unreachable ({type(e).__name__}); check the network path / VPN')
        if r.status_code == 401:
            raise SwosError(f'{self.ip}: authentication failed (user {self.user!r}); check switches.json / SWOS_PASSWORD_*')
        return r

    def get_text(self, path):
        c = Switch._cache.setdefault(self.ip, {})
        hit = c.get(path)
        if hit and time.monotonic() - hit[0] < self.CACHE_TTL:
            return hit[1]
        text = self._get_text(path)
        c[path] = (time.monotonic(), text)
        return text

    def _get_text(self, path):
        r = self._req('GET', path)
        if r.status_code != 200:
            raise SwosError(f'{self.ip}: GET {path} -> HTTP {r.status_code}')
        data = r.content
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        return data.decode('latin-1')

    def get_section(self, url):
        return parse_js_value(self.get_text(url))

    def post_section(self, url, body):
        Switch._cache.pop(self.ip, None)
        r = self._req('POST', url, data=body.encode('ascii'), headers={'Content-Type': 'text/plain'})
        if r.status_code != 200:
            raise SwosError(f'{self.ip}: POST {url} -> HTTP {r.status_code}')

    def schema(self, refresh=False):
        if refresh or self.ip not in Switch._schemas:
            Switch._schemas[self.ip] = Schema(self._get_text('engine.js'))
        return Switch._schemas[self.ip]

    def raw_config(self, sections=None):
        sch = self.schema()
        urls = sections or sorted({f.url for f in sch.forms if f.writable_fields})
        return {u: self.get_section(u) for u in urls}

    def info(self):
        sch = self.schema()
        sysb = self.get_section('sys.b')
        vid, bid = sch.version_ids
        ident_f = next((f for fm in sch.forms_for('sys.b') for f in fm.fields if f.name == 'Identity'), None)
        model_f = next((f for fm in sch.forms_for('sys.b') for f in fm.fields if f.name in ('Board Name', 'Model')), None)
        return dict(ip=self.ip, identity=_hexstr(sysb.get(ident_f.id if ident_f else 'i05')),
                    version=_hexstr(sysb.get(vid, '')), model=_hexstr(sysb.get(model_f.id)) if model_f else '?',
                    sys=sysb)

    def port_names(self):
        link = self.get_section('link.b')
        f = next((f for fm in self.schema().forms_for('link.b') for f in fm.fields if f.name == 'Name'), None)
        return [_hexstr(x) for x in link.get(f.id if f else 'i0a', [])]

    def backup_text(self):
        return self.get_text('backup.swb')

    def upload_backup_and_reboot(self, path):
        Switch._cache.pop(self.ip, None)
        with open(path, 'rb') as fh:
            r = self._req('POST', 'backup.swb', files={'file': (Path(path).name, fh, 'application/octet-stream')})
        if r.status_code != 200:
            raise SwosError(f'{self.ip}: backup upload -> HTTP {r.status_code}; switch NOT rebooted')
        self.post_section('reboot', '*')


# ---------------------------------------------------------------- change plans
class Plan:
    def __init__(self, sw, what):
        self.sw, self.what = sw, what
        self.posts = []        # (form, url, body, [(key, old, new)], verify[(field, port|None, raw|None, rec_key)])
        self.warnings = []
        self.reboot_upload = None

    def add(self, form, body, changes, verify):
        if changes:
            self.posts.append((form, form.url, body, changes, verify))

    @property
    def token(self):
        h = hashlib.sha256((self.sw.ip + self.what + ''.join(u + b for _, u, b, _, _ in self.posts) +
                            str(self.reboot_upload)).encode()).hexdigest()
        return h[:10]

    def text(self):
        lines = [f'DRY RUN - {self.sw.ip}: {self.what}']
        if not self.posts and not self.reboot_upload:
            lines.append('Nothing to change: the switch already has these values.')
            return '\n'.join(lines + self.warnings)
        for form, url, body, changes, _ in self.posts:
            lines.append(f'[{form.label}] POST /{url} (whole UI form, other fields re-sent unchanged, applies live):')
            lines += [f'  {k}: {a} -> {b}' for k, a, b in changes]
        if self.reboot_upload:
            lines.append(f'Restore Backup upload of {self.reboot_upload.name}, then REBOOT (switch offline ~20-40 s).')
        lines += self.warnings
        lines.append(f'To apply, call again with confirm="{self.token}".')
        return '\n'.join(lines)


def form_body(form, obj):
    return {f.id: obj[f.id] for f in form.writable_fields if f.id in obj}


def plan_fields(sw, what, changes):
    """changes: [(form, field, port|None, user_value)] -> Plan (port-form fields need a port)."""
    plan = Plan(sw, what)
    by_form = {}
    for form, f, port, value in changes:
        by_form.setdefault(id(form), (form, []))[1].append((f, port, value))
    names = None
    for form, items in by_form.values():
        obj = sw.get_section(form.url)
        body = form_body(form, obj)
        diffs, verify = [], []
        for f, port, value in items:
            raw = encode(f, value)
            if form.kind == 'ports' or (f.kind == 'check' and f.count > 1 and port is not None):
                if port is None:
                    raise SwosError(f'{f.name} is per port: give a port')
                if names is None:
                    names = sw.port_names()
                p = port_number(port, names, f.count) - 1
                cur = body.get(f.id)
                if f.kind == 'check':
                    new = (int(cur) & ~(1 << p)) | (raw << p)
                    old_d, new_d = show(f, int(cur) >> p & 1), show(f, raw)
                else:
                    new = list(cur)
                    old_d, new_d = show(f, cur[p]), show(f, raw)
                    new[p] = raw
                key = f'{form.label}: {f.name} [port {p + 1}{" " + names[p] if names and p < len(names) else ""}]'
            else:
                cur = body.get(f.id)
                new = raw
                old_d, new_d = show_scalar(f, cur), show_scalar(f, raw)
                key = f'{form.label}: {f.name}'
            if body.get(f.id) != new:
                body[f.id] = new
                diffs.append((key, old_d, new_d))
                verify.append((f.id, new))
            if f.name in ('Static IP Address', 'Address Acquisition', 'Allow From', 'Allow From #2',
                          'Allow From Ports', 'Allow From VLAN'):
                plan.warnings.append(f'WARNING: {f.name} controls management access - a wrong value can lock you out.')
        plan.add(form, js_literal(body), diffs, verify)
    return plan


def plan_vlan(sw, vlan_id, members=None, igmp=None, remove=False):
    sch = sw.schema()
    form = next(f for f in sch.forms if f.url == 'vlan.b' and not f.ro)
    fid = next(f for f in form.fields if f.unique)
    fmem = next(f for f in form.fields if f.kind == 'check' and f.count > 1)
    figmp = next((f for f in form.fields if f.kind == 'check' and f.count == 1), None)
    rows = sw.get_section('vlan.b')
    vid = encode(fid, vlan_id)
    names = sw.port_names()
    cur = next((r for r in rows if r.get(fid.id) == vid), None)
    new_rows = [dict((f.id, r.get(f.id)) for f in form.writable_fields) for r in rows]
    plan = Plan(sw, f'VLAN {vlan_id} ' + ('remove' if remove else 'set'))
    diffs = []
    label = f'{form.label}: VLAN {vlan_id}'
    if remove:
        if cur is None:
            plan.warnings.append(f'VLAN {vlan_id} is not in the table.')
            return plan
        new_rows = [r for r in new_rows if r[fid.id] != vid]
        diffs.append((label, 'present', 'removed'))
    else:
        if cur is None:
            if form.max_entries and len(rows) >= form.max_entries:
                raise SwosError(f'VLAN table full ({form.max_entries})')
            rec = {f.id: 0 for f in form.writable_fields}
            rec[fid.id] = vid
            rec[fmem.id] = 0
            new_rows.append(rec)
            diffs.append((label, '(absent)', 'added'))
        rec = next(r for r in new_rows if r[fid.id] == vid)
        if members is not None:
            m = sum(1 << (p - 1) for p in parse_ports(members, names, fmem.count))
            if rec[fmem.id] != m:
                diffs.append((label + ' / ' + fmem.name, port_ranges(mask_ports(rec[fmem.id] or 0, fmem.count)),
                              port_ranges(mask_ports(m, fmem.count))))
                rec[fmem.id] = m
        if igmp is not None and figmp:
            v = encode(figmp, igmp)
            if rec[figmp.id] != v:
                diffs.append((label + ' / ' + figmp.name, show(figmp, rec[figmp.id] or 0), show(figmp, v)))
                rec[figmp.id] = v
        if cur is None and members is None:
            plan.warnings.append('Note: new VLAN without members given -> no member ports.')
    new_rows.sort(key=lambda r: r[fid.id])          # the UI posts rows sorted by VLAN ID
    plan.add(form, list_literal(new_rows), diffs, [('rows', new_rows, [f.id for f in form.writable_fields])])
    if diffs:
        plan.warnings.append('Check that the uplink / management port stays a member of the management VLAN.')
    return plan


def plan_restore(sw, path):
    """Live restore: for every writable UI form whose values differ from the file, POST the file's values."""
    sch = sw.schema()
    want = parse_swb(Path(path).read_text(encoding='latin-1'))
    live = sw.raw_config()
    plan = Plan(sw, f'restore {Path(path).name} (live, no reboot)')
    order = sorted([f for f in sch.forms if f.writable_fields and f.url in want],
                   key=lambda f: (f.url == 'sys.b', f.url != 'vlan.b'))
    for form in order:
        w, l = want[form.url], live.get(form.url)
        if form.kind == 'list':
            rows = [dict((f.id, r.get(f.id)) for f in form.writable_fields) for r in w]
            cur = [dict((f.id, r.get(f.id)) for f in form.writable_fields) for r in (l or [])]
            if rows != cur:
                changes = diff_models(model(sch, {form.url: l or []}), model(sch, {form.url: w})) \
                    or [(form.label, 'differs', 'file (order)')]
                plan.add(form, list_literal(rows), changes, [('rows', rows, [f.id for f in form.writable_fields])])
            continue
        body = form_body(form, l)
        diffs, verify = [], []
        for f in form.writable_fields:
            if f.id in w and w[f.id] != body.get(f.id):
                if form.kind == 'ports':
                    a, b = per_port(f, l, f.count), per_port(f, w, f.count)
                    for p in range(f.count):
                        if a[p] != b[p]:
                            diffs.append((f'{form.label}: {f.name} [port {p + 1}]', show(f, a[p]), show(f, b[p])))
                else:
                    diffs.append((f'{form.label}: {f.name}', show_scalar(f, body.get(f.id)), show_scalar(f, w[f.id])))
                body[f.id] = w[f.id]
                verify.append((f.id, w[f.id]))
                if form.url == 'sys.b' and f.name in ('Static IP Address', 'Address Acquisition', 'Allow From',
                                                      'Allow From Ports', 'Allow From VLAN'):
                    plan.warnings.append(f'WARNING: {f.name} changes management access.')
        plan.add(form, js_literal(body), diffs, verify)
    # what a live restore cannot set
    known = {(f.url, fl.id) for f in sch.forms for fl in f.writable_fields}
    skipped = []
    for url, obj in want.items():
        if url == 'pwd.b':
            continue
        if isinstance(obj, dict):
            lv = live.get(url) if isinstance(live.get(url), dict) else None
            for k, v in obj.items():
                if (url, k) not in known and lv is not None and k in lv and lv[k] != v:
                    skipped.append(f'{url} {k}')
    if skipped:
        plan.warnings.append('Not settable live (not in the web UI), left as is: ' + ', '.join(skipped))
    if password_state(want) is not None:
        plan.warnings.append('Admin password in the file is ' + password_state(want) + '; a live restore never touches the password.')
    return plan


def plan_restore_upload(sw, path):
    sch = sw.schema()
    want = parse_swb(Path(path).read_text(encoding='latin-1'))
    live = sw.raw_config()
    plan = Plan(sw, f'restore {Path(path).name} (Restore Backup upload + reboot)')
    plan.reboot_upload = Path(path)
    plan.warnings += ['Differences (live -> file):'] + \
        [f'  {k}: {a} -> {b}' for k, a, b in diff_models(model(sch, live), model(sch, want))]
    if password_state(want) == 'set':
        plan.warnings.append('WARNING: the file carries an admin password; after the restore that password applies.')
    return plan


def apply_plan(plan):
    """POST each form, read back, verify. Returns result lines."""
    sw, out = plan.sw, []
    if plan.reboot_upload:
        sw.upload_backup_and_reboot(plan.reboot_upload)
        out.append(f'Uploaded {plan.reboot_upload.name}; reboot requested.')
        return out
    for form, url, body, changes, verify in plan.posts:
        sw.post_section(url, body)
        back = sw.get_section(url)
        bad = []
        for item in verify:
            if item[0] == 'rows':                                # list form: compare whole table
                keys = item[2]
                want = sorted(json.dumps({k: r.get(k) for k in keys}, sort_keys=True) for r in item[1])
                got = sorted(json.dumps({k: r.get(k) for k in keys}, sort_keys=True) for r in back) \
                    if isinstance(back, list) else None
                if want != got:
                    bad.append('table')
            elif not isinstance(back, dict) or back.get(item[0]) != item[1]:
                bad.append(item[0])
        out.append(f'[{form.label}] POST /{url}: HTTP 200, read-back ' + ('OK' if not bad else f'MISMATCH in {bad}'))
        out += [f'  {k}: {a} -> {b}' for k, a, b in changes]
    return out


# ---------------------------------------------------------------- files
def safe_name(s):
    return re.sub(r'[^\w.+-]', '_', s).strip('_') or 'switch'


def save_backup(sw, label=''):
    text = sw.backup_text()
    if not text.strip():
        raise SwosError('switch returned an empty backup (default configuration?)')
    ident = sw.info()['identity']
    name = f'{safe_name(ident)}-{sw.ip}.swb'
    P = Paths()
    P.current.mkdir(parents=True, exist_ok=True)
    dest = P.current / name
    data = text.encode('latin-1')
    moved = []
    today = datetime.date.today()
    for old in sorted(P.current.glob(f'*-{sw.ip}.swb')):
        if old.name == name and old.read_bytes() == data:
            return dict(path=dest, unchanged=True, moved=[])
        adir = P.archive / today.isoformat()
        adir.mkdir(parents=True, exist_ok=True)
        old_bytes = old.read_bytes()
        twin = next((a for a in sorted(adir.glob(f'*{sw.ip}*.swb')) if a.read_bytes() == old_bytes), None)
        if twin:                                       # already archived under another name: no duplicate
            old.unlink()
            moved.append(twin)
            continue
        tag = safe_name(label) if label else 'superseded-' + datetime.datetime.now().strftime('%H%M')
        target = adir / f'{old.stem}-before-{tag}-{today:%Y%m%d}.swb'
        i = 2
        while target.exists():
            target = adir / f'{old.stem}-before-{tag}-{today:%Y%m%d}-{i}.swb'
            i += 1
        shutil.move(str(old), str(target))
        moved.append(target)
    dest.write_bytes(data)
    return dict(path=dest, unchanged=False, moved=moved)


def log_change(lines):
    """Append one entry to the changelog file (Markdown list)."""
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    text = f'- {stamp} ' + '\n  '.join(lines) + '\n'
    path = Paths().changelog
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text('# SwOS change log\n\n', encoding='utf-8')
    with open(path, 'a', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    return text
