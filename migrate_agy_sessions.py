#!/usr/bin/env python3
"""
Antigravity Session Migrator: CLI -> Desktop
=============================================
A universal, zero-dependency interactive tool to migrate conversation sessions from
Antigravity CLI (`agy`) into Antigravity Desktop.

Features:
- Pure Python 3 standard library (zero external dependencies).
- Cross-platform interactive TUI (Windows msvcrt / Unix termios).
- Startup check: Antigravity Desktop must be running. If not, prompts user to start it
  with [Enter] to retry or [Esc/q] to quit.
- Step 1: pick the target project.
- Step 2: pick sessions (Space to toggle, 'a' all, 'f' project filter, Enter/c to migrate).
- Uniform navigation: [Esc] or the "< Back" button returns to the previous step; on Step 1 it exits.
- Automatic filtering of corrupt/ghost uninitialized sessions.
- Automatic bundling of child subagent trajectories with their parent sessions.
- Live RPC injection into Antigravity Desktop language server and direct SQLite/Protobuf sync.
- 100% non-destructive: CLI session files are never modified or deleted.
"""

import os
import sys
import uuid
import time
import json
import socket
import shutil
import sqlite3
import argparse
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone


def get_desktop_cid(cli_cid):
    """
    Deterministically map a CLI conversation UUID to a dedicated Desktop conversation UUID.
    Why this is critical:
    Antigravity Desktop's language_server.exe runs two fsnotify watchers concurrently:
      1. ~/.gemini/antigravity/conversations     (source = 0, Desktop)
      2. ~/.gemini/antigravity-cli/conversations (source = 17, CORTEX_TRAJECTORY_SOURCE_CLI)
    Both watchers share the same in-memory Store.summaries[cid] map. If a migrated session in
    ~/.gemini/antigravity/conversations/ keeps the same UUID as ~/.gemini/antigravity-cli/conversations/,
    then on every app startup the CLI watcher overwrites Store.summaries[cid].source = 17, causing
    the Desktop UI to filter out and hide the migrated session! And if the file is removed from
    antigravity-cli, the CLI watcher records Store.deletedIds[cid] = true in RAM.
    Using a deterministic 36-char UUID5 for the Desktop copy keeps ~/.gemini/antigravity-cli 100%
    untouched while ensuring the Desktop copy is exclusively owned by watcher #1 (source = 0).
    """
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"antigravity-desktop:{cli_cid}"))


# Migration direction (CLI -> Desktop)
TO_DESKTOP = 'desktop'
CLI_SOURCE_ENUM = 17          # CORTEX_TRAJECTORY_SOURCE_CLI
CLI_SOURCE_NAME = 'CORTEX_TRAJECTORY_SOURCE_CLI'


def map_cid(cid, direction=TO_DESKTOP):
    return get_desktop_cid(cid)


# ---------------------------------------------------------------------------
# Cross-Platform Terminal & Keypress Handling
# ---------------------------------------------------------------------------


def init_terminal():
    """Enable ANSI virtual terminal processing on Windows and UTF-8 encoding."""
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_out = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
        except Exception:
            pass


def hide_cursor():
    sys.stdout.write('\033[?25l')
    sys.stdout.flush()


def show_cursor():
    sys.stdout.write('\033[?25h')
    sys.stdout.flush()


def clear_screen():
    sys.stdout.write('\033[2J\033[H')
    sys.stdout.flush()


def get_terminal_size():
    try:
        cols, rows = os.get_terminal_size()
        return cols, rows
    except Exception:
        return 90, 24


def get_key():
    """Wait for a single keypress cross-platform and return a normalized string."""
    if os.name == 'nt':
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            if ch2 == b'H':
                return 'UP'
            if ch2 == b'P':
                return 'DOWN'
            if ch2 == b'K':
                return 'LEFT'
            if ch2 == b'M':
                return 'RIGHT'
            if ch2 == b'G':
                return 'HOME'
            if ch2 == b'O':
                return 'END'
            if ch2 == b'I':
                return 'PAGE_UP'
            if ch2 == b'Q':
                return 'PAGE_DOWN'
            return 'SPECIAL'
        if ch in (b'\r', b'\n'):
            return 'ENTER'
        if ch == b' ':
            return 'SPACE'
        if ch == b'\x1b':
            return 'ESC'
        if ch == b'\t':
            return 'TAB'
        if ch in (b'\x03', b'\x1a'):
            return 'CTRL_C'
        try:
            return ch.decode('latin1').lower()
        except Exception:
            return ''
    else:
        import tty
        import termios
        import select
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'A':
                            return 'UP'
                        if ch3 == 'B':
                            return 'DOWN'
                        if ch3 == 'C':
                            return 'RIGHT'
                        if ch3 == 'D':
                            return 'LEFT'
                        if ch3 == 'H':
                            return 'HOME'
                        if ch3 == 'F':
                            return 'END'
                        if ch3 in ('5', '6'):
                            sys.stdin.read(1)  # consume ~
                            return 'PAGE_UP' if ch3 == '5' else 'PAGE_DOWN'
                return 'ESC'
            if ch in ('\r', '\n'):
                return 'ENTER'
            if ch == ' ':
                return 'SPACE'
            if ch == '\t':
                return 'TAB'
            if ch == '\x03':
                return 'CTRL_C'
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

# ---------------------------------------------------------------------------
# Protobuf Varint & Wire Helpers (Zero External Dependencies)
# ---------------------------------------------------------------------------


def encode_varint(n):
    res = bytearray()
    while n >= 128:
        res.append((n & 0x7f) | 0x80)
        n >>= 7
    res.append(n)
    return bytes(res)


def parse_fields(data, start=0, end=None):
    if end is None:
        end = len(data)
    fields = []
    idx = start
    while idx < end:
        tag_val = 0
        shift = 0
        while True:
            b = data[idx]
            tag_val |= (b & 0x7f) << shift
            idx += 1
            if not (b & 0x80):
                break
            shift += 7
        field_num = tag_val >> 3
        wire_type = tag_val & 7
        if wire_type == 0:
            v = 0
            s = 0
            while True:
                b = data[idx]
                v |= (b & 0x7f) << s
                idx += 1
                if not (b & 0x80):
                    break
                s += 7
            fields.append((field_num, wire_type, v))
        elif wire_type == 2:
            v_len = 0
            s = 0
            while True:
                b = data[idx]
                v_len |= (b & 0x7f) << s
                idx += 1
                if not (b & 0x80):
                    break
                s += 7
            val = data[idx:idx + v_len]
            idx += v_len
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:
            val = data[idx:idx + 8]
            idx += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 5:
            val = data[idx:idx + 4]
            idx += 4
            fields.append((field_num, wire_type, val))
        else:
            raise ValueError(
                f"Unsupported wire type {wire_type} at offset {idx}")
    return fields


def encode_field(field_num, wire_type, val):
    tag = (field_num << 3) | wire_type
    res = bytearray(encode_varint(tag))
    if wire_type == 0:
        res.extend(encode_varint(val))
    elif wire_type == 2:
        res.extend(encode_varint(len(val)))
        res.extend(val)
    elif wire_type in (1, 5):
        res.extend(val)
    return bytes(res)


def serialize_fields(fields):
    return b''.join(encode_field(f[0], f[1], f[2]) for f in fields)

# ---------------------------------------------------------------------------
# Path & Configuration Discovery
# ---------------------------------------------------------------------------


def normalize_path(p):
    """Normalize file path for cross-platform comparisons."""
    if not p:
        return ''
    p = urllib.parse.unquote(p)
    if p.startswith('file:///'):
        p = p[8:]
    return p.replace('\\', '/').rstrip('/').lower()


