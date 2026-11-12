"""MCP server: manage MikroTik SwOS / SwOS Lite switches live over their web API.

Run:  python server.py   (stdio).  Switches + credentials: switches.json / SWOS_PASSWORD_* env vars.
Every write is a dry run first; it applies only when called again with confirm=<token from the dry run>.
"""
import sys

sys.dont_write_bytecode = True       # keep tools/swos-mcp free of __pycache__

import functools
import re
import time
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP                       # mcp 1.x
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP        # mcp 2.x (same tool()/run() API)

import swos
from swos import SwosError

mcp = FastMCP('swos', instructions=(
    'Manages MikroTik SwOS / SwOS Lite switches live over their web API. Read tools are safe. Every write tool '
    'returns a DRY RUN with a token first; call it again with confirm=<token> ONLY after the owner approved that '
    'exact change. Applied changes are read back, logged in the changelog file and a fresh backup is saved to '
    '<configs_dir>/current (the old one to <configs_dir>/archive/<date>/).'))

STATUS_SKIP = {'Hops', 'Last Hop', 'Length', 'Fault At', 'Cable Pairs'}


# ---------------------------------------------------------------- helpers
def _switch(name):
    key = str(name).strip().lower()
    for cfg in swos.load_switches():
        if key in (cfg['ip'], cfg['name'].lower()):
            return swos.Switch(cfg)
    raise SwosError(f'unknown switch {name!r}; known: ' + ', '.join(f"{c['name']} ({c['ip']})" for c in swos.load_switches()))


def _pw_warning(sw):
    return f'WARNING: admin password of {sw.ip} is empty (stock) - warning only, nothing is blocked.' if sw.password_empty else ''


def _err(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except SwosError as e:
            return f'ERROR: {e}'
    return wrap


def _header(sw, info, source='live'):
    lines = [f"{info['identity']}  {sw.ip}  SwOS {info['version']}  {info['model']}  [{source}]"]
    w = _pw_warning(sw)
    return lines + ([w] if w else [])


def _select_forms(schema, section, writable=True):
    forms = [f for f in schema.forms if (f.writable_fields if writable else True)]
    if not section:
        return forms
    keys = [s.strip().lower().replace('.b', '') for s in section.split(',') if s.strip()]
    sel = [f for f in forms if any(k in (f.tab.lower(), f.url[:-2], f.label.lower()) for k in keys)]
    if not sel:
        raise SwosError(f'no section {section!r}; sections: ' + ', '.join(sorted({f.tab for f in forms})))
    return sel


def _table(header, rows):
    return ['  ' + ' | '.join(header)] + ['  ' + ' | '.join(r) for r in rows]


def _format_forms(forms, raw, names, fields_of=lambda fm: fm.writable_fields):
    out = []
    for form in forms:
        obj = raw.get(form.url)
        fields = fields_of(form)
        if obj is None or not fields:
            continue
        out.append(f'== {form.label}  [{form.url}{", per port" if form.kind == "ports" else ""}]')
        if form.kind == 'ports':
            n = max(f.count for f in fields)
            cols = {f.id: swos.per_port(f, obj, f.count) for f in fields}
            rows = []
            has_name = any(f.name == 'Name' for f in fields)
            off = 8 if n == 2 else 0                 # 2-entry per-port forms are the SFP+ cages (ports 9, 10)
            for p in range(n):
                nm = names[p + off] if p + off < len(names) and not has_name else ''
                rows.append([f'{p + off + 1} {nm}'.strip()] + [swos.show(f, cols[f.id][p]) if p < f.count else '' for f in fields])
            out += _table(['port'] + [f.name for f in fields], rows)
        elif form.kind == 'list':
            recs = obj if isinstance(obj, list) else []
            if not recs:
                out.append('  (empty)')
            else:
                out += _table([f.name for f in fields], [[swos.show_scalar(f, r.get(f.id)) for f in fields] for r in recs])
        else:
            out += [f'  {f.name}: {swos.show_scalar(f, obj.get(f.id))}' for f in fields]
    return out


def _source_raw(src, schema_sw=None):
    """'live:<switch>' / '<switch>' or a .swb path -> (raw sections, switch used for the schema, label)."""
    s = str(src).strip()
    if s.lower().endswith('.swb'):
        path = swos.resolve_config_path(s)
        raw = swos.parse_swb(path.read_text(encoding='latin-1'))
        sw = schema_sw
        if sw is None:
            ip = next((c['ip'] for c in swos.load_switches() if f"-{c['ip']}" in path.name), None)
            sw = _switch(ip) if ip else _switch(swos.load_switches()[0]['ip'])
        return raw, sw, swos.Paths().show(path)
    sw = _switch(s.split(':', 1)[1] if s.lower().startswith('live:') else s)
    return sw.raw_config(), sw, f'live {sw.ip}'


def _apply(plan, reason, confirm):
    """Shared dry-run / confirm logic for all writes."""
    if not confirm:
        return plan.text()
    if confirm != plan.token:
        return ('NOT APPLIED: confirm token does not match this request or the live config changed since the dry run.\n'
                + plan.text())
    if not plan.posts and not plan.reboot_upload:
        return plan.text()
    sw = plan.sw
    info = sw.info()
    result = swos.apply_plan(plan)
    if plan.reboot_upload:
        result += _wait_back(sw)
    ok = all('MISMATCH' not in r for r in result)
    entry = [f"{info['model']} {sw.ip} ({info['identity']}, SwOS {info['version']}) via swos-mcp: {plan.what}"
             + (f' - {reason}' if reason else '') + ':']
    for _, _, _, changes, _ in plan.posts:
        entry += [f'{k}: {a} -> {b}' for k, a, b in changes]
    entry.append('Read back: ' + ('OK.' if ok else 'MISMATCH, see tool output.'))
    try:
        bk = swos.save_backup(sw, label=_slug(plan.what))
        entry.append(f"Backup: {swos.Paths().show(bk['path'])}" +
                     (f" (old -> archive/{bk['moved'][0].parent.name}/{bk['moved'][0].name})" if bk['moved'] else ''))
    except SwosError as e:
        entry.append(f'Backup after change FAILED: {e}')
    swos.log_change(entry)
    return '\n'.join(['APPLIED - ' + sw.ip] + result + [f'Logged in {swos.Paths().changelog.name}: ' + entry[-1]])


def _slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:40]


