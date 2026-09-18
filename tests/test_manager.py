import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from browser_lease.manager import Manager, Problem, conflicts, identity, normalize


def spec(root, task="task-a", **overrides):
    return {
        "task": task,
        "slot": task,
        "agent_pid": os.getpid(),
        "environment": "cn-test",
        "browser_data_dir": str(root / task / "profile"),
        "account": task,
        "course_run": task,
        "artifacts_dir": str(root / task / "artifacts"),
        "control_mode": "browser",
        "chrome_executable": shutil.which("false"),
        "headless": True,
        **overrides,
    }


def active(s):
    return {"spec": normalize(s), "status": "ready"}


@pytest.mark.parametrize(
    "key,value",
    [
        ("slot", "task-a"),
        ("account", "task-a"),
        ("course_run", "task-a"),
        ("environment_access", "exclusive"),
    ],
)
def test_exclusive_claims(tmp_path, key, value):
    a = active(spec(tmp_path))
    b = normalize(spec(tmp_path, "task-b", **{key: value}))
    assert conflicts(b, [a])


def test_independent_tasks_and_environment_scopes(tmp_path):
    a = active(spec(tmp_path))
    assert not conflicts(normalize(spec(tmp_path, "task-b")), [a])
    b = normalize(
        spec(tmp_path, "task-b", account="task-a", course_run="task-a", environment="prod")
    )
    assert not conflicts(b, [a])
    a["status"] = "stopped"
    assert not conflicts(a["spec"], [a])


def test_directories_overlap_across_types_and_symlinks(tmp_path):
    a = active(spec(tmp_path))
    parent = Path(a["spec"]["browser_data_dir"])
    parent.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    for path in (parent, parent / "sub", parent.parent, alias / "sub"):
        b = normalize(spec(tmp_path, "task-b", artifacts_dir=str(path)))
        assert "artifacts_dir:browser_data_dir" in conflicts(b, [a])[0]["fields"]


def test_desktop_exclusive_across_environments(tmp_path):
    a = active(spec(tmp_path))
    b = normalize(
        spec(tmp_path, "task-b", environment="prod", control_mode="desktop", headless=False)
    )
    assert "desktop" in conflicts(b, [a])[0]["fields"]
    assert "desktop" in conflicts(a["spec"], [{"spec": b, "status": "starting"}])[0]["fields"]


def test_resource_read_write_conflicts(tmp_path):
    resource = {"kind": "course", "id": "course-1", "access": "read"}
    a = active(spec(tmp_path, resources=[resource]))
    b = normalize(spec(tmp_path, "task-b", resources=[resource]))
    assert not conflicts(b, [a])
    b["resources"][0]["access"] = "write"
    assert "resource:course:course-1" in conflicts(b, [a])[0]["fields"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_pid": True},
        {"agent_pid": 1},
        {"headless": "false"},
        {"task": "../../x"},
        {"browser_data_dir": "/"},
        {"browser_data_dir": "relative/path"},
        {"control_mode": "anything"},
        {"resources": [{"kind": "course"}]},
        {"environment_access": "anything"},
        {"course_run": ""},
        {"token": "not-allowed"},
    ],
)
def test_invalid_contract(tmp_path, overrides):
    with pytest.raises(Problem):
        normalize(spec(tmp_path, **overrides))


def test_conflict_creates_no_directories_or_task(tmp_path):
    m = Manager(tmp_path / "state")
    row = active(spec(tmp_path))
    m.save(row, "fixture")
    b = spec(tmp_path, "task-b", account="task-a")
    with pytest.raises(Problem, match="资源被占用") as err:
        m.register(b)
    assert err.value.code == "resource_conflict"
    assert not Path(b["browser_data_dir"]).exists()
    assert len(m.rows()) == 1


def test_unmanaged_and_account_bound_directories(tmp_path):
    m = Manager(tmp_path / "state")
    s = normalize(spec(tmp_path))
    p = Path(s["browser_data_dir"])
    p.mkdir(parents=True)
    (p / "existing.txt").write_text("do not touch")
    with pytest.raises(Problem) as err:
        m.register(spec(tmp_path))
    assert err.value.code == "unmanaged_directory"
    assert (p / "existing.txt").read_text() == "do not touch"
    (p / "existing.txt").unlink()
    m.directory(s, "browser_data_dir", create=True)
    s["account"] = "other"
    with pytest.raises(Problem) as err:
        m.directory(s, "browser_data_dir")
    assert err.value.code == "directory_owner_conflict"


def test_failed_start_releases_claims_but_preserves_audit(tmp_path):
    m = Manager(tmp_path / "state", start_timeout=1)
    with pytest.raises(Problem):
        m.register(spec(tmp_path))
    row = m.get("task-a")
    assert row["status"] == "failed"
    assert row["connection"] is None
    assert [e["action"] for e in m.events("task-a")][-1] == "reserved"
    assert m.stop("task-a")["status"] == "failed"
    assert oct(m.db_path.stat().st_mode & 0o777) == "0o600"