def is_session_for_project(session, project):
    """Check if a session belongs to a project by project_id or workspace URI."""
    if not project or project.get('id') == 'ALL':
        return True

    # Direct project_id match
    if session.get('project_id') == project['id']:
        return True

    # Check workspace URIs against project folder
    proj_path = normalize_path(project.get(
        'readablePath') or project.get('folderUri', ''))
    if not proj_path:
        return False

    ws_list = session.get('workspace_uris', [])
    if isinstance(ws_list, str):
        try:
            ws_list = json.loads(ws_list)
        except Exception:
            ws_list = [ws_list] if ws_list else []

    for w in ws_list:
        norm_w = normalize_path(w)
        if norm_w and (norm_w == proj_path or norm_w.startswith(proj_path) or proj_path in norm_w):
            return True

    return False


def get_default_paths():
    home = os.path.expanduser('~')
    gemini_dir = os.path.join(home, '.gemini')
    cli_dir = os.path.join(gemini_dir, 'antigravity-cli')
    app_dir = os.path.join(gemini_dir, 'antigravity')
    config_dir = os.path.join(gemini_dir, 'config')
    return cli_dir, app_dir, config_dir


def discover_projects(config_dir):
    projects_dir = os.path.join(config_dir, 'projects')
    projects = []
    if not os.path.exists(projects_dir):
        return projects

    for fname in os.listdir(projects_dir):
        if not fname.endswith('.json'):
            continue
        # Skip special sentinel projects
        if fname in ('default-cli-project.json', 'outside-of-project.json'):
            continue
        fpath = os.path.join(projects_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
                pid = data.get('id')
                name = data.get('name', 'Unnamed Project')
                folder_uri = ''
                resources = data.get('projectResources', {}
                                     ).get('resources', [])
                if resources and 'gitFolder' in resources[0]:
                    folder_uri = resources[0]['gitFolder'].get('folderUri', '')
                readable_path = ''
                if folder_uri:
                    clean_uri = urllib.parse.unquote(folder_uri)
                    if clean_uri.startswith('file:///'):
                        readable_path = clean_uri[8:]
                        if len(readable_path) > 2 and readable_path[1] == ':':
                            readable_path = readable_path[0].upper(
                            ) + readable_path[1:]
                projects.append({
                    'id': pid,
                    'name': name,
                    'folderUri': folder_uri,
                    'readablePath': readable_path or folder_uri,
                    'session_count': 0
                })
        except Exception:
            continue
    projects.sort(key=lambda p: p['name'].lower())
    return projects


def check_installations(cli_dir, app_dir, config_dir):
    """Return a list of human-readable problems; empty list means both installs are usable."""
    problems = []
    for name, d in (('Antigravity CLI', cli_dir), ('Antigravity Desktop', app_dir)):
        if not os.path.isdir(d):
            problems.append(f"{name} not found ({d})")
        elif not os.path.exists(os.path.join(d, 'conversation_summaries.db')):
            problems.append(
                f"{name} has no session database ({os.path.join(d, 'conversation_summaries.db')})")
    if not discover_projects(config_dir):
        problems.append(
            f"No Antigravity projects found ({os.path.join(config_dir, 'projects')})")
    return problems


def _read_cids(sum_db):
    if not os.path.exists(sum_db):
        return set()
    try:
        con = sqlite3.connect(sum_db)
        try:
            return {r[0] for r in con.execute('SELECT conversation_id FROM conversation_summaries')}
        finally:
            con.close()
    except Exception:
        return set()


def discover_sessions(src_dir, dst_dir, target_project_id, direction=TO_DESKTOP):
    """Load all valid root sessions from src_dir, grouping subagents under root parents."""
    sum_db = os.path.join(src_dir, 'conversation_summaries.db')
    if not os.path.exists(sum_db):
        return []

    con = sqlite3.connect(sum_db)
    cur = con.cursor()
    cols = [c[1] for c in cur.execute(
        'PRAGMA table_info(conversation_summaries)').fetchall()]
    rows = cur.execute(
        'SELECT * FROM conversation_summaries ORDER BY last_modified_time DESC').fetchall()
    all_sessions = []
    for r in rows:
        d = dict(zip(cols, r))
        all_sessions.append(d)
    con.close()

    # Query destination summaries to check what's already migrated
    app_migrated_map = {}
    app_sum_db = os.path.join(dst_dir, 'conversation_summaries.db')
    if os.path.exists(app_sum_db):
        try:
            acon = sqlite3.connect(app_sum_db)
            acur = acon.cursor()
            for row in acur.execute('SELECT conversation_id, project_id, source, title FROM conversation_summaries').fetchall():
                app_migrated_map[row[0]] = {
                    'project_id': row[1] or '',
                    'source': row[2] or '',
                    'title': row[3] or ''
                }
            acon.close()
        except Exception:
            pass

    # Map subagents to their parents
    subagents_by_parent = {}
    for s in all_sessions:
        parent_id = s.get('parent_conversation_id', '')
        if parent_id:
            subagents_by_parent.setdefault(parent_id, []).append(s)

    # Filter to root sessions and discard ghost uninitialized sessions
    root_sessions = []
    for s in all_sessions:
        parent_id = s.get('parent_conversation_id', '')
        nesting = s.get('nesting_depth', 0)
        if not parent_id and nesting == 0:
            cid = s['conversation_id']
            step_count = s.get('step_count', 0)
            mod_time = s.get('last_modified_time', '') or ''
            raw_title = s.get('title', '') or ''
            raw_preview = s.get('preview', '') or ''

            # FILTER OUT GHOST / CORRUPTED EMPTY SESSIONS
            if (step_count == 0 and not raw_title.strip() and not raw_preview.strip()) or mod_time.startswith('0001'):
                continue
            # Skip CLI-watcher mirrors inside the Desktop store
            if (s.get('source') or '') == CLI_SOURCE_NAME:
                continue

            # Determine migration status (check deterministic mapped cid or native cid in dst_dir/conversations)
            d_cid = map_cid(cid, direction)
            app_conv_db = os.path.join(dst_dir, 'conversations', f'{d_cid}.db')
            native_conv_db = os.path.join(
                dst_dir, 'conversations', f'{cid}.db')
            app_info = app_migrated_map.get(d_cid) or app_migrated_map.get(cid)
            has_desktop_db = os.path.exists(app_conv_db) or (os.path.exists(
                native_conv_db) and app_info and app_info['source'] != 'CORTEX_TRAJECTORY_SOURCE_CLI')
            if has_desktop_db and app_info and app_info['source'] != 'CORTEX_TRAJECTORY_SOURCE_CLI':
                if app_info['project_id'] == target_project_id:
                    status = 'Migrated'
                else:
                    status = 'In Other Project'
            elif os.path.exists(app_conv_db) and not app_info:
                status = 'Migrated'
            else:
                status = 'New'

            # Load title from annotation if title is empty
            title = raw_title
            for ann_base, check_id in ((src_dir, cid), (dst_dir, d_cid), (dst_dir, cid)):
                if title:
                    break
                ann_path = os.path.join(
                    ann_base, 'annotations', f'{check_id}.pbtxt')
                if os.path.exists(ann_path):
                    try:
                        with open(ann_path, 'r', encoding='utf-8') as af:
                            content = af.read()
                            if 'title:"' in content:
                                title = content.split('title:"')[
                                    1].split('"')[0]
                            elif 'title: "' in content:
                                title = content.split('title: "')[
                                    1].split('"')[0]
                    except Exception:
                        pass
            if not title and app_info and app_info.get('title'):
                title = app_info['title']
            if not title:
                title = raw_preview or 'Untitled Session'

            clean_title = title.replace('\n', ' ').strip()
            children = subagents_by_parent.get(cid, [])

            if ' ' in mod_time:
                date_str = mod_time.split(' ')[0]
            elif 'T' in mod_time:
                date_str = mod_time.split('T')[0]
            else:
                date_str = mod_time[:10]

            root_sessions.append({
                'conversation_id': cid,
                'desktop_cid': d_cid,
                'title': title,
                'clean_title': clean_title,
                'preview': raw_preview,
                'step_count': step_count,
                'date': date_str,
                'status': status,
                'project_id': s.get('project_id', ''),
                'subagents': [c['conversation_id'] for c in children],
                'workspace_uris': s.get('workspace_uris', '[]')
            })

    return root_sessions

# ---------------------------------------------------------------------------
# Antigravity 2.0 Live RPC Client
# ---------------------------------------------------------------------------


def is_pid_running(pid):
    """Fast check whether a process ID is currently alive cross-platform."""
    if not pid or pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(0x1000, False, pid)
            if not h_proc:
                return False
            exit_code = ctypes.c_ulong()
            # STILL_ACTIVE = 259
            if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                kernel32.CloseHandle(h_proc)
                return exit_code.value == 259
            kernel32.CloseHandle(h_proc)
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def is_port_listening(port, timeout=0.05):
    """Fast probe to check if a TCP port is actively listening on localhost."""
    if not port or port <= 0:
        return False
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def get_process_name(pid):
    """Cross-platform process name lookup for a given PID."""
    if not pid or pid <= 0:
        return ""
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(0x1000, False, pid)
            if not h_proc:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(512)
                size = wintypes.DWORD(512)
                if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                    return buf.value.lower()
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            return ""
    elif os.name == 'posix':
        try:
            comm = f"/proc/{pid}/comm"
            if os.path.exists(comm):
                with open(comm, 'r', encoding='utf-8') as f:
                    return f.read().strip().lower()
        except Exception:
            pass
    return ""


def detect_live_hub(app_dir):
    """
    Detect running Antigravity Desktop Language Server.
    Uses instant PID, process image, and TCP port checks before touching HTTP to prevent hanging.
    """
    daemon_dir = os.path.join(app_dir, 'daemon')
    if not os.path.exists(daemon_dir):
        return None

    hub_files = [f for f in os.listdir(daemon_dir) if f.startswith(
        'hub_') and f.endswith('.json')]
    if not hub_files:
        return None

    hub_files.sort(key=lambda f: os.path.getmtime(
        os.path.join(daemon_dir, f)), reverse=True)
    # Use proxy-free opener for localhost to avoid Windows system proxy discovery delays
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    for hf in hub_files:
        hpath = os.path.join(daemon_dir, hf)
        try:
            with open(hpath, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
            pid = data.get('pid')
            http_port = data.get('httpPort')
            csrf = data.get('csrfToken')

            # 1. Fast PID check (instant ~0ms): skip stale dead hub files
            if not is_pid_running(pid):
                continue

            # 2. Process check: ignore CLI ('agy') hubs
            proc_path = get_process_name(pid)
            if proc_path.endswith('agy.exe') or proc_path.endswith('/agy'):
                continue

            # 3. Fast TCP port probe (instant ~0ms): skip closed ports
            if not is_port_listening(http_port):
                continue

            if http_port and csrf:
                url = f'http://127.0.0.1:{http_port}/exa.language_server_pb.LanguageServerService/GetWorkspaceInfos'
                req = urllib.request.Request(url, data=b'{}', headers={
                    'Content-Type': 'application/json',
                    'x-codeium-csrf-token': csrf
                })
                with opener.open(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        return {
                            'httpPort': http_port,
                            'csrfToken': csrf,
                            'pid': pid
                        }
        except Exception:
            continue
    return None


def rpc_write_summary(hub, cascade_id, summary_dict):
    url = f"http://127.0.0.1:{hub['httpPort']}/exa.language_server_pb.LanguageServerService/JetboxWriteSummary"
    payload = json.dumps(
        {'cascadeId': cascade_id, 'summary': summary_dict}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json',
        'x-codeium-csrf-token': hub['csrfToken']
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return resp.status == 200


def rpc_update_annotations(hub, cascade_id, title):
    url = f"http://127.0.0.1:{hub['httpPort']}/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations"
    payload = json.dumps({
        'cascadeId': cascade_id,
        'annotations': {'title': title},
        'mergeAnnotations': True
    }).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json',
        'x-codeium-csrf-token': hub['csrfToken']
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return resp.status == 200

# ---------------------------------------------------------------------------
# Migration Logic (100% Non-destructive)
# ---------------------------------------------------------------------------


def to_rfc3339(ts_str):
    """Convert SQLite timestamp ('YYYY-MM-DD HH:MM:SS.ffffff+00:00') to strict RFC3339 ('YYYY-MM-DDTHH:MM:SS.ffffffZ')."""
    if not ts_str or not isinstance(ts_str, str):
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    ts = ts_str.strip()
    if not ts or ts.startswith('0001'):
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    if ' ' in ts and 'T' not in ts:
        parts = ts.split(' ')
        ts = parts[0] + 'T' + parts[1]
    if ts.endswith('+00:00') or ts.endswith('-00:00'):
        ts = ts[:-6] + 'Z'
    elif not ts.endswith('Z') and '+' not in ts[10:] and '-' not in ts[10:]:
        ts = ts + 'Z'
    return ts


def ensure_checkpoint_title_in_db(cur, title):
    """
    Ensure that computeCascadeTitle in language_server.exe (which checks steps 0..49 for
    step_type == 23 -> field 30 -> subfield 4 task_title) resolves the proper session title
    even if language_server.exe ever recomputes the summary from the .db file.
    """
    if not title:
        return
    try:
        rows = cur.execute(
            'SELECT idx, step_type, status, metadata, step_payload FROM steps WHERE idx < 50 ORDER BY idx ASC').fetchall()
        if not rows:
            return
        # 1. If any step in 0..49 already has step_type == 23, update its field 30 -> subfield 4
        for s_idx, s_type, s_status, s_meta, s_payload in rows:
            if s_type == 23 and s_status == 3 and s_payload:
                s_fields = parse_fields(s_payload)
                new_s_fields = []
                updated = False
                for fn, wt, val in s_fields:
                    if fn == 30 and wt == 2:
                        cp_fields = [f for f in parse_fields(val) if f[0] != 4]
                        cp_fields.insert(0, (4, 2, title.encode('utf-8')))
                        new_s_fields.append(
                            (30, 2, serialize_fields(cp_fields)))
                        updated = True
                    else:
                        new_s_fields.append((fn, wt, val))
                if updated:
                    cur.execute('UPDATE steps SET step_payload = ? WHERE idx = ?',
                                (serialize_fields(new_s_fields), s_idx))
                    return

        # 2. If there is a system notice step (step_type == 101) in 0..49, convert it into a checkpoint title step
        for s_idx, s_type, s_status, s_meta, s_payload in rows:
            if s_type == 101 and s_status == 3 and s_payload:
                s_fields = [f for f in parse_fields(
                    s_payload) if f[0] in (4, 5)]
                s_fields.insert(0, (1, 0, 23))
                cp_bytes = serialize_fields(
                    [(4, 2, title.encode('utf-8')), (9, 0, 1), (12, 0, s_idx), (24, 0, 1), (25, 0, 0)])
                s_fields.append((30, 2, cp_bytes))
                cur.execute('UPDATE steps SET step_type = 23, step_payload = ? WHERE idx = ?',
                            (serialize_fields(s_fields), s_idx))
                return

        # 3. For short sessions (< 50 steps) with no step_type 23 or 101, append a checkpoint title step at max_idx + 1
        max_idx, _, _, last_meta, last_payload = rows[-1]
        if max_idx + 1 < 50:
            new_idx = max_idx + 1
            meta_bytes = last_meta or b''
            if meta_bytes:
                m_fields = []
                for fn, wt, val in parse_fields(meta_bytes):
                    if fn == 3:
                        m_fields.append((3, 0, 5))
                    elif fn == 20 and wt == 2:
                        f20 = [f for f in parse_fields(val) if f[0] != 2]
                        f20.append((2, 0, new_idx))
                        m_fields.append((20, 2, serialize_fields(f20)))
                    else:
                        m_fields.append((fn, wt, val))
                meta_bytes = serialize_fields(m_fields)
            cp_bytes = serialize_fields([(4, 2, title.encode(
                'utf-8')), (9, 0, 1), (12, 0, new_idx), (24, 0, 1), (25, 0, 0)])
            payload_bytes = serialize_fields(
                [(1, 0, 23), (4, 0, 3), (5, 2, meta_bytes), (30, 2, cp_bytes)])
            cur.execute(
                'INSERT INTO steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) VALUES (?, 23, 3, 0, ?, ?, 0)',
                (new_idx, meta_bytes, payload_bytes)
            )
    except Exception:
        pass


def replace_uuids_in_bytes(blob, byte_id_map):
    """Replace 36-byte ASCII UUIDs in a binary blob (length-invariant for Protobuf)."""
    if not blob or not byte_id_map:
        return blob
    res = blob
    for old_b, new_b in byte_id_map.items():
        if old_b in res:
            res = res.replace(old_b, new_b)
    return res


def migrate_single_session(cid, src_dir, dst_dir, target_project, hub=None, title=None, id_map=None, direction=TO_DESKTOP):
    """Migrate a single conversation ID from src_dir into dst_dir using its deterministic mapped UUID."""
    d_cid = (id_map or {}).get(cid) or map_cid(cid, direction)
    byte_id_map = {k.encode('utf-8'): v.encode('utf-8')
                   for k, v in (id_map or {cid: d_cid}).items()}
    # Also include brain directory path replacement if present in blobs
    src_db = os.path.join(src_dir, 'conversations', f'{cid}.db')
    dst_db = os.path.join(dst_dir, 'conversations', f'{d_cid}.db')

    if os.path.exists(src_db):
        os.makedirs(os.path.dirname(dst_db), exist_ok=True)
        shutil.copy2(src_db, dst_db)

    folder_uri = target_project.get('folderUri', '')
    now_ts = time.time()

    if os.path.exists(dst_db):
        con = sqlite3.connect(dst_db)
        cur = con.cursor()
        traj_source = 1  # Desktop trajectory source
        cur.execute(
            'UPDATE trajectory_meta SET cascade_id = ?, source = ?', (d_cid, traj_source))

        try:
            blob_row = cur.execute(
                'SELECT data FROM trajectory_metadata_blob WHERE id = "main"').fetchone()
            if blob_row and blob_row[0]:
                raw_blob = replace_uuids_in_bytes(blob_row[0], byte_id_map)
                meta_fields = parse_fields(raw_blob)
                new_meta = []
                has_proj = False
                has_ws = False
                for f in meta_fields:
                    if f[0] == 18:
                        new_meta.append(
                            (18, 2, target_project['id'].encode('utf-8')))
                        has_proj = True
                    elif f[0] == 7 and folder_uri:
                        new_meta.append((7, 2, folder_uri.encode('utf-8')))
                        has_ws = True
                    else:
                        new_meta.append(f)
                if not has_proj:
                    new_meta.append(
                        (18, 2, target_project['id'].encode('utf-8')))
                if not has_ws and folder_uri:
                    new_meta.append((7, 2, folder_uri.encode('utf-8')))
                new_blob = serialize_fields(new_meta)
                cur.execute(
                    'UPDATE trajectory_metadata_blob SET data = ? WHERE id = "main"', (new_blob,))
        except Exception:
            pass

        # Rewrite UUID references in steps, gen_metadata, and parent_references
        try:
            step_rows = cur.execute(
                'SELECT idx, metadata, step_payload FROM steps').fetchall()
            for s_idx, s_meta, s_payload in step_rows:
                new_m = replace_uuids_in_bytes(s_meta, byte_id_map)
                new_p = replace_uuids_in_bytes(s_payload, byte_id_map)
                if new_m != s_meta or new_p != s_payload:
                    cur.execute(
                        'UPDATE steps SET metadata = ?, step_payload = ? WHERE idx = ?', (new_m, new_p, s_idx))
        except Exception:
            pass

        try:
            gen_rows = cur.execute(
                'SELECT idx, data FROM gen_metadata').fetchall()
            for g_idx, g_data in gen_rows:
                new_g = replace_uuids_in_bytes(g_data, byte_id_map)
                if new_g != g_data:
                    cur.execute(
                        'UPDATE gen_metadata SET data = ? WHERE idx = ?', (new_g, g_idx))
        except Exception:
            pass

        try:
            pref_rows = cur.execute(
                'SELECT idx, data FROM parent_references').fetchall()
            for p_idx, p_data in pref_rows:
                new_pr = replace_uuids_in_bytes(p_data, byte_id_map)
                if new_pr != p_data:
                    cur.execute(
                        'UPDATE parent_references SET data = ? WHERE idx = ?', (new_pr, p_idx))
        except Exception:
            pass

        if title:
            ensure_checkpoint_title_in_db(cur, title)

        con.commit()
        con.close()

        try:
            os.utime(dst_db, (now_ts, now_ts))
        except Exception:
            pass

    # Clean up any legacy same-UUID copy in dst_dir/conversations so it doesn't collide
    legacy_dst_db = os.path.join(dst_dir, 'conversations', f'{cid}.db')
    if d_cid != cid and os.path.exists(legacy_dst_db):
        try:
            os.remove(legacy_dst_db)
        except Exception:
            pass

    src_brain = os.path.join(src_dir, 'brain', cid)
    dst_brain = os.path.join(dst_dir, 'brain', d_cid)
    if os.path.exists(src_brain):
        os.makedirs(os.path.dirname(dst_brain), exist_ok=True)
        shutil.copytree(src_brain, dst_brain, dirs_exist_ok=True)

    src_ann = os.path.join(src_dir, 'annotations', f'{cid}.pbtxt')
    dst_ann = os.path.join(dst_dir, 'annotations', f'{d_cid}.pbtxt')
    if os.path.exists(src_ann):
        os.makedirs(os.path.dirname(dst_ann), exist_ok=True)
        shutil.copy2(src_ann, dst_ann)
    elif title:
        os.makedirs(os.path.dirname(dst_ann), exist_ok=True)
        try:
            with open(dst_ann, 'w', encoding='utf-8') as af:
                escaped_title = title.replace('\\', '\\\\').replace('"', '\\"')
                af.write(f'title:"{escaped_title}"\n')
        except Exception:
            pass


def clean_and_update_raw_summary(raw_summary, title, project_id, folder_uri, direction=TO_DESKTOP):
    """Clean protobuf raw_summary: set/strip source (field 20) per direction and update projectId (field 18)."""
    if not raw_summary:
        return raw_summary
    try:
        fields = parse_fields(raw_summary)
        new_fields = []
        has_title_annotation = False
        has_summary_field = False
        for fnum, wtype, val in fields:
            if fnum == 1:
                # summary text
                new_fields.append((1, 2, title.encode('utf-8')))
                has_summary_field = True
            elif fnum == 15:
                # annotations
                has_title_annotation = True
                ann_fields = parse_fields(val)
                new_ann = [f for f in ann_fields if f[0] != 1]
                new_ann.insert(0, (1, 2, title.encode('utf-8')))
                new_fields.append((15, 2, serialize_fields(new_ann)))
            elif fnum == 17:
                # trajectoryMetadata
                meta_fields = parse_fields(val)
                new_meta = []
                has_proj = False
                has_ws = False
                for mfnum, mwtype, mval in meta_fields:
                    if mfnum == 18:
                        new_meta.append((18, 2, project_id.encode('utf-8')))
                        has_proj = True
                    elif mfnum == 7 and folder_uri:
                        new_meta.append((7, 2, folder_uri.encode('utf-8')))
                        has_ws = True
                    else:
                        new_meta.append((mfnum, mwtype, mval))
                if not has_proj:
                    new_meta.append((18, 2, project_id.encode('utf-8')))
                if not has_ws and folder_uri:
                    new_meta.append((7, 2, folder_uri.encode('utf-8')))
                new_fields.append((17, 2, serialize_fields(new_meta)))
            elif fnum == 20:
                # Field 20 is source enum -> strip for Desktop, re-added as CLI below for CLI target
                pass
            else:
                new_fields.append((fnum, wtype, val))

        if not has_summary_field:
            new_fields.insert(0, (1, 2, title.encode('utf-8')))

        if not has_title_annotation:
            ann_bytes = encode_field(1, 2, title.encode('utf-8'))
            new_fields.append((15, 2, ann_bytes))

        return serialize_fields(new_fields)
    except Exception:
        return raw_summary


def update_proto_summaries_file(app_dir, cid, new_raw_summary, mtime_sec=None):
    """Ensure agyhub_summaries_proto.pb has the clean summary AND file_mod_times entry for persistence."""
    pb_path = os.path.join(app_dir, 'agyhub_summaries_proto.pb')
    if not os.path.exists(pb_path) or not new_raw_summary:
        return
    try:
        with open(pb_path, 'rb') as f:
            pb_data = f.read()

        fields = parse_fields(pb_data)
        entries = {}
        mod_times = {}
        other_fields = []
        for fnum, wtype, val in fields:
            if fnum == 1 and wtype == 2:
                inner = parse_fields(val)
                ecid = ''
                f2 = None
                for ifnum, iwtype, ival in inner:
                    if ifnum == 1:
                        ecid = ival.decode('utf-8', 'replace')
                    if ifnum == 2:
                        f2 = ival
                if ecid and f2 is not None:
                    entries[ecid] = f2
            elif fnum == 2 and wtype == 2:
                inner = parse_fields(val)
                ecid = ''
                mval = None
                for ifnum, iwtype, ival in inner:
                    if ifnum == 1:
                        ecid = ival.decode('utf-8', 'replace')
                    if ifnum == 2:
                        mval = (iwtype, ival)
                if ecid and mval is not None:
                    mod_times[ecid] = mval
            else:
                other_fields.append((fnum, wtype, val))

        entries[cid] = new_raw_summary
        if mtime_sec is not None:
            mod_times[cid] = (0, int(mtime_sec))

        root_fields = []
        for ecid, raw in entries.items():
            entry_bytes = encode_field(1, 2, ecid.encode(
                'utf-8')) + encode_field(2, 2, raw)
            root_fields.append((1, 2, entry_bytes))
        for ecid, (mwt, mval) in mod_times.items():
            mt_bytes = encode_field(1, 2, ecid.encode(
                'utf-8')) + encode_field(2, mwt, mval)
            root_fields.append((2, 2, mt_bytes))
        root_fields.extend(other_fields)

        new_pb_data = serialize_fields(root_fields)
        with open(pb_path, 'wb') as f:
            f.write(new_pb_data)
    except Exception:
        pass


def sync_session_summaries(session, src_dir, dst_dir, target_project, hub=None, id_map=None, direction=TO_DESKTOP):
    """Sync summaries into the target store (Desktop: live RPC or direct store write; CLI: direct write)."""
    cid = session['conversation_id']
    d_cid = (id_map or {}).get(cid) or session.get(
        'desktop_cid') or map_cid(cid, direction)
    byte_id_map = {k.encode('utf-8'): v.encode('utf-8')
                   for k, v in (id_map or {cid: d_cid}).items()}
    title = session['title']
    preview = session['preview'] or title
    step_count = session['step_count']

    src_sum_db = os.path.join(src_dir, 'conversation_summaries.db')
    raw_summary = None
    last_mod = ''
    last_input_time = ''
    last_input_step = -1
    if os.path.exists(src_sum_db):
        con = sqlite3.connect(src_sum_db)
        cur = con.cursor()
        try:
            r = cur.execute(
                'SELECT raw_summary, last_modified_time, last_user_input_time, last_user_input_step_index FROM conversation_summaries WHERE conversation_id = ?', (cid,)).fetchone()
            if r:
                raw_summary, last_mod, last_input_time, last_input_step = r
        except sqlite3.OperationalError:
            try:
                r = cur.execute(
                    'SELECT raw_summary, last_modified_time FROM conversation_summaries WHERE conversation_id = ?', (cid,)).fetchone()
                if r:
                    raw_summary, last_mod = r
            except Exception:
                pass
        con.close()

    folder_uri = target_project.get('folderUri', '')
    ws_uri = folder_uri or session.get('workspace_uris', '[]')
    if isinstance(ws_uri, list):
        ws_uri_json = json.dumps(ws_uri)
    elif ws_uri.startswith('[') and ws_uri.endswith(']'):
        ws_uri_json = ws_uri
    else:
        ws_uri_json = json.dumps([ws_uri])

    dst_db = os.path.join(dst_dir, 'conversations', f'{d_cid}.db')
    mtime_sec = int(os.path.getmtime(dst_db)) if os.path.exists(
        dst_db) else int(time.time())

    if hub:
        # Allow fsnotify (handleEvent) in language_server.exe to process the dst_db write
        # and record Store.fileModTimes[d_cid] = mtime(dst_db) in memory BEFORE we write
        # the canonical summary and title via RPC.
        time.sleep(0.6)
        rfc_last_mod = to_rfc3339(last_mod)
        rfc_last_input = to_rfc3339(last_input_time or last_mod)
        ws_entry_uri = folder_uri or target_project.get('readablePath', '')
        summary_payload = {
            'summary': title,
            'stepCount': step_count,
            'lastModifiedTime': rfc_last_mod,
            'status': 'CASCADE_RUN_STATUS_IDLE',
            'createdTime': rfc_last_mod,
            'source': 'CORTEX_TRAJECTORY_SOURCE_UNSPECIFIED',
            'workspaces': [{
                'workspaceFolderAbsoluteUri': ws_entry_uri,
                'gitRootAbsoluteUri': ws_entry_uri
            }],
            'lastUserInputTime': rfc_last_input,
            'lastUserInputStepIndex': max(0, int(last_input_step)) if last_input_step is not None else 0,
            'annotations': {
                'title': title
            },
            'trajectoryMetadata': {
                'initializationStateId': d_cid,
                'rootConversationId': d_cid,
                'workspaceUris': [ws_entry_uri],
                'projectId': target_project['id']
            },
            'trajectoryType': 'CORTEX_TRAJECTORY_TYPE_CASCADE'
        }
        rpc_write_summary(hub, d_cid, summary_payload)
        rpc_update_annotations(hub, d_cid, title)

    # Clean and update raw protobuf summary (strip CLI source, update projectId, replace cid -> d_cid)
    new_raw_summary = clean_and_update_raw_summary(
        raw_summary, title, target_project['id'], folder_uri, direction)
    new_raw_summary = replace_uuids_in_bytes(new_raw_summary, byte_id_map)

    # Update backing SQLite DB
    app_sum_db = os.path.join(dst_dir, 'conversation_summaries.db')
    if os.path.exists(app_sum_db):
        con = sqlite3.connect(app_sum_db)
        cur = con.cursor()

        cols = {c[1] for c in cur.execute(
            'PRAGMA table_info(conversation_summaries)').fetchall()}
        if 'last_user_input_time' in cols:
            cur.execute('''INSERT INTO conversation_summaries
                (conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, source, project_id, nesting_depth, parent_conversation_id, last_user_input_time, last_user_input_step_index, raw_summary)
                VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', '', ?, 0, '', ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                title = excluded.title,
                preview = excluded.preview,
                step_count = excluded.step_count,
                last_modified_time = excluded.last_modified_time,
                workspace_uris = excluded.workspace_uris,
                status = 'CASCADE_RUN_STATUS_IDLE',
                source = '',
                project_id = excluded.project_id,
                raw_summary = excluded.raw_summary
            ''', (d_cid, title, preview, step_count, last_mod, ws_uri_json, target_project['id'], last_input_time, last_input_step, new_raw_summary))
        else:
            cur.execute('''INSERT INTO conversation_summaries
                (conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, source, project_id, nesting_depth, parent_conversation_id, raw_summary)
                VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', '', ?, 0, '', ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                title = excluded.title,
                preview = excluded.preview,
                step_count = excluded.step_count,
                last_modified_time = excluded.last_modified_time,
                workspace_uris = excluded.workspace_uris,
                status = 'CASCADE_RUN_STATUS_IDLE',
                source = '',
                project_id = excluded.project_id,
                raw_summary = excluded.raw_summary
            ''', (d_cid, title, preview, step_count, last_mod, ws_uri_json, target_project['id'], new_raw_summary))

        try:
            cur.execute('''UPDATE conversations_fts 
                SET project = ?, workspace = ?, title = ? 
                WHERE conversation_id = ? AND chunk_index = -1''', (target_project['name'], target_project['name'], title, d_cid))
        except Exception:
            pass

        con.commit()
        con.close()

    # Update agyhub_summaries_proto.pb directly (preserving file_mod_times)
    update_proto_summaries_file(
        dst_dir, d_cid, new_raw_summary, mtime_sec=mtime_sec)

# ---------------------------------------------------------------------------
# Interactive TUI Screens
# ---------------------------------------------------------------------------


def prompt_desktop_not_running():
    """
    Display warning when Antigravity Desktop is not running.
    Returns:
        'RETRY': user pressed [Enter] or [Space]
        'QUIT': user pressed [Esc], [q], or [Ctrl+C]
    """
    clear_screen()
    cols, _ = get_terminal_size()
    w = max(88, min(cols, 105))
    border = '=' * w
    print(f"\033[1;33m{border}")
    print(" [!] Antigravity Desktop Is Not Running")
    print(f"{border}\033[0m\n")
    print("Migrating sessions into Antigravity Desktop requires the Desktop app to be open")
    print("so sessions can be registered live with its language server.\n")
    print("Please launch Antigravity Desktop, then:\n")
    print("  \033[1;32m[Enter]\033[0m  Retry connection")
    print("  \033[1;90m[Esc/q]\033[0m  Quit application\n")
    sys.stdout.flush()
    while True:
        k = get_key()
        if k in ('ENTER', 'SPACE'):
            return 'RETRY'
        if k in ('ESC', 'q', 'CTRL_C'):
            return 'QUIT'


def select_project_screen(projects, direction=TO_DESKTOP, initial_idx=1):
    """
    Step 1: Select target project via arrow keys and Enter.
    Row 0: < Exit Application
    Rows 1..N: projects
    Esc/q quits.
    """
    total = len(projects)
    total_rows = total + 1
    cursor_idx = max(0, min(initial_idx, total_rows - 1))
    page_size = 14
    scroll_offset = 0

    while True:
        clear_screen()
        cols, rows = get_terminal_size()
        w = max(88, min(cols, 105))
        border = '=' * w

        print(f"\033[1;36m{border}")
        print(f"             Antigravity Session Migrator: CLI -> Desktop             ")
        print(f"{border}\033[0m")
        print("\n\033[1mStep 1: Select Target Project\033[0m")
        nav_hint = "Use [Up/Down] arrows to navigate, [Enter] to select, [Esc/q] to exit"
        print(f"\033[90m{nav_hint}\033[0m\n")

        # Row 0: Exit Button
        is_exit_cur = (cursor_idx == 0)
        exit_text = "  < Exit Application  "
        if is_exit_cur:
            hint = "\033[90m(Press Enter or Esc to exit)\033[0m"
            print(f"\033[1;31;7m[ {exit_text} ]\033[0m   {hint}")
        else:
            print(f"\033[31m[ {exit_text} ]\033[0m")
        print("\033[90m" + ("-" * w) + "\033[0m")

        if cursor_idx >= 1:
            proj_idx = cursor_idx - 1
            if proj_idx < scroll_offset:
                scroll_offset = proj_idx
            elif proj_idx >= scroll_offset + page_size:
                scroll_offset = proj_idx - page_size + 1

        visible_projects = projects[scroll_offset:scroll_offset + page_size]

        for rel_idx, p in enumerate(visible_projects):
            actual_idx = scroll_offset + rel_idx
            row_idx = actual_idx + 1
            is_cur = (row_idx == cursor_idx)
            ptr = "\033[1;36m>\033[0m " if is_cur else "  "

            p_name = p['name'][:20].ljust(20)
            cnt_str = f"[{p.get('session_count', 0)} sessions]".rjust(15)
            path_str = f"({p['readablePath']})"

            line = f"{ptr}\033[1m{p_name}\033[0m \033[33m{cnt_str}\033[0m \033[90m{path_str}\033[0m"
            plain_line = f"{ptr}{p_name} {cnt_str} {path_str}"

            if is_cur:
                print(f"\033[7m{plain_line}\033[0m")
            else:
                print(line)

        if total > page_size:
            print(
                f"\n  \033[90m... Showing {scroll_offset+1}-{min(scroll_offset+page_size, total)} of {total} projects ...\033[0m")

        print("\033[90m" + ("-" * w) + "\033[0m")
        sys.stdout.flush()

        k = get_key()
        if k in ('UP', 'k'):
            cursor_idx = (cursor_idx - 1) % total_rows
        elif k in ('DOWN', 'j'):
            cursor_idx = (cursor_idx + 1) % total_rows
        elif k == 'PAGE_UP':
            if cursor_idx == 0:
                cursor_idx = total_rows - 1
            else:
                cursor_idx = max(0, cursor_idx - page_size)
        elif k == 'PAGE_DOWN':
            if cursor_idx == total_rows - 1:
                cursor_idx = 0
            else:
                cursor_idx = min(total_rows - 1, cursor_idx + page_size)
        elif k == 'HOME':
            cursor_idx = 0
        elif k == 'END':
            cursor_idx = total_rows - 1
        elif k in ('ESC', 'q', 'CTRL_C'):
            return 'QUIT', cursor_idx
        elif k == 'ENTER':
            if cursor_idx == 0:
                return 'QUIT', cursor_idx
            return projects[cursor_idx - 1], cursor_idx


def select_sessions_screen(all_sessions, target_project, direction=TO_DESKTOP):
    """
    Step 2: Select sessions to migrate.
    Columns are fixed-width padded and aligned.
    Esc/b returns to Step 1, q quits.
    'f' toggles filtering by selected project.
    """
    BACK_IDX = 0
    selected_set = set()

    # Filter by project by default if there are matching sessions
    matching_sessions = [
        s for s in all_sessions if is_session_for_project(s, target_project)]
    filter_active = (len(matching_sessions) > 0)

    cursor_idx = 1
    page_size = 14
    scroll_offset = 0
    status_message = ""

    while True:
        # Determine currently active session view
        if filter_active and matching_sessions:
            current_sessions = matching_sessions
        else:
            current_sessions = all_sessions

        total_sessions = len(current_sessions)
        CONFIRM_IDX = total_sessions + 1
        total_rows = total_sessions + 2

        # Ensure cursor within bounds
        if cursor_idx >= total_rows:
            cursor_idx = CONFIRM_IDX

        clear_screen()
        cols, rows = get_terminal_size()
        w = max(88, min(cols, 110))
        border = '=' * w

        # Header with Project & Filter Info
        if filter_active and matching_sessions:
            filter_note = f"Matching '{target_project['name']}' only ({len(current_sessions)} sessions) - press 'f' to show all {len(all_sessions)}"
        else:
            filter_note = f"Showing all {len(all_sessions)} sessions - press 'f' to filter"

        proj_title = f"\033[1;32m{target_project['name']}\033[0m \033[90m({target_project['id']})\033[0m"

        print(f"\033[1;36m{border}")
        print(f"  Target Project: {proj_title}")
        print(f"  Filter:         \033[90m[{filter_note}]\033[0m")
        print(f"{border}\033[0m")
        print("\n\033[1mStep 2: Select Sessions to Migrate\033[0m")
        print("\033[90m[Up/Down] Navigate | [Space/Enter] Toggle | [a] All | [f] Filter | [Esc/b] Back | [q] Quit\033[0m\n")

        # ----------------- Row 0: Back Button -----------------
        is_back_cur = (cursor_idx == BACK_IDX)
        back_text = "  < Back to Project Selection  "
        if is_back_cur:
            hint = "\033[90m(Press Enter or Esc to change project)\033[0m"
            print(f"\033[1;33;7m[ {back_text} ]\033[0m   {hint}")
        else:
            print(f"\033[33m[ {back_text} ]\033[0m")
        print("\033[90m" + ("-" * w) + "\033[0m")

        # ----------------- Rows 1..N: Sessions -----------------
        if 1 <= cursor_idx <= total_sessions:
            sess_idx = cursor_idx - 1
            if sess_idx < scroll_offset:
                scroll_offset = sess_idx
            elif sess_idx >= scroll_offset + page_size:
                scroll_offset = sess_idx - page_size + 1

        visible_sessions = current_sessions[scroll_offset:scroll_offset + page_size]

        # Determine adaptive title width based on terminal size
        title_width = 38
        if cols >= 115:
            title_width = min(50, cols - 65)

        for rel_idx, s in enumerate(visible_sessions):
            actual_sess_idx = scroll_offset + rel_idx
            row_idx = actual_sess_idx + 1
            is_cur = (row_idx == cursor_idx)
            is_checked = s['conversation_id'] in selected_set

            box = "[x] " if is_checked else "[ ] "
            ptr = "> " if is_cur else "  "

            # 1. Title Column (Fixed exact width)
            raw_title = s['clean_title']
            if len(raw_title) > title_width:
                title_disp = raw_title[:title_width - 3] + '...'
            else:
                title_disp = raw_title.ljust(title_width)

            # 2. Subagents Column (Fixed exact width 12 chars)
            if s['subagents']:
                subs_text = f"(+{len(s['subagents'])} subs)".ljust(12)
                subs_col = f"\033[90m{subs_text}\033[0m"
            else:
                subs_text = " " * 12
                subs_col = subs_text

            # 3. Steps Column (Fixed exact width 12 chars)
            steps_text = f"{s['step_count']} steps".rjust(12)
            steps_col = f"\033[90m{steps_text}\033[0m"

            # 4. Separator Column (Fixed exact width 3 chars)
            sep_text = " | "
            sep_col = f"\033[90m{sep_text}\033[0m"

            # 5. Date Column (Fixed exact width 10 chars)
            date_text = s['date'][:10].ljust(10)
            date_col = f"\033[90m{date_text}\033[0m"

            # 6. Status Column (Fixed exact width 16 chars)
            if s['status'] == 'Migrated':
                st_text = "[Migrated]".ljust(16)
                status_col = f"\033[90m{st_text}\033[0m"
            elif s['status'] == 'In Other Project':
                st_text = "[Other Project]".ljust(16)
                status_col = f"\033[33m{st_text}\033[0m"
            else:
                st_text = "[New]".ljust(16)
                status_col = f"\033[1;34m{st_text}\033[0m"

            box_colored = f"\033[1;32m{box}\033[0m" if is_checked else box
            ptr_colored = "\033[1;36m>\033[0m " if is_cur else "  "

            plain_row = f"{ptr}{box}{title_disp} {subs_text} {steps_text}{sep_text}{date_text}  {st_text}"
            colored_row = f"{ptr_colored}{box_colored}{title_disp} {subs_col} {steps_col}{sep_col}{date_col}  {status_col}"

            if is_cur:
                print(f"\033[7m{plain_row}\033[0m")
            else:
                print(colored_row)

        if total_sessions > page_size:
            print(
                f"     \033[90m... Showing {scroll_offset+1}-{min(scroll_offset+page_size, total_sessions)} of {total_sessions} sessions ...\033[0m")
        else:
            print()
        print("\033[90m" + ("-" * w) + "\033[0m")

        # ----------------- Status Alert Message (if any) -----------------
        if status_message:
            print(f"\033[1;33m  {status_message}\033[0m")
            status_message = ""
        else:
            print()

        # ----------------- Bottom Row: Confirm Button -----------------
        is_confirm_cur = (cursor_idx == CONFIRM_IDX)
        btn_count = len(selected_set)
        btn_text = f"  >>> [ Confirm & Migrate ({btn_count} selected) ] <<<  "
        if is_confirm_cur:
            print(
                f"\033[1;32;7m{btn_text}\033[0m   \033[90m(Press Enter to execute)\033[0m")
        else:
            print(
                f"\033[1;32m{btn_text}\033[0m   \033[90m(or press 'c' anytime to confirm)\033[0m")

        sys.stdout.flush()
        k = get_key()

        if k in ('UP', 'k'):
            cursor_idx = (cursor_idx - 1) % total_rows
        elif k in ('DOWN', 'j'):
            cursor_idx = (cursor_idx + 1) % total_rows
        elif k in ('ESC', 'LEFT', 'b'):
            # Instant Back
            return 'BACK'
        elif k in ('q', 'CTRL_C'):
            # Quit app
            return 'QUIT'
        elif k == 'f':
            # Toggle project filter
            filter_active = not filter_active
            scroll_offset = 0
            cursor_idx = 1
            continue
        elif k == 'HOME':
            cursor_idx = BACK_IDX
        elif k == 'END':
            cursor_idx = CONFIRM_IDX
        elif k == 'PAGE_UP':
            if cursor_idx == BACK_IDX:
                cursor_idx = CONFIRM_IDX
            else:
                cursor_idx = max(BACK_IDX, cursor_idx - page_size)
        elif k == 'PAGE_DOWN':
            if cursor_idx == CONFIRM_IDX:
                cursor_idx = BACK_IDX
            else:
                cursor_idx = min(CONFIRM_IDX, cursor_idx + page_size)
        elif k == 'SPACE':
            if 1 <= cursor_idx <= total_sessions:
                cid = current_sessions[cursor_idx - 1]['conversation_id']
                if cid in selected_set:
                    selected_set.remove(cid)
                else:
                    selected_set.add(cid)
        elif k == 'a':
            # Toggle select-all for currently visible sessions
            current_cids = {s['conversation_id'] for s in current_sessions}
            if current_cids.issubset(selected_set):
                selected_set.difference_update(current_cids)
            else:
                selected_set.update(current_cids)
        elif k == 'ENTER':
            if cursor_idx == BACK_IDX:
                return 'BACK'
            elif 1 <= cursor_idx <= total_sessions:
                cid = current_sessions[cursor_idx - 1]['conversation_id']
                if cid in selected_set:
                    selected_set.remove(cid)
                else:
                    selected_set.add(cid)
            elif cursor_idx == CONFIRM_IDX:
                if len(selected_set) == 0:
                    status_message = "[!] Please select at least one session using [Space] or [Enter] before confirming."
                else:
                    return [s for s in all_sessions if s['conversation_id'] in selected_set]
        elif k == 'c':
            if len(selected_set) == 0:
                status_message = "[!] Please select at least one session using [Space] or [Enter] before confirming."
            else:
                return [s for s in all_sessions if s['conversation_id'] in selected_set]

# ---------------------------------------------------------------------------
# Main Execution Orchestrator
# ---------------------------------------------------------------------------


def run_migration():
    parser = argparse.ArgumentParser(
        description="Antigravity Session Migrator: CLI -> Desktop")
    parser.add_argument(
        '--cli-dir', help="Custom path to ~/.gemini/antigravity-cli")
    parser.add_argument(
        '--app-dir', help="Custom path to ~/.gemini/antigravity")
    parser.add_argument('--config-dir', help="Custom path to ~/.gemini/config")
    args = parser.parse_args()

    default_cli, default_app, default_config = get_default_paths()
    cli_dir = os.path.abspath(args.cli_dir or default_cli)
    app_dir = os.path.abspath(args.app_dir or default_app)
    config_dir = os.path.abspath(args.config_dir or default_config)

    init_terminal()

    # Startup check 1: verify both Antigravity CLI and Desktop installations exist
    problems = check_installations(cli_dir, app_dir, config_dir)
    if problems:
        cols, _ = get_terminal_size()
        w = max(88, min(cols, 105))
        border = '=' * w
        print(f"\033[1;31m{border}")
        print(" [!] Missing Installation / Configuration")
        print(f"{border}\033[0m\n")
        print("Both Antigravity CLI and Antigravity Desktop must be installed to migrate sessions:\n")
        for p in problems:
            print(f"  \033[1;31m*\033[0m {p}")
        print(
            f"\n\033[90mPlease ensure both environments are set up before running this migrator.\033[0m\n")
        sys.exit(1)

    # Startup check 2: Antigravity Desktop must be running
    hub = detect_live_hub(app_dir)
    while not hub:
        action = prompt_desktop_not_running()
        if action == 'QUIT':
            print("\nMigration cancelled.")
            sys.exit(0)
        hub = detect_live_hub(app_dir)

    projects = discover_projects(config_dir)
    if not projects:
        print(
            f"\033[1;31mError: No Antigravity projects found in {config_dir}/projects/\033[0m")
        sys.exit(1)

    # Initial cursor positions
    current_project_idx = 1
    cwd = os.getcwd().replace('\\', '/').lower()
    for idx, p in enumerate(projects):
        if p['readablePath'] and p['readablePath'].lower().replace('\\', '/') == cwd:
            current_project_idx = idx + 1
            break

    target_project = None
    selected_sessions = []
    step = 1

    while True:
        if step == 1:
            # Recalculate session counts for projects using CLI store
            temp_sessions = discover_sessions(
                cli_dir, app_dir, projects[0]['id'] if projects else '')
            for p in projects:
                p['session_count'] = sum(
                    1 for s in temp_sessions if is_session_for_project(s, p))

            try:
                hide_cursor()
                proj_res, current_project_idx = select_project_screen(
                    projects, initial_idx=current_project_idx)
            finally:
                show_cursor()

            if proj_res in (None, 'QUIT'):
                print("\nMigration cancelled.")
                sys.exit(0)

            target_project = proj_res
            step = 2
            continue

        elif step == 2:
            sessions = discover_sessions(
                cli_dir, app_dir, target_project['id'])
            if not sessions:
                clear_screen()
                print(
                    f"\n\033[1;33mNo sessions found to migrate in {cli_dir}.\033[0m")
                print("\nPress any key to return to project selection...")
                get_key()
                step = 1
                continue

            try:
                hide_cursor()
                sess_res = select_sessions_screen(sessions, target_project)
            finally:
                show_cursor()

            if sess_res == 'BACK':
                step = 1
                continue
            if sess_res in (None, 'QUIT'):
                print("\nMigration cancelled.")
                sys.exit(0)
            if not sess_res:
                print("\nNo sessions selected. Migration cancelled.")
                sys.exit(0)

            # Double check Desktop is still running before executing migration
            hub = detect_live_hub(app_dir)
            while not hub:
                action = prompt_desktop_not_running()
                if action == 'QUIT':
                    step = 1
                    break
                hub = detect_live_hub(app_dir)
            if not hub:
                continue

            selected_sessions = sess_res
            break

    # -----------------------------------------------------------------------
    # Step 3: Execute Migration
    # -----------------------------------------------------------------------
    clear_screen()
    print(
        f"\033[1;36m========================================================================")
    print(
        f"             Migrating Selected Sessions to {target_project['name']}             ")
    print(
        f"========================================================================\033[0m\n")

    hub = detect_live_hub(app_dir)
    if hub:
        print(
            f"\033[1;32m[OK] Antigravity Desktop Live Language Server detected (Port {hub['httpPort']}, PID {hub['pid']})\033[0m")
        print(
            f"\033[90m     Sessions will be injected live into the desktop sidebar immediately.\033[0m\n")

    success_count = 0
    total_to_migrate = len(selected_sessions)

    for idx, s in enumerate(selected_sessions, 1):
        cid = s['conversation_id']
        title = s['title']
        subagents = s['subagents']
        all_ids = [cid] + subagents
        id_map = {k: map_cid(k) for k in all_ids}

        sub_info = f" (+ {len(subagents)} subagents)" if subagents else ""
        print(f"[{idx}/{total_to_migrate}] Migrating \033[1m\"{s['clean_title']}\"\033[0m{sub_info} ...", end=" ", flush=True)

        try:
            migrate_single_session(
                cid, cli_dir, app_dir, target_project, hub=hub, title=title, id_map=id_map)
            for sub_id in subagents:
                migrate_single_session(
                    sub_id, cli_dir, app_dir, target_project, hub=hub, id_map=id_map)

            sync_session_summaries(s, cli_dir, app_dir,
                                   target_project, hub=hub, id_map=id_map)
            print("\033[1;32mDone\033[0m")
            success_count += 1
        except Exception as e:
            print(f"\033[1;31mFailed: {e}\033[0m")

    print(
        f"\n\033[1;32m========================================================================")
    print(
        f" Migration Complete: {success_count}/{total_to_migrate} sessions successfully migrated! ")
    print(
        f"========================================================================\033[0m\n")
    print(
        f"Target Project: \033[1m{target_project['name']}\033[0m ({target_project['id']})")
    print("Open any migrated session directly in Antigravity Desktop:")
    for s in selected_sessions:
        d_cid = s.get('desktop_cid') or map_cid(s['conversation_id'])
        print(f" - {s['title']}: conversation://{d_cid}")
    print(
        f"\n\033[90mAll original files in Antigravity CLI ({cli_dir}) remain 100% intact.\033[0m\n")


if __name__ == '__main__':
    try:
        run_migration()
    except KeyboardInterrupt:
        show_cursor()
        print("\n\nOperation cancelled by user.")
        sys.exit(130)
