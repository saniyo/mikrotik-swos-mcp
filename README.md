# mikrotik-swos-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude Code, Claude Desktop or any MCP client)
**read and change MikroTik SwOS / SwOS Lite switches live** through their web API. You no longer have to download, edit
and upload `.swb` backups by hand.

[Українською](README.uk.md)

- **No hard-coded field tables.** The server downloads each switch's own `engine.js` and learns field ids, names, types,
  option lists and UI forms from it. The minified names change with every SwOS build, and the parser copes with that.
- **Writes work exactly like the web UI.** The server POSTs one UI form to `/<section>.b`, and the change applies live
  without a reboot.
- **Safe by default.** Every write first returns a dry-run diff and a token. Nothing changes until the tool is called again
  with `confirm=<token>`, and the token is refused if the live configuration changed in between. After a write the server
  reads the values back, appends a changelog entry and saves a fresh backup.
- **Backups with real field names.** It decodes `.swb` files and diffs live against file or file against file. A prepared
  `.swb` can be restored live by applying only the forms that differ, without a reboot.
- **Gentle on production switches.** One request at a time, at least 0.2 s between requests, a 3 s cache and no background
  polling.
- **Verifiable.** `verify/` renders the switch's own web UI offline on captured data and compares every value it shows
  with the decoder (see [Verification](#verification)).

## Supported devices

| Device / firmware | Status |
|---|---|
| CSS610-8G-2S+, SwOS Lite 2.21 | **Verified against the web UI** on 3 production switches: 3,251 displayed values, 0 mismatches. Live write tested (identity round trip, read back, no reboot) |
| CSS610-8G-2S+, SwOS Lite 2.18 | Used in production before an upgrade: reads, backups and dry runs; `.swb` decoding byte-checked. Parser OK |
| CSS610-8G-2S+, SwOS Lite 2.20 | Parser OK on the reference `engine.js` from lanrat/python-mikrotik-swos (no device test) |
| CSS610-8P-2S+ (PoE), SwOS Lite 2.20 | Parser OK on the reference `engine.js`, PoE tab included (no device test) |
| Full SwOS (CRS305/309/310/326, CSS326) | **Not supported yet.** Same UI framework, but descriptive keys, variable port count and >32-bit masks. Parser fails on the reference files |

Other SwOS Lite devices probably work because everything is read from the device. Please run `verify/` on yours and open
an issue with the result line.

## Install

```bash
git clone https://github.com/saniyo/mikrotik-swos-mcp
cd mikrotik-swos-mcp
pip install -r requirements.txt          # mcp (1.x or 2.x) + requests
cp switches.example.json switches.json   # then edit it
```

`switches.json`:

```json
{
  "user": "admin",
  "configs_dir": "configs",
  "changelog": "configs/CHANGELOG.md",
  "switches": [
    {"ip": "192.168.88.1", "name": "core", "password": "", "note": "CSS610 in rack A"}
  ]
}
```

- `configs_dir` holds `current/` (the latest backup of each switch), `archive/<date>/` (superseded backups) and
  `manual-changes/` (prepared `.swb` files for `restore_backup`). Relative paths are relative to the config file.
- **Passwords:** use the `password` field, or the environment variables `SWOS_PASSWORD_192_168_88_1` (one switch) or
  `SWOS_PASSWORD` (all switches). Environment variables win. Passwords are never printed or written anywhere. An empty
  admin password only triggers a warning; nothing is blocked.
- `SWOS_CONFIG=/path/to/switches.json` keeps the config outside the repository.

## Register with your MCP client

Claude Code:

```bash
claude mcp add swos --scope user -- /path/to/python /path/to/mikrotik-swos-mcp/server.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{"mcpServers": {"swos": {"command": "/path/to/python", "args": ["/path/to/mikrotik-swos-mcp/server.py"]}}}
```

Use a stable interpreter, not a virtual environment that another tool may rebuild. The server runs over stdio.

## Tools

| Tool | What it does |
|---|---|
| `list_switches` | Configured switches with live identity, version, uptime and a warning for an empty password |
| `read_config` | Config with the web-UI field names. `section` filters by tab (VLAN, VLANs, Link, System, SNMP...). `source` is `live` or a `.swb` path |
| `read_status` | Per port: link, speed, duplex, flow control. Plus uptime, CPU temperature, serial and MAC. `detail` adds RSTP, traffic rates, SFP and cable diagnostics |
| `diff_configs` | Diff of any two of: a live switch, a `.swb` file |
| `set_identity` | System > Identity |
| `set_port` | Port name, enabled, VLAN mode / receive / default VLAN ID / force VLAN ID |
| `set_vlan` | Add, change or remove one VLAN table entry (members, IGMP snooping) |
| `set_field` | Any web-UI field by tab and field name, e.g. `SNMP` / `Location`, `System` / `Allow From Ports` |
| `save_backup` | Saves `current/<identity>-<IP>.swb`. The previous file moves to `archive/<date>/`; no duplicates are created |
| `restore_backup` | Applies a `.swb` from `manual-changes/`: `live` (only the differing forms, no reboot) or `upload` (Restore Backup, which reboots) |

A write session looks like this:

```
> set_identity switch=core identity=CSS610-RACK-A
DRY RUN - 192.168.88.1: identity -> CSS610-RACK-A
[System / General] POST /sys.b (whole UI form, other fields re-sent unchanged, applies live):
  System / General: Identity: MikroTik -> CSS610-RACK-A
To apply, call again with confirm="b39d5ed576".
> set_identity switch=core identity=CSS610-RACK-A confirm=b39d5ed576
APPLIED - 192.168.88.1
[System / General] POST /sys.b: HTTP 200, read-back OK
```

## How SwOS Lite talks (read from `engine.js`, not guessed)

- `GET /<section>.b` (`sys.b`, `link.b`, `fwd.b`, `vlan.b`, `snmp.b`, `rstp.b`, `lacp.b`, `host.b`, `acl.b`; read-only ones
  start with `!`, like `!stats.b`) returns a JavaScript literal with `iNN` keys, e.g. `{i01:0x03ff,i0a:['506f727431',...]}`.
  Strings are hex, IPv4 is a little-endian 32-bit number, per-port checkboxes are bit masks.
- The web UI writes one **form** at a time (one tab block with its own Apply button): `POST /<section>.b`,
  `Content-Type: text/plain`, and the body holds every writable field of that form in the same literal format. Several tabs
  share `fwd.b` / `sys.b`, and each posts only its own fields. The change applies immediately. This server posts the same
  form and re-sends the untouched fields with the values it has just read.
- **Restore Backup** = multipart `POST /backup.swb` (field `file`), then `POST /reboot`.
- A `.swb` backup holds the same data with one-letter keys: index NN becomes `'A'+NN`, continuing with `'a'..` after `'Z'`.
- HTTP digest auth, HTTP/1.0, one connection at a time.
- Field definitions look like `{n:"VLAN Mode",id:"i15",t:G,m:["disabled","optional","strict"]}`. The type function
  names (`G`) and property names (`m`) differ between builds, so the parser identifies them by code signatures (for example,
  the checkbox type is the one whose parser contains `>>1&2147483647`).

## Verification

The decoder is checked against the switch's **own** web UI, not against a hand-written expectation:

```bash
python verify/make_harness.py core          # captures each section once (read-only), builds verify/out/<ip>/
python -m http.server 8765 --bind 127.0.0.1 --directory verify/out
# open http://127.0.0.1:8765/<ip>/index.html and wait a few seconds
```

The page is the switch's real `index.html` + `engine.js`, fed with the captured data through a fake `XMLHttpRequest`, so it
never talks to the switch (POSTs are dropped). It renders every tab and compares each input, select, checkbox and read-only
value with the decoder. The result line reads like `ok 870, blank=default 117, mismatch 0, missing 3, extra 0`:

- `blank=default`: the UI leaves a field empty when it holds its default value (for example `0.0.0.0`).
- `missing 3`: Root Bridge and Allow From are composite fields that the UI shows as one combined input
  (`8000.aa:bb:cc:dd:ee:ff`). They are expected; check them by eye.
- `extra`: a UI element the decoder does not know. It should be 0. Anything else is a bug, so please report it.

Keys that the switch sends but no UI field shows (SwOS Lite 2.21): `sys.b` `i02` current IP, `i06` version,
`i0b` build time, `i0c` upgrade flag, `i0d` watchdog (shown on 2.18, hidden on 2.21); `link.b` `i09` per-port
counter (lanrat names it port uptime; the unit is unverified); `rstp.b` `i04` matches the STP path cost (4 / 19 / 100 for
1G / 100M / 10M links). The rest are zero and not referenced by `engine.js`: `sys.b` `i15 i16 i1c-i1f i20 i21 i23-i26 i2b`,
`link.b` `i0b i0c`, `sfp.b` `i07`, `stats.b` `i03`.

## Comparison with related projects

| | this project | [MikroMCP](https://github.com/AliKarami/MikroMCP) | [python-mikrotik-swos](https://github.com/lanrat/python-mikrotik-swos) |
|---|---|---|---|
| Type | MCP server (Python) | MCP server (TypeScript, RouterOS + SwOS) | Library, CLI, Ansible (Python) |
| Field map | read from each switch's `engine.js` at runtime | static tables | static tables per platform |
| SwOS Lite / full SwOS | Lite only (for now) | both (SwOS marked experimental) | both, more devices |
| Coverage | every web-UI field (generic `set_field`) | sys, link, fwd, VLAN, LACP, SNMP, PoE, upgrade | sys, ports, PoE, LAG, VLAN, SNMP, password |
| Write safety | dry run + token, read-back, changelog, backup | dry run, lockout guard, snapshot/rollback | Ansible check mode |
| Restore | live (only differing forms) or upload + reboot | upload + reboot | upload |
| `.swb` decode / diff | yes | no | no |
| Verified against the UI | yes (`verify/`) | no | no |

Not here yet, worth adopting: full SwOS, a hard lockout guard (MikroMCP), PoE convenience tools, firmware upgrade.

## Safety notes

- Fields that affect management access (IP, Allow From, VLAN changes) produce warnings in the dry run. Unlike some related
  projects, there is **no automatic lockout guard yet**, so review VLAN changes on the uplink port yourself.
- The admin password is never changed. A live restore never touches the password either.
- Static hosts and ACL entries are read and diffed but have no dedicated write tool; use `restore_backup` instead.
- `verify/` and all read tools never write. The verify page drops every POST.

## Related projects

- [AliKarami/MikroMCP](https://github.com/AliKarami/MikroMCP), [lanrat/python-mikrotik-swos](https://github.com/lanrat/python-mikrotik-swos)
  (its `webui_reference/` and field maps were used as an independent cross-check: every SwOS Lite key they map agrees
  with ours), [tofkamp/mikrotik_swos](https://github.com/tofkamp/mikrotik_swos),
  [probert94/python-switchos](https://github.com/probert94/python-switchos).

## License

MIT. Use at your own risk: these switches often carry production traffic, so always read the dry run.