def test_pid_reuse_not_live_and_cleanup_no_unrelated_kill(tmp_path):
    m = Manager(tmp_path / "state")
    s = normalize(spec(tmp_path))
    s["owner_identity"]["create_time"] -= 100
    row = {
        "spec": s,
        "status": "ready",
        "directories": {},
        "connection": None,
        "browser_identity": {
            "pid": os.getpid(),
            "create_time": identity(os.getpid())["create_time"] - 100,
        },
    }
    m.save(row, "fixture")
    assert m.view(row)["orphaned"] is True
    assert m.cleanup_orphans() == [{"task": "task-a", "status": "ready", "error": None}]
    assert m.get("task-a")["status"] == "ready"
    assert m.cleanup_orphans(apply=True)[0]["status"] == "stopped"
    assert identity(os.getpid()) is not None


def test_refuse_live_owner_takeover(tmp_path):
    m = Manager(tmp_path / "state")
    m.save(active(spec(tmp_path)), "fixture")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(Problem) as err:
            m.reconnect("task-a", child.pid)
        assert err.value.code == "owner_alive"
    finally:
        child.terminate()
        child.wait()


def test_profile_purge_preserves_evidence_and_checks_inode(tmp_path):
    m = Manager(tmp_path / "state")
    with pytest.raises(Problem):
        m.register(spec(tmp_path))
    row = m.get("task-a")
    evidence = Path(row["spec"]["artifacts_dir"]) / "evidence.txt"
    evidence.write_text("keep")
    profile = Path(row["spec"]["browser_data_dir"])
    original = profile.with_name("original")
    profile.rename(original)
    profile.mkdir()
    (profile / ".browser-lease-owner.json").write_text(
        (original / ".browser-lease-owner.json").read_text()
    )
    with pytest.raises(Problem) as err:
        m.purge_profile("task-a")
    assert err.value.code == "directory_changed"
    (profile / ".browser-lease-owner.json").unlink()
    profile.rmdir()
    original.rename(profile)
    assert m.purge_profile("task-a")["profile_purged"]
    assert not profile.exists()
    assert evidence.read_text() == "keep"


def reserve_worker(root, request, queue):
    m = Manager(Path(root))
    with m.lock():
        s = normalize(request)
        found = conflicts(s, m.rows())
        if found:
            queue.put("conflict")
        else:
            time.sleep(0.1)  # Widen the race between checking and inserting.
            m.save({"spec": s, "status": "starting"}, "reserved")
            queue.put("reserved")


def test_multiprocess_claim_race(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    requests = [spec(tmp_path, f"task-{i}", slot="shared-slot") for i in range(6)]
    procs = [
        ctx.Process(target=reserve_worker, args=(str(tmp_path / "state"), s, q)) for s in requests
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(10)
        assert p.exitcode == 0
    results = [q.get(timeout=1) for _ in procs]
    assert results.count("reserved") == 1
    assert results.count("conflict") == 5
    assert len(Manager(tmp_path / "state").rows()) == 1


def test_queries_available_during_lifecycle_lock(tmp_path):
    m = Manager(tmp_path / "state")
    with m.lock():
        m.save(active(spec(tmp_path)), "fixture")
        # Initialization and reads must not wait for the browser lifecycle lock.
        other = Manager(tmp_path / "state")
        assert other.get("task-a")["status"] == "ready"


def test_cli_json_errors_and_pagination(tmp_path, capsys):
    from browser_lease.cli import parser, run

    m = Manager(tmp_path / "state")
    for i in range(3):
        m.save(active(spec(tmp_path, f"task-{i}")), "fixture")
    result = run(parser().parse_args(["list", "--limit", "1", "--offset", "1"]), m)
    assert result["total"] == 3
    assert result["tasks"][0]["spec"]["task"] == "task-1"
    with pytest.raises(Problem):
        parser().parse_args(["register"])
    with pytest.raises(Problem):
        run(parser().parse_args(["list", "--limit", "1000"]), m)
    bad = tmp_path / "bad.json"
    bad.write_text("{bad")
    with pytest.raises(Problem) as err:
        run(parser().parse_args(["register", "--file", str(bad)]), m)
    assert err.value.code == "invalid_json"


def test_agent_process_walks_parent_chain():
    from browser_lease.manager import agent_process

    me = psutil.Process()
    found = agent_process(start=os.getpid(), names=frozenset({me.name().lower()}))
    assert found["agent"]["pid"] == os.getpid()
    assert found["agent"]["create_time"] == identity(os.getpid())["create_time"]
    assert found["chain"][0]["pid"] == os.getpid()


def test_agent_process_without_known_agent():
    from browser_lease.manager import agent_process

    found = agent_process(start=os.getpid(), names=frozenset({"no-such-agent"}))
    assert found["agent"] is None
    assert found["chain"]
