import functools
import http.server
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import websocket
from test_manager import spec

from browser_lease.manager import Manager, Problem, chrome_executable, identity


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Page:
    def __init__(self, connection):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(connection["cdp_url"] + "/json/list", timeout=2) as r:
            pages = [p for p in json.load(r) if p["type"] == "page"]
        self.ws = websocket.create_connection(
            pages[0]["webSocketDebuggerUrl"], suppress_origin=True, timeout=5
        )
        self.seq = 0

    def call(self, method, params=None):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        while True:
            result = json.loads(self.ws.recv())
            if result.get("id") == self.seq:
                assert "error" not in result, result
                return result.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def navigate(self, url):
        self.call("Page.navigate", {"url": url})
        for _ in range(50):
            if self.evaluate("document.title") == "QA isolation":
                return
            time.sleep(0.05)
        pytest.fail("Local page did not load")

    def close(self):
        self.ws.close()


@pytest.mark.chrome
def test_real_chrome_isolation_reconnect_cleanup(tmp_path):
    exe = chrome_executable()  # Fail visibly if Chrome is missing; do not silently skip acceptance.
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text(
        "<title>QA isolation</title><h1>Local test</h1>"
        '<a id="download" href="sample.txt" download="sample.txt">Download</a>'
    )
    (web_root / "sample.txt").write_text("local download evidence")
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(web_root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    m = Manager(tmp_path / "state")
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pages = []
    try:
        sa = spec(tmp_path, "a", chrome_executable=exe, agent_pid=owner.pid)
        sb = spec(tmp_path, "b", chrome_executable=exe)
        a = m.register(sa)
        b = m.register(sb)
        assert a["status"] == b["status"] == "ready"
        assert a["browser_identity"] != b["browser_identity"]
        assert a["connection"]["cdp_url"] != b["connection"]["cdp_url"]
        assert m.register(sb)["browser_identity"] == b["browser_identity"]
        pa, pb = Page(a["connection"]), Page(b["connection"])
        pages.extend([pa, pb])
        pa.navigate(url)
        pb.navigate(url)
        pa.evaluate(
            "document.cookie='qa_identity=a; Max-Age=3600; Path=/'; localStorage.setItem('qa','a')"
        )
        pb.evaluate(
            "document.cookie='qa_identity=b; Max-Age=3600; Path=/'; localStorage.setItem('qa','b')"
        )
        assert pa.evaluate("document.cookie") == "qa_identity=a"
        assert pb.evaluate("document.cookie") == "qa_identity=b"
        assert pa.evaluate("localStorage.getItem('qa')") == "a"
        assert pb.evaluate("localStorage.getItem('qa')") == "b"
        pa.evaluate("document.getElementById('download').click()")
        download = Path(a["downloads_dir"]) / "sample.txt"
        for _ in range(100):
            if download.exists():
                break
            time.sleep(0.05)
        assert download.read_text() == "local download evidence"
        assert not (Path(b["downloads_dir"]) / "sample.txt").exists()

        # Read through a fresh Manager instance: no dependency on caller memory.
        fresh = Manager(tmp_path / "state")
        assert fresh.view(fresh.get("a"), probe=True)["health"] == "reachable"
        with pytest.raises(Problem) as err:
            fresh.reconnect("a", os.getpid())
        assert err.value.code == "owner_alive"
        owner.terminate()
        owner.wait()
        assert fresh.view(fresh.get("a"))["orphaned"]
        restored = fresh.reconnect("a", os.getpid())
        assert restored["connection"] == a["connection"]
        assert pa.evaluate("localStorage.getItem('qa')") == "a"

        # Simulate caller death between spawning Chrome and persisting its PID.
        interrupted = fresh.get("a")
        interrupted["status"] = "starting"
        interrupted["browser_identity"] = None
        interrupted["connection"] = None
        fresh.save(interrupted, "simulate_spawn_persist_interruption")
        restored = Manager(tmp_path / "state").reconnect("a", os.getpid())
        assert restored["browser_identity"] == a["browser_identity"]
        assert pa.evaluate("localStorage.getItem('qa')") == "a"

        bad = spec(tmp_path, "conflict", chrome_executable=exe, account="b")
        with pytest.raises(Problem) as err:
            fresh.register(bad)
        assert err.value.code == "resource_conflict"
        assert not Path(bad["browser_data_dir"]).exists()

        pa.close()
        assert fresh.stop("a")["status"] == "stopped"
        assert fresh.stop("a")["status"] == "stopped"
        assert identity(b["browser_identity"]["pid"]) == b["browser_identity"]
        assert pb.evaluate("document.cookie") == "qa_identity=b"
        assert fresh.view(fresh.get("b"), probe=True)["health"] == "reachable"

        # Reusing a stopped slot with its original account retains login storage.
        sa2 = {
            **sa,
            "task": "a-next",
            "agent_pid": os.getpid(),
            "artifacts_dir": str(tmp_path / "a-next" / "artifacts"),
        }
        a2 = fresh.register(sa2)
        pa2 = Page(a2["connection"])
        pages.append(pa2)
        pa2.navigate(url)
        assert pa2.evaluate("localStorage.getItem('qa')") == "a"
        pa2.close()
        fresh.stop("a-next")
        assert fresh.purge_profile("a-next")["profile_purged"]
        assert Path(sa2["artifacts_dir"]).exists()
        assert pb.evaluate("localStorage.getItem('qa')") == "b"
    finally:
        for page in pages:
            page.close()
        for row in m.rows():
            m.stop(row["spec"]["task"])
        if owner.poll() is None:
            owner.terminate()
        owner.wait()
        server.shutdown()
        server.server_close()


def register_worker(root, request, barrier, queue):
    m = Manager(Path(root))
    barrier.wait(timeout=10)
    try:
        result = m.register(request)
        queue.put(("ready", result["spec"]["task"]))
    except Problem as exc:
        queue.put((exc.code, request["task"]))


@pytest.mark.chrome
def test_real_concurrent_registration(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue, barrier = ctx.Queue(), ctx.Barrier(2)
    exe = chrome_executable()
    procs = [
        ctx.Process(
            target=register_worker,
            args=(
                str(tmp_path / "state"),
                spec(tmp_path, f"race-{i}", slot="exclusive-slot", chrome_executable=exe),
                barrier,
                queue,
            ),
        )
        for i in range(2)
    ]
    m = Manager(tmp_path / "state")
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=40)
            assert p.exitcode == 0
        results = [queue.get(timeout=2) for _ in procs]
        assert sorted(r[0] for r in results) == ["ready", "resource_conflict"]
        rows = m.rows()
        assert len(rows) == 1
        assert m.view(rows[0], probe=True)["health"] == "reachable"
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(5)
        for row in m.rows():
            m.stop(row["spec"]["task"])