def _wait_back(sw, limit=120):
    time.sleep(8)
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            sw.get_section('sys.b')
            return [f'Switch answers again after ~{int(time.time() - t0) + 8} s.']
        except SwosError:
            time.sleep(3)
    return [f'MISMATCH: switch did not answer within {limit} s after the reboot.']


# ---------------------------------------------------------------- read tools
@mcp.tool()
@_err
def list_switches(probe: bool = True) -> str:
    """List the configured SwOS switches; with probe=True also read identity, version and uptime live."""
    out = []
    for cfg in swos.load_switches():
        sw = swos.Switch(cfg, timeout=(3, 6))
        line = f"{cfg['name']}: {cfg['ip']} - {cfg['note']}"
        if probe:
            try:
                info = sw.info()
                up = next((f for fm in sw.schema().forms_for('sys.b') for f in fm.fields if f.kind == 'uptime'), None)
                line += (f" | live: identity {info['identity']}, SwOS {info['version']}, {info['model']}"
                         + (f", uptime {swos.show(up, info['sys'].get(up.id))}" if up else ''))
            except SwosError as e:
                line += f' | {e}'
        if sw.password_empty:
            line += ' | WARNING: admin password empty (warning only)'
        out.append(line)
    return '\n'.join(out)


@mcp.tool()
@_err
def read_config(switch: str, section: str = '', source: str = 'live') -> str:
    """Configuration with real field names (only fields the web UI can set).
    switch: IP or name from list_switches. section: optional tab filter, e.g. 'VLAN', 'VLANs', 'Link', 'System', 'SNMP'
    (comma list). source: 'live' (default) or a .swb path (relative to configs_dir, e.g. 'current/X.swb' or 'manual-changes/Y.swb'),
    decoded with this switch's field list."""
    sw = _switch(switch)
    if source and source != 'live':
        raw, _, label = _source_raw(source, schema_sw=sw)
    else:
        raw, label = sw.raw_config(), 'live'
    info = sw.info()
    sch = sw.schema()
    names = [swos._hexstr(x) for x in raw.get('link.b', {}).get(sch.forms_for('link.b')[0].field('Name').id, [])]
    out = _header(sw, info, label)
    if label != 'live':
        idf = sch.find('System', 'Identity')[1]
        pw = swos.password_state(raw)
        out.append(f"(file identity: {swos.show_scalar(idf, raw.get('sys.b', {}).get(idf.id))}"
                   + (f', admin password in file: {pw}' if pw else '') + ')')
    out += _format_forms(_select_forms(sch, section), raw, names)
    return '\n'.join(out)


