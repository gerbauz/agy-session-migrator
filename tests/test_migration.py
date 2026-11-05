import os
import sys
import json
import sqlite3
import pytest
from unittest.mock import patch

from migrate_agy_sessions import (
    TO_DESKTOP,
    CLI_SOURCE_ENUM,
    CLI_SOURCE_NAME,
    get_desktop_cid,
    map_cid,
    direction_label,
    check_installations,
    discover_projects,
    discover_sessions,
    prune_ghost_sessions,
    migrate_single_session,
    sync_session_summaries,
    encode_varint,
    parse_fields,
    encode_field,
    serialize_fields,
    clean_and_update_raw_summary,
    replace_uuids_in_bytes,
    prompt_desktop_not_running,
    select_project_screen,
    select_sessions_screen,
    file_uri_to_path,
    normalize_path,
    PlatformProfile,
    PLATFORM,
    UNIX_ESCAPE_SEQUENCES,
    _posix_get_key,
)

# ---------------------------------------------------------------------------
# UUID Mapping & Direction Tests
# ---------------------------------------------------------------------------

def test_uuid_mapping_determinism():
    cid = "01234567-89ab-cdef-0123-456789abcdef"
    d_cid1 = get_desktop_cid(cid)
    d_cid2 = get_desktop_cid(cid)
    assert d_cid1 == d_cid2
    assert len(d_cid1) == 36
    assert d_cid1 != cid
    assert map_cid(cid) == d_cid1

def test_direction_label():
    assert "CLI -> Antigravity Desktop" in direction_label(TO_DESKTOP)

# ---------------------------------------------------------------------------
# Installation Checker Tests
# ---------------------------------------------------------------------------