@mcp.tool()
@_err
def read_status(switch: str, detail: bool = False) -> str:
    """Live status: per port link, speed, duplex, flow control, enabled; system uptime, CPU temperature, serial, MAC.
    detail=True adds RSTP role/state, traffic rates, SFP info and cable diagnostics."""
    sw = _switch(switch)
    sch = sw.schema()
    info = sw.info()
    link = sw.get_section('link.b')
    names = [swos._hexstr(x) for x in link.get(sch.forms_for('link.b')[0].field('Name').id, [])]
    sysb = info['sys']
    sysro = [f for fm in sch.forms_for('sys.b') for f in fm.fields if f.ro and f.kind != 'other' and f.name not in ('Root Bridge', 'Root Bridge #2')]
    out = _header(sw, info) + ['  ' + ', '.join(f'{f.name}: {swos.show_scalar(f, sysb.get(f.id))}' for f in sysro)]
    lf = sch.forms_for('link.b')[0]
    pick = lambda fm: [f for f in fm.fields if f.name != 'Name' and f.kind != 'other' and (detail or f.name not in STATUS_SKIP)
                       and (f.ro or f.name in ('Enabled', 'Auto Negotiation'))]
    out += _format_forms([lf], {'link.b': link}, names, pick)
    if detail:
        forms = [f for f in sch.forms if f.url == 'rstp.b'] + [f for f in sch.forms if f.tab == 'Stats'] + \
                [f for f in sch.forms if f.url == 'sfp.b']
        raw = {f.url: sw.get_section(f.path) for f in forms}
        rate = lambda fm: [f for f in fm.fields if f.ro and ('Rate' in f.name or fm.url != 'stats.b')]
        out += _format_forms(forms, raw, names, rate)
    return '\n'.join(out)


@mcp.tool()
@_err
def diff_configs(a: str, b: str, schema_switch: str = '') -> str:
    """Differences between two configs. a / b: a switch (IP or name, read live) or a .swb path (relative to configs_dir).
    Field names come from the switch's engine.js (schema_switch, else the switch the file name belongs to)."""
    ssw = _switch(schema_switch) if schema_switch else None
    raw_a, sw_a, la = _source_raw(a, ssw)
    raw_b, sw_b, lb = _source_raw(b, ssw or sw_a)
    sch = (ssw or sw_a).schema()
    d = swos.diff_models(swos.model(sch, raw_a), swos.model(sch, raw_b))
    out = [f'diff  A = {la}  B = {lb}  (fields settable in the web UI)']
    out += [f'  {k}: {x} -> {y}' for k, x, y in d] or ['  no differences']
    pa, pb = swos.password_state(raw_a), swos.password_state(raw_b)
    if pa and pb and pa != pb:
        out.append(f'  admin password: {pa} -> {pb}')
    return '\n'.join(out)


# ---------------------------------------------------------------- write tools
@mcp.tool()
@_err
def set_identity(switch: str, identity: str, reason: str = '', confirm: str = '') -> str:
    """Set the switch identity (System > Identity, max 15 ASCII chars). Dry run unless confirm=<token>."""
    sw = _switch(switch)
    form, f = sw.schema().find('System', 'Identity')
    return _apply(swos.plan_fields(sw, f'identity -> {identity}', [(form, f, None, identity)]), reason, confirm)


@mcp.tool()
@_err
def set_port(switch: str, port: str, name: str | None = None, enabled: bool | None = None,
             vlan_mode: str | None = None, vlan_receive: str | None = None, default_vlan_id: int | None = None,
             force_vlan_id: bool | None = None, reason: str = '', confirm: str = '') -> str:
    """Change one port: name (max 15 chars), enabled, VLAN mode (disabled/optional/strict), VLAN receive
    (any/only tagged/only untagged), default VLAN ID (1-4095), force VLAN ID. port: 1-10, 'sfp1'/'sfp2' or the port name.
    Only given values change. Dry run unless confirm=<token>."""
    sw = _switch(switch)
    sch = sw.schema()
    ch = []
    for tab, fname, val in (('Link', 'Name', name), ('Link', 'Enabled', enabled), ('VLAN', 'VLAN Mode', vlan_mode),
                            ('VLAN', 'VLAN Receive', vlan_receive), ('VLAN', 'Default VLAN ID', default_vlan_id),
                            ('VLAN', 'Force VLAN ID', force_vlan_id)):
        if val is not None:
            form, f = sch.find(tab, fname)
            ch.append((form, f, port, val))
    if not ch:
        return 'Nothing given to change.'
    what = f'port {port}: ' + ', '.join(f'{f.name}={v}' for _, f, _, v in ch)
    plan = swos.plan_fields(sw, what, ch)
    if vlan_mode is not None or vlan_receive is not None or default_vlan_id is not None:
        plan.warnings.append('Check the management path: VLAN changes on the uplink port can cut access to the switch.')
    return _apply(plan, reason, confirm)


@mcp.tool()
@_err
def set_vlan(switch: str, vlan_id: int, members: str | None = None, igmp_snooping: bool | None = None,
             remove: bool = False, reason: str = '', confirm: str = '') -> str:
    """Add / change / remove one entry of the VLAN table (VLANs tab). members: ports like '1-4,9', 'all', 'none' or
    port names (replaces the member list). Dry run unless confirm=<token>."""
    sw = _switch(switch)
    return _apply(swos.plan_vlan(sw, vlan_id, members, igmp_snooping, remove), reason, confirm)


@mcp.tool()
@_err
def set_field(switch: str, tab: str, field: str, value: str, port: str = '', reason: str = '', confirm: str = '') -> str:
    """Generic: set any web-UI field, e.g. tab='SNMP' field='Location'; tab='System' field='Allow From Ports' value='1-10';
    per-port tabs (Link, VLAN, Forwarding, LAG, RSTP) need port. Field names as shown by read_config.
    Dry run unless confirm=<token>."""
    sw = _switch(switch)
    form, f = sw.schema().find(tab, field)
    if f.kind == 'check' and f.count > 1 and form.kind != 'ports' and not port:
        value = sum(1 << (p - 1) for p in swos.parse_ports(value, sw.port_names(), f.count))
        plan = swos.Plan(sw, f'{form.label} {f.name} -> {swos.port_ranges(swos.mask_ports(value, f.count))}')
        obj = sw.get_section(form.url)
        body = swos.form_body(form, obj)
        if body.get(f.id) != value:
            old = swos.show_scalar(f, body.get(f.id))
            body[f.id] = value
            plan.add(form, swos.js_literal(body), [(f'{form.label}: {f.name}', old, swos.show_scalar(f, value))], [(f.id, value)])
        return _apply(plan, reason, confirm)
    what = f'{form.label} {f.name}' + (f' port {port}' if port else '') + f' -> {value}'
    return _apply(swos.plan_fields(sw, what, [(form, f, port or None, value)]), reason, confirm)


@mcp.tool()
@_err
def save_backup(switch: str, label: str = '') -> str:
    """Download the switch backup into <configs_dir>/current/<identity>-<IP>.swb. A different older file for this IP moves to
    <configs_dir>/archive/<YYYY-MM-DD>/<old name>-before-<label>-<YYYYMMDD>.swb; an identical one is left alone."""
    sw = _switch(switch)
    r = swos.save_backup(sw, label)
    if r['unchanged']:
        return f"Unchanged: {swos.Paths().show(r['path'])} already equals the switch backup."
    out = [f"Saved {swos.Paths().show(r['path'])}"]
    out += [f'Old file is now {swos.Paths().show(m)}' for m in r['moved']]
    return '\n'.join(out)


@mcp.tool()
@_err
def restore_backup(switch: str, file: str, method: str = 'live', reason: str = '', confirm: str = '') -> str:
    """Apply a prepared .swb from <configs_dir>/manual-changes/ to the switch.
    method='live' (default): only the differing web-UI forms are POSTed like Apply in the UI - no reboot.
    method='upload': the web UI's Restore Backup (upload + reboot, switch offline ~30 s).
    Dry run (shows the diff) unless confirm=<token>."""
    sw = _switch(switch)
    manual = swos.Paths().manual
    path = swos.resolve_config_path(file, base=manual)
    if manual not in path.parents:
        raise SwosError(f'restore only takes files from {manual}')
    other = [c['ip'] for c in swos.load_switches() if f"-{c['ip']}-" in path.name or path.stem.endswith(f"-{c['ip']}")]
    if other and sw.ip not in other:
        raise SwosError(f'{path.name} belongs to {other[0]}, not {sw.ip}')
    if method == 'live':
        plan = swos.plan_restore(sw, path)
    elif method == 'upload':
        plan = swos.plan_restore_upload(sw, path)
    else:
        raise SwosError("method must be 'live' or 'upload'")
    if not other:
        plan.warnings.append(f'Note: the file name carries no IP of a known switch; check it is meant for {sw.ip}.')
    return _apply(plan, reason, confirm)


if __name__ == '__main__':
    mcp.run()