def test_check_installations_missing_all(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cfg_dir = tmp_path / "cfg"

    problems = check_installations(str(cli_dir), str(app_dir), str(cfg_dir))
    assert len(problems) >= 3
    assert any("Antigravity CLI not found" in p for p in problems)
    assert any("Antigravity Desktop not found" in p for p in problems)
    assert any("No Antigravity projects found" in p for p in problems)

def test_check_installations_complete(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cfg_dir = tmp_path / "cfg"

    cli_dir.mkdir()
    app_dir.mkdir()
    (cfg_dir / "projects").mkdir(parents=True)

    # Add databases
    sqlite3.connect(cli_dir / "conversation_summaries.db").close()
    sqlite3.connect(app_dir / "conversation_summaries.db").close()

    # Add dummy project
    proj_file = cfg_dir / "projects" / "test-proj.json"
    proj_file.write_text(json.dumps({
        "id": "test-proj-id",
        "name": "Test Project",
        "projectResources": {"resources": [{"gitFolder": {"folderUri": "file:///path/to/repo"}}]}
    }), encoding="utf-8")

    problems = check_installations(str(cli_dir), str(app_dir), str(cfg_dir))
    assert problems == []

# ---------------------------------------------------------------------------
# Protobuf Varint and Field Serialization Tests
# ---------------------------------------------------------------------------

def test_protobuf_helpers():
    # Encode and parse fields
    num_val = 12345
    str_val = b"test string"
    raw = serialize_fields([(1, 0, num_val), (2, 2, str_val)])

    parsed = parse_fields(raw)
    assert len(parsed) == 2
    assert parsed[0] == (1, 0, num_val)
    assert parsed[1] == (2, 2, str_val)

def test_clean_and_update_raw_summary():
    # Create raw summary with source enum = 17 (CLI) and project_id
    f_title = (1, 2, b"Old Title")
    f_source = (20, 0, CLI_SOURCE_ENUM)
    f_meta = (17, 2, serialize_fields([(18, 2, b"old-project-id")]))
    orig_summary = serialize_fields([f_title, f_source, f_meta])

    cleaned = clean_and_update_raw_summary(
        orig_summary,
        title="New Title",
        project_id="new-project-id",
        folder_uri="file:///new/path"
    )

    parsed = parse_fields(cleaned)
    field_map = {f[0]: f for f in parsed}

    # Summary text updated
    assert field_map[1][2] == b"New Title"
    # Source enum (field 20) stripped for Desktop
    assert 20 not in field_map
    # TrajectoryMetadata updated
    meta_parsed = parse_fields(field_map[17][2])
    meta_dict = {f[0]: f[2] for f in meta_parsed}
    assert meta_dict[18] == b"new-project-id"
    assert meta_dict[7] == b"file:///new/path"

def test_replace_uuids_in_bytes():
    old_id = "11111111-1111-1111-1111-111111111111"
    new_id = "22222222-2222-2222-2222-222222222222"
    id_map = {old_id.encode('utf-8'): new_id.encode('utf-8')}

    data = f"hello {old_id} world".encode('utf-8')
    res = replace_uuids_in_bytes(data, id_map)
    assert res == f"hello {new_id} world".encode('utf-8')

# ---------------------------------------------------------------------------
# Session Discovery Tests
# ---------------------------------------------------------------------------

def _create_mock_summaries_db(db_path, sessions):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute('''
        CREATE TABLE conversation_summaries (
            conversation_id text PRIMARY KEY,
            title text NOT NULL DEFAULT "",
            preview text NOT NULL DEFAULT "",
            step_count integer NOT NULL DEFAULT 0,
            last_modified_time datetime NOT NULL,
            workspace_uris text NOT NULL,
            status text NOT NULL DEFAULT "",
            source text NOT NULL DEFAULT "",
            project_id text NOT NULL DEFAULT "",
            agent_name text NOT NULL DEFAULT "",
            parent_conversation_id text NOT NULL DEFAULT "",
            nesting_depth integer NOT NULL DEFAULT 0,
            last_user_input_time datetime,
            last_user_input_step_index integer DEFAULT -1,
            raw_summary blob,
            app_data_dir text NOT NULL DEFAULT ""
        )
    ''')
    for s in sessions:
        cur.execute('''
            INSERT INTO conversation_summaries (
                conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, parent_conversation_id, nesting_depth,
                raw_summary, app_data_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            s.get("conversation_id", "cid"),
            s.get("title", ""),
            s.get("preview", ""),
            s.get("step_count", 1),
            s.get("last_modified_time", "2026-11-05 12:00:00"),
            s.get("workspace_uris", "[]"),
            s.get("status", "CASCADE_RUN_STATUS_IDLE"),
            s.get("source", ""),
            s.get("project_id", ""),
            s.get("parent_conversation_id", ""),
            s.get("nesting_depth", 0),
            s.get("raw_summary", None),
            s.get("app_data_dir", ""),
        ))
    con.commit()
    con.close()

def test_discover_sessions_filtering_ghosts_and_mirrors(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"

    sessions = [
        # Valid root session
        {
            "conversation_id": "c1",
            "title": "Valid CLI Session",
            "step_count": 5,
            "last_modified_time": "2026-11-05 10:00:00",
            "project_id": "p1"
        },
        # Ghost / uninitialized session (0 steps, no title/preview)
        {
            "conversation_id": "c2",
            "title": "",
            "preview": "",
            "step_count": 0,
            "last_modified_time": "2026-11-05 10:00:00"
        },
        # Corrupt 0001 year session
        {
            "conversation_id": "c3",
            "title": "Corrupt Date",
            "step_count": 2,
            "last_modified_time": "0001-01-01 00:00:00"
        },
        # CLI watcher mirror inside Desktop store (should be filtered)
        {
            "conversation_id": "c4",
            "title": "CLI Mirror",
            "step_count": 2,
            "source": CLI_SOURCE_NAME,
            "last_modified_time": "2026-11-05 10:00:00"
        },
        # Child subagent session (parent = c1)
        {
            "conversation_id": "c5",
            "title": "Subagent Task",
            "step_count": 2,
            "parent_conversation_id": "c1",
            "nesting_depth": 1,
            "last_modified_time": "2026-11-05 10:05:00"
        }
    ]

    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", sessions)
    _create_mock_summaries_db(app_dir / "conversation_summaries.db", [])

    discovered = discover_sessions(str(cli_dir), str(app_dir), "p1")
    assert len(discovered) == 1
    assert discovered[0]['conversation_id'] == "c1"
    assert discovered[0]['clean_title'] == "Valid CLI Session"
    assert discovered[0]['subagents'] == ["c5"]

def test_discover_sessions_defensive_checks(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cli_dir.mkdir()
    app_dir.mkdir()
    (cli_dir / "conversations").mkdir()

    # c1 has physical file
    (cli_dir / "conversations" / "c1.db").touch()
    # c2 has physical file but app_data_dir is 'antigravity' (Desktop mirror)
    (cli_dir / "conversations" / "c2.db").touch()
    # c3 has NO physical file

    sessions = [
        {
            "conversation_id": "c1",
            "title": "Valid CLI Session",
            "step_count": 3,
            "last_modified_time": "2026-11-05 10:00:00",
            "project_id": "p1",
            "app_data_dir": "antigravity-cli",
        },
        {
            "conversation_id": "c2",
            "title": "Desktop Mirror in CLI DB",
            "step_count": 3,
            "last_modified_time": "2026-11-05 10:00:00",
            "project_id": "p1",
            "app_data_dir": "antigravity",
        },
        {
            "conversation_id": "c3",
            "title": "Missing File Session",
            "step_count": 3,
            "last_modified_time": "2026-11-05 10:00:00",
            "project_id": "p1",
            "app_data_dir": "antigravity-cli",
        },
    ]

    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", sessions)
    _create_mock_summaries_db(app_dir / "conversation_summaries.db", [])

    discovered = discover_sessions(str(cli_dir), str(app_dir), "p1")
    assert len(discovered) == 1
    assert discovered[0]['conversation_id'] == "c1"

def test_prune_ghost_sessions_safety(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cli_dir.mkdir()
    app_dir.mkdir()
    (cli_dir / "conversations").mkdir()
    (app_dir / "conversations").mkdir()

    # Create trajectory file for legit CLI session
    (cli_dir / "conversations" / "cli-real.db").touch()
    # Create trajectory file for ghost in app_dir
    (app_dir / "conversations" / "ghost-mirror.db").touch()
    # Create trajectory file for edge case: file exists in CLI despite app_data_dir = 'antigravity'
    (cli_dir / "conversations" / "safe-file-exists.db").touch()

    sessions = [
        # 1. Genuine CLI session (has CLI file, app_data_dir='antigravity-cli') -> KEEP
        {
            "conversation_id": "cli-real",
            "title": "Real CLI Session",
            "app_data_dir": "antigravity-cli",
            "raw_summary": b"payload",
            "project_id": "p1",
        },
        # 2. Ghost mirror (NO CLI file, app_data_dir='antigravity', file exists in Desktop) -> PRUNE
        {
            "conversation_id": "ghost-mirror",
            "title": "Desktop Mirror",
            "app_data_dir": "antigravity",
            "raw_summary": b"preview",
            "project_id": "p1",
        },
        # 3. Ghost empty preview (NO CLI file, app_data_dir='antigravity', raw_summary=None) -> PRUNE
        {
            "conversation_id": "ghost-empty",
            "title": "Ghost Empty",
            "app_data_dir": "antigravity",
            "raw_summary": None,
            "project_id": "p1",
        },
        # 4. Safety rule 1: Has CLI file on disk -> NEVER prune even if app_data_dir='antigravity' -> KEEP
        {
            "conversation_id": "safe-file-exists",
            "title": "Safe File Exists",
            "app_data_dir": "antigravity",
            "raw_summary": b"",
            "project_id": "p1",
        },
        # 5. Safety rule 2: Missing CLI file BUT app_data_dir='antigravity-cli' -> KEEP
        {
            "conversation_id": "safe-cli-appdir",
            "title": "Safe CLI App Dir",
            "app_data_dir": "antigravity-cli",
            "raw_summary": b"payload",
            "project_id": "p1",
        },
    ]

    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", sessions)

    pruned = prune_ghost_sessions(str(cli_dir), str(app_dir), project={"id": "p1"})
    assert pruned == 2

    # Verify rows in DB
    con = sqlite3.connect(cli_dir / "conversation_summaries.db")
    remaining = {r[0] for r in con.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
    con.close()

    assert "cli-real" in remaining
    assert "safe-file-exists" in remaining
    assert "safe-cli-appdir" in remaining
    assert "ghost-mirror" not in remaining
    assert "ghost-empty" not in remaining

def test_prune_ghost_sessions_project_scoping(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cli_dir.mkdir()
    app_dir.mkdir()
    (cli_dir / "conversations").mkdir()
    (app_dir / "conversations").mkdir()

    sessions = [
        # Ghost in Project A
        {
            "conversation_id": "ghost-a",
            "title": "Ghost A",
            "app_data_dir": "antigravity",
            "raw_summary": None,
            "project_id": "proj-a",
        },
        # Ghost in Project B
        {
            "conversation_id": "ghost-b",
            "title": "Ghost B",
            "app_data_dir": "antigravity",
            "raw_summary": None,
            "project_id": "proj-b",
        },
    ]

    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", sessions)

    # Prune scoped only to Project A
    pruned = prune_ghost_sessions(str(cli_dir), str(app_dir), project={"id": "proj-a"})
    assert pruned == 1

    con = sqlite3.connect(cli_dir / "conversation_summaries.db")
    remaining = {r[0] for r in con.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
    con.close()

    assert "ghost-a" not in remaining
    assert "ghost-b" in remaining

# ---------------------------------------------------------------------------
# Migration Logic Tests (CLI -> Desktop)
# ---------------------------------------------------------------------------

def _setup_mock_conversation_db(db_path, cid, source=CLI_SOURCE_ENUM):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("CREATE TABLE trajectory_meta (cascade_id text, source integer)")
    cur.execute("INSERT INTO trajectory_meta VALUES (?, ?)", (cid, source))
    cur.execute("CREATE TABLE trajectory_metadata_blob (id text PRIMARY KEY, data blob)")
    cur.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (b'',))
    cur.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, has_subtrajectory integer, metadata blob, step_payload blob, step_format integer)")
    con.commit()
    con.close()

def test_migrate_cli_to_desktop_sqlite_and_files(tmp_path):
    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cli_dir.mkdir()
    app_dir.mkdir()

    cid = "12345678-1234-5678-1234-567812345678"
    src_db = cli_dir / "conversations" / f"{cid}.db"
    _setup_mock_conversation_db(src_db, cid, source=CLI_SOURCE_ENUM)
    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", [
        {"conversation_id": cid, "title": "CLI to Desktop Migration"}
    ])
    _create_mock_summaries_db(app_dir / "conversation_summaries.db", [])

    target_project = {
        "id": "target-p-id",
        "name": "Target Project",
        "folderUri": "file:///target/workspace",
        "readablePath": "C:/target/workspace"
    }

    d_cid = get_desktop_cid(cid)
    id_map = {cid: d_cid}
    session = {
        "conversation_id": cid,
        "title": "CLI to Desktop Migration",
        "preview": "CLI to Desktop Preview",
        "step_count": 5,
        "workspace_uris": '["file:///target/workspace"]'
    }

    migrate_single_session(cid, str(cli_dir), str(app_dir), target_project, id_map=id_map)
    sync_session_summaries(session, str(cli_dir), str(app_dir), target_project, id_map=id_map)

    # Verify destination conversation DB exists and has source = 1 (Desktop)
    dst_db = app_dir / "conversations" / f"{d_cid}.db"
    assert dst_db.exists()
    con = sqlite3.connect(dst_db)
    meta = con.execute("SELECT cascade_id, source FROM trajectory_meta").fetchone()
    assert meta[0] == d_cid
    assert meta[1] == 1  # 1 for Desktop
    con.close()

    # Verify summary in app_dir
    con = sqlite3.connect(app_dir / "conversation_summaries.db")
    row = con.execute("SELECT conversation_id, project_id, title FROM conversation_summaries WHERE conversation_id = ?", (d_cid,)).fetchone()
    assert row is not None
    assert row[0] == d_cid
    assert row[1] == "target-p-id"
    assert row[2] == "CLI to Desktop Migration"
    con.close()

# ---------------------------------------------------------------------------
# UI Navigation & ESC/Back Key Handling Tests
# ---------------------------------------------------------------------------

def test_prompt_desktop_not_running_keys(capsys):
    # ENTER returns RETRY
    with patch("migrate_agy_sessions.get_key", return_value="ENTER"):
        res = prompt_desktop_not_running()
        assert res == "RETRY"

    # ESC returns QUIT
    with patch("migrate_agy_sessions.get_key", return_value="ESC"):
        res = prompt_desktop_not_running()
        assert res == "QUIT"

    # 'q' returns QUIT
    with patch("migrate_agy_sessions.get_key", return_value="q"):
        res = prompt_desktop_not_running()
        assert res == "QUIT"

    captured = capsys.readouterr()
    assert "Antigravity Desktop Is Not Running" in captured.out
    assert "Retry connection" in captured.out
    assert "Quit application" in captured.out

def test_select_project_screen_navigation():
    projects = [
        {"id": "p1", "name": "Project 1", "readablePath": "/p1", "session_count": 2},
        {"id": "p2", "name": "Project 2", "readablePath": "/p2", "session_count": 0}
    ]

    # ESC returns QUIT (since Step 1 is the initial screen)
    with patch("migrate_agy_sessions.get_key", return_value="ESC"):
        res, idx = select_project_screen(projects, initial_idx=1)
        assert res == "QUIT"

    # 'q' returns QUIT
    with patch("migrate_agy_sessions.get_key", return_value="q"):
        res, idx = select_project_screen(projects, initial_idx=1)
        assert res == "QUIT"

    # Row 0 (< Exit Application) on ENTER returns QUIT
    with patch("migrate_agy_sessions.get_key", side_effect=["ENTER"]):
        res, idx = select_project_screen(projects, initial_idx=0)
        assert res == "QUIT"

    # Selecting Project 1 (Row 1)
    with patch("migrate_agy_sessions.get_key", side_effect=["ENTER"]):
        res, idx = select_project_screen(projects, initial_idx=1)
        assert res == projects[0]

def test_select_sessions_screen_navigation():
    sessions = [
        {"conversation_id": "c1", "clean_title": "Session 1", "subagents": [], "step_count": 3, "date": "2026-11-05", "status": "New"}
    ]
    project = {"id": "p1", "name": "Project 1"}

    # ESC returns BACK (to return to Step 1: Projects)
    with patch("migrate_agy_sessions.get_key", return_value="ESC"):
        res = select_sessions_screen(sessions, project)
        assert res == "BACK"

    # 'b' key returns BACK
    with patch("migrate_agy_sessions.get_key", return_value="b"):
        res = select_sessions_screen(sessions, project)
        assert res == "BACK"

    # Row 0 (< Back to Project Selection) on ENTER returns BACK
    with patch("migrate_agy_sessions.get_key", side_effect=["UP", "ENTER"]):
        res = select_sessions_screen(sessions, project)
        assert res == "BACK"

    # 'q' returns QUIT
    with patch("migrate_agy_sessions.get_key", return_value="q"):
        res = select_sessions_screen(sessions, project)
        assert res == "QUIT"

# ---------------------------------------------------------------------------
# Project Discovery & Matching Tests
# ---------------------------------------------------------------------------

def test_discover_projects_filtering(tmp_path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)

    # Valid project
    (projects_dir / "valid.json").write_text(json.dumps({
        "id": "proj-1",
        "name": "Alpha Project",
        "projectResources": {"resources": [{"gitFolder": {"folderUri": "file:///c%3A/Users/test/alpha"}}]}
    }), encoding="utf-8")

    # Sentinel files to be skipped
    (projects_dir / "default-cli-project.json").write_text("{}", encoding="utf-8")
    (projects_dir / "outside-of-project.json").write_text("{}", encoding="utf-8")
    (projects_dir / "not_json.txt").write_text("hello", encoding="utf-8")

    discovered = discover_projects(str(tmp_path))
    assert len(discovered) == 1
    assert discovered[0]['id'] == "proj-1"
    assert discovered[0]['name'] == "Alpha Project"

def test_is_session_for_project():
    from migrate_agy_sessions import is_session_for_project

    project = {
        "id": "proj-xyz",
        "name": "XYZ",
        "readablePath": "c:/work/xyz",
        "folderUri": "file:///c:/work/xyz"
    }

    # Matches by project_id
    assert is_session_for_project({"project_id": "proj-xyz"}, project) is True
    # Matches by workspace_uris
    assert is_session_for_project({"workspace_uris": ["file:///c:/work/xyz/subdir"]}, project) is True
    # Doesn't match different project
    assert is_session_for_project({"project_id": "other-id", "workspace_uris": ["file:///c:/other"]}, project) is False

def test_argparse_flags_and_help():
    import subprocess
    result = subprocess.run([sys.executable, "migrate_agy_sessions.py", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    # Old non-interactive flags must be gone
    assert "--project" not in result.stdout
    assert "--all" not in result.stdout
    assert "--list" not in result.stdout
    # Directory overrides must be present
    assert "--cli-dir" in result.stdout
    assert "--app-dir" in result.stdout
    assert "--config-dir" in result.stdout

def test_startup_missing_installations_error(tmp_path):
    import subprocess
    fake_cli = tmp_path / "nonexistent_cli"
    fake_app = tmp_path / "nonexistent_app"
    result = subprocess.run([
        sys.executable, "migrate_agy_sessions.py",
        "--cli-dir", str(fake_cli),
        "--app-dir", str(fake_app),
        "--config-dir", str(tmp_path)
    ], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Missing Installation" in result.stdout
    assert "Both Antigravity CLI and Antigravity Desktop must be installed" in result.stdout

def test_detect_live_hub_stale_files_fast(tmp_path):
    from migrate_agy_sessions import detect_live_hub
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    # Create 20 dead/stale hub files
    for i in range(20):
        (daemon_dir / f"hub_{i+1000}.json").write_text(json.dumps({
            "pid": 999990 + i,
            "httpPort": 1000 + i,
            "csrfToken": "test"
        }), encoding="utf-8")
    
    import time
    t0 = time.time()
    res = detect_live_hub(str(tmp_path))
    duration = time.time() - t0
    assert res is None
    # Must finish virtually instantly without hanging
    assert duration < 0.5

def test_is_pid_running():
    from migrate_agy_sessions import is_pid_running
    # Current process should be alive
    assert is_pid_running(os.getpid()) is True
    # Nonexistent PID should be dead
    assert is_pid_running(999999) is False
    assert is_pid_running(0) is False
    assert is_pid_running(-1) is False

def test_run_migration_desktop_offline_quit(tmp_path, monkeypatch):
    from migrate_agy_sessions import run_migration

    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cfg_dir = tmp_path / "cfg"
    cli_dir.mkdir()
    app_dir.mkdir()
    (cfg_dir / "projects").mkdir(parents=True)
    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", [])
    _create_mock_summaries_db(app_dir / "conversation_summaries.db", [])
    (cfg_dir / "projects" / "p1.json").write_text(json.dumps({"id": "p1", "name": "P1"}), encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "migrate_agy_sessions.py",
        "--cli-dir", str(cli_dir),
        "--app-dir", str(app_dir),
        "--config-dir", str(cfg_dir)
    ])

    # Desktop is offline, user presses 'q' to quit
    with patch("migrate_agy_sessions.detect_live_hub", return_value=None):
        with patch("migrate_agy_sessions.get_key", return_value="q"):
            with pytest.raises(SystemExit) as exc:
                run_migration()
            assert exc.value.code == 0

def test_run_migration_desktop_retry_and_proceed(tmp_path, monkeypatch):
    from migrate_agy_sessions import run_migration

    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cfg_dir = tmp_path / "cfg"
    cli_dir.mkdir()
    app_dir.mkdir()
    (cfg_dir / "projects").mkdir(parents=True)
    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", [])
    _create_mock_summaries_db(app_dir / "conversation_summaries.db", [])
    (cfg_dir / "projects" / "p1.json").write_text(json.dumps({
        "id": "p1",
        "name": "P1",
        "projectResources": {"resources": []}
    }), encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "migrate_agy_sessions.py",
        "--cli-dir", str(cli_dir),
        "--app-dir", str(app_dir),
        "--config-dir", str(cfg_dir)
    ])

    # First call: None (offline) -> user presses ENTER
    # Second call: returns live hub -> proceeds to Step 1 (Projects), user presses ESC to exit
    mock_hub = {"httpPort": 12345, "csrfToken": "token", "pid": 1111}
    with patch("migrate_agy_sessions.detect_live_hub", side_effect=[None, mock_hub, mock_hub]):
        with patch("migrate_agy_sessions.get_key", side_effect=["ENTER", "ESC"]):
            with pytest.raises(SystemExit) as exc:
                run_migration()
            assert exc.value.code == 0

def test_select_project_screen_status_message(capsys):
    projects = [
        {"id": "p1", "name": "Project 1", "readablePath": "/p1", "session_count": 0}
    ]
    # Verify status_message is printed under the border line
    with patch("migrate_agy_sessions.get_key", return_value="ESC"):
        res, idx = select_project_screen(
            projects, initial_idx=1, status_message="[!] Project 'Project 1' has no sessions to migrate."
        )
        assert res == "QUIT"

    captured = capsys.readouterr()
    assert "[!] Project 'Project 1' has no sessions to migrate." in captured.out

def test_select_sessions_screen_filter_not_leaking_other_projects(capsys):
    sessions = [
        {"conversation_id": "c_other", "clean_title": "Other Project Session", "subagents": [], "step_count": 3, "date": "2026-11-05", "status": "New", "project_id": "p_other"}
    ]
    target_project = {"id": "p_target", "name": "Target Project", "readablePath": "/target"}

    # User immediately presses ESC to go BACK
    with patch("migrate_agy_sessions.get_key", return_value="ESC"):
        res = select_sessions_screen(sessions, target_project)
        assert res == "BACK"

    captured = capsys.readouterr()
    # Filter is active by default, so 'Other Project Session' should NOT be displayed
    assert "Other Project Session" not in captured.out
    assert "(0 sessions)" in captured.out

def test_run_migration_zero_sessions_after_prune_prompts_user(tmp_path, monkeypatch):
    from migrate_agy_sessions import run_migration

    cli_dir = tmp_path / "cli"
    app_dir = tmp_path / "app"
    cfg_dir = tmp_path / "cfg"
    cli_dir.mkdir()
    app_dir.mkdir()
    (cli_dir / "conversations").mkdir()
    (app_dir / "conversations").mkdir()
    (cfg_dir / "projects").mkdir(parents=True)

    # Ghost session: in CLI summary db, but has NO trajectory file in cli_dir/conversations!
    (app_dir / "conversations" / "ghost-1.db").touch()

    _create_mock_summaries_db(cli_dir / "conversation_summaries.db", [
        {
            "conversation_id": "ghost-1",
            "title": "Ghost Session",
            "app_data_dir": "antigravity",
            "project_id": "p1",
            "raw_summary": b"preview",
        }
    ])
    _create_mock_summaries_db(app_dir / "conversation_summaries.db", [])

    (cfg_dir / "projects" / "p1.json").write_text(json.dumps({
        "id": "p1",
        "name": "Project One",
        "projectResources": {"resources": []}
    }), encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "migrate_agy_sessions.py",
        "--cli-dir", str(cli_dir),
        "--app-dir", str(app_dir),
        "--config-dir", str(cfg_dir)
    ])

    mock_hub = {"httpPort": 12345, "csrfToken": "token", "pid": 1111}
    # User selects Project One (ENTER), prune runs, count becomes 0,
    # prompt is shown in Step 1, then user presses ESC to exit
    with patch("migrate_agy_sessions.detect_live_hub", return_value=mock_hub):
        with patch("migrate_agy_sessions.get_key", side_effect=["ENTER", "ESC"]):
            with patch("migrate_agy_sessions.select_sessions_screen") as mock_step2:
                with pytest.raises(SystemExit) as exc:
                    run_migration()
                assert exc.value.code == 0
                # Step 2 should NEVER have been called!
                assert not mock_step2.called

# ---------------------------------------------------------------------------
# Cross-Platform Tests (Windows / Linux / macOS)
# ---------------------------------------------------------------------------

def test_platform_profiles_configuration():
    win = PlatformProfile('win32')
    assert win.is_windows is True
    assert win.is_macos is False
    assert win.is_linux is False
    assert win.case_sensitive_paths is False
    assert 'agy.exe' in win.cli_process_names

    mac = PlatformProfile('darwin')
    assert mac.is_windows is False
    assert mac.is_macos is True
    assert mac.is_linux is False
    assert mac.case_sensitive_paths is False
    assert 'agy' in mac.cli_process_names

    linux = PlatformProfile('linux')
    assert linux.is_windows is False
    assert linux.is_macos is False
    assert linux.is_linux is True
    assert linux.case_sensitive_paths is True
    assert 'agy' in linux.cli_process_names


def test_file_uri_to_path_cross_platform():
    # Windows drive letter URI
    assert file_uri_to_path("file:///C:/Users/Eliza/repo") == "C:/Users/Eliza/repo"
    assert file_uri_to_path("file:///c%3A/Users/Eliza/repo") == "c:/Users/Eliza/repo"

    # Unix absolute paths (must retain leading slash!)
    assert file_uri_to_path("file:///home/eliza/repo") == "/home/eliza/repo"
    assert file_uri_to_path("file:///Users/eliza/repo") == "/Users/eliza/repo"
    assert file_uri_to_path("file:///var/log") == "/var/log"

    # Bare paths pass through untouched
    assert file_uri_to_path("/home/eliza/repo") == "/home/eliza/repo"
    assert file_uri_to_path("C:\\Users\\Eliza\\repo") == "C:\\Users\\Eliza\\repo"
    assert file_uri_to_path("") == ""


def test_normalize_path_case_sensitivity():
    # Under Linux (case sensitive)
    with patch.object(PLATFORM, 'case_sensitive_paths', True):
        assert normalize_path("/Home/Eliza/MyRepo/") == "/Home/Eliza/MyRepo"
        assert normalize_path("file:///Home/Eliza/MyRepo") == "/Home/Eliza/MyRepo"

    # Under Windows / macOS (case insensitive)
    with patch.object(PLATFORM, 'case_sensitive_paths', False):
        assert normalize_path("/Home/Eliza/MyRepo/") == "/home/eliza/myrepo"
        assert normalize_path("C:\\Users\\Eliza\\MyRepo\\") == "c:/users/eliza/myrepo"
        assert normalize_path("file:///C:/Users/Eliza/MyRepo") == "c:/users/eliza/myrepo"


def test_discover_projects_posix_uri_keeps_leading_slash(tmp_path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)

    # Project on Linux/macOS path
    (projects_dir / "posix-proj.json").write_text(json.dumps({
        "id": "proj-posix",
        "name": "Posix Project",
        "projectResources": {"resources": [{"gitFolder": {"folderUri": "file:///home/eliza/workspace/project"}}]}
    }), encoding="utf-8")

    discovered = discover_projects(str(tmp_path))
    assert len(discovered) == 1
    assert discovered[0]['id'] == "proj-posix"
    assert discovered[0]['readablePath'] == "/home/eliza/workspace/project"


def test_detect_live_hub_skips_cli_on_all_platforms(tmp_path):
    from migrate_agy_sessions import detect_live_hub

    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()

    # Hub 1: CLI daemon on Linux/macOS (/usr/local/bin/agy) -> PID 2001
    (daemon_dir / "hub_2001.json").write_text(json.dumps({
        "pid": 2001,
        "httpPort": 4001,
        "csrfToken": "token1"
    }))
    # Hub 2: CLI daemon on Windows (C:\bin\agy.exe) -> PID 2002
    (daemon_dir / "hub_2002.json").write_text(json.dumps({
        "pid": 2002,
        "httpPort": 4002,
        "csrfToken": "token2"
    }))
    # Hub 3: Desktop Language Server (/path/to/language_server) -> PID 2003
    (daemon_dir / "hub_2003.json").write_text(json.dumps({
        "pid": 2003,
        "httpPort": 4003,
        "csrfToken": "token3"
    }))

    def fake_get_process_name(pid):
        if pid == 2001:
            return "/usr/local/bin/agy"
        if pid == 2002:
            return "C:\\Program Files\\Antigravity\\agy.exe"
        if pid == 2003:
            return "/opt/antigravity/language_server"
        return ""

    with patch("migrate_agy_sessions.is_pid_running", return_value=True), \
         patch("migrate_agy_sessions.is_port_listening", return_value=True), \
         patch("migrate_agy_sessions.get_process_name", side_effect=fake_get_process_name), \
         patch("urllib.request.build_opener") as mock_opener:

        mock_resp = mock_opener.return_value.open.return_value.__enter__.return_value
        mock_resp.status = 200

        live_hub = detect_live_hub(str(tmp_path))
        assert live_hub is not None
        assert live_hub['pid'] == 2003
        assert live_hub['httpPort'] == 4003


def test_posix_get_key_non_tty():
    with patch("sys.stdin.isatty", return_value=False):
        with patch("sys.stdin.readline", return_value=""):
            assert _posix_get_key() == "QUIT"


def test_posix_get_key_escape_sequences():
    from unittest.mock import MagicMock
    mock_termios = MagicMock()
    mock_tty = MagicMock()

    with patch.dict("sys.modules", {"termios": mock_termios, "tty": mock_tty}), \
         patch("sys.stdin.isatty", return_value=True), \
         patch.object(sys.stdin, "fileno", return_value=0, create=True):

        # Test UP arrow: '\x1b[A'
        with patch("sys.stdin.read", side_effect=['\x1b', '[', 'A']), \
             patch("select.select", side_effect=[([sys.stdin], [], []), ([sys.stdin], [], [])]):
            assert _posix_get_key() == "UP"

        # Test Home: '\x1bOH'
        with patch("sys.stdin.read", side_effect=['\x1b', 'O', 'H']), \
             patch("select.select", side_effect=[([sys.stdin], [], []), ([sys.stdin], [], [])]):
            assert _posix_get_key() == "HOME"

        # Test Page Up: '\x1b[5~'
        with patch("sys.stdin.read", side_effect=['\x1b', '[', '5', '~']), \
             patch("select.select", side_effect=[([sys.stdin], [], []), ([sys.stdin], [], []), ([sys.stdin], [], [])]):
            assert _posix_get_key() == "PAGE_UP"

        # Test Standalone ESC (select times out)
        with patch("sys.stdin.read", side_effect=['\x1b']), \
             patch("select.select", return_value=([], [], [])):
            assert _posix_get_key() == "ESC"

