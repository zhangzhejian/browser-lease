from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import psutil

ACTIVE = {"starting", "ready", "stopping", "cleanup_required"}
DEFAULT_ROOT = Path.home() / ".local/state/browser-lease"
MARKER = ".browser-lease-owner.json"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
START_TIMEOUT = 20.0
STOP_TIMEOUT = 5.0


class Problem(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def identity(pid: int) -> dict | None:
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        return {"pid": p.pid, "create_time": p.create_time()}
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise Problem("process_unreadable", "无法核对进程身份", pid=pid) from exc


def alive(ref: dict | None) -> bool:
    return bool(ref and identity(ref["pid"]) == ref)


def overlap(a: str | Path, b: str | Path) -> bool:
    a, b = Path(a), Path(b)
    return a == b or a in b.parents or b in a.parents


def write_json(path: Path, data: dict):
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def chrome_executable() -> str:
    candidates = (
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        if sys.platform == "darwin"
        else [
            shutil.which("google-chrome"),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
        ]
    )
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return str(Path(c).resolve())
    raise Problem("chrome_missing", "找不到 Chrome；申报时可填写 chrome_executable 绝对路径")


def native_launch(args: list[str]) -> list[str]:
    # An x86 Python under Rosetta otherwise starts universal Chrome in emulation as well.
    if sys.platform == "darwin" and platform.machine() == "x86_64":
        arm = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if arm.returncode == 0 and arm.stdout.strip() == "1":
            return ["/usr/bin/arch", "-arm64", *args]
    return args


def normalize(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise Problem("invalid_request", "申报必须是 JSON 对象")
    required = {
        "task",
        "slot",
        "agent_pid",
        "environment",
        "browser_data_dir",
        "account",
        "course_run",
        "artifacts_dir",
        "control_mode",
    }
    optional = {"description", "headless", "chrome_executable", "resources", "environment_access"}
    if required - raw.keys() or raw.keys() - required - optional:
        raise Problem(
            "invalid_request",
            "申报字段不完整或包含未知字段",
            missing=sorted(required - raw.keys()),
            unknown=sorted(raw.keys() - required - optional),
        )
    s = json.loads(json.dumps(raw))
    for key in ("task", "slot", "environment", "account"):
        if not isinstance(s[key], str) or not ID.fullmatch(s[key]):
            raise Problem("invalid_request", f"{key} 需为 1–128 位字母、数字、点、横线或下划线")
    if s["course_run"] is not None and (
        not isinstance(s["course_run"], str) or not ID.fullmatch(s["course_run"])
    ):
        raise Problem("invalid_request", "course_run 需为标识符；不涉及学习实例时填 null")
    if type(s["agent_pid"]) is not int or s["agent_pid"] <= 1:
        raise Problem("invalid_request", "agent_pid 必须是实际长期运行的智能体进程 PID，且大于 1")
    owner = identity(s["agent_pid"])
    if not owner:
        raise Problem("agent_not_alive", "申报进程不存在", agent_pid=s["agent_pid"])
    try:
        if psutil.Process(s["agent_pid"]).uids().real != os.getuid():
            raise Problem("invalid_request", "只能申报当前系统用户的进程")
    except psutil.NoSuchProcess as exc:
        raise Problem("agent_not_alive", "申报进程已退出") from exc
    s["owner_identity"] = owner
    for key in ("browser_data_dir", "artifacts_dir"):
        if not isinstance(s[key], str) or not Path(s[key]).is_absolute():
            raise Problem("invalid_request", f"{key} 必须是绝对路径")
        s[key] = str(Path(s[key]).resolve())
        if Path(s[key]) in (Path("/"), Path.home()) or Path(s[key]) in Path.home().parents:
            raise Problem("unsafe_directory", f"{key} 不能是根目录或用户主目录及其父目录")
    if overlap(s["browser_data_dir"], s["artifacts_dir"]):
        raise Problem("invalid_request", "浏览器目录与证据目录不能相同或相互包含")
    if s["control_mode"] not in ("browser", "desktop"):
        raise Problem("invalid_request", "control_mode 只能为 browser 或 desktop")
    s.setdefault("headless", False)
    if type(s["headless"]) is not bool or (s["headless"] and s["control_mode"] == "desktop"):
        raise Problem("invalid_request", "headless 必须是布尔值；desktop 不能使用无头模式")
    s.setdefault("environment_access", "shared")
    if s["environment_access"] not in ("shared", "exclusive"):
        raise Problem("invalid_request", "environment_access 只能为 shared 或 exclusive")
    s.setdefault("description", "")
    if not isinstance(s["description"], str) or len(s["description"]) > 2000:
        raise Problem("invalid_request", "description 最多 2000 字符")
    s.setdefault("resources", [])
    if not isinstance(s["resources"], list) or len(s["resources"]) > 100:
        raise Problem("invalid_request", "resources 最多 100 项")
    seen = set()
    for r in s["resources"]:
        if not isinstance(r, dict) or set(r) != {"kind", "id", "access"}:
            raise Problem("invalid_request", "resources 每项必须包含 kind、id、access")
        if any(not isinstance(r[k], str) or not ID.fullmatch(r[k]) for k in ("kind", "id")):
            raise Problem("invalid_request", "资源 kind、id 需为标识符")
        if r["access"] not in ("read", "write") or (r["kind"], r["id"]) in seen:
            raise Problem("invalid_request", "资源 access 只能为 read/write，资源不可重复")
        seen.add((r["kind"], r["id"]))
    s["resources"] = sorted(s["resources"], key=lambda r: (r["kind"], r["id"]))
    exe = s.get("chrome_executable") or chrome_executable()
    if not isinstance(exe, str) or not Path(exe).is_absolute():
        raise Problem("invalid_request", "chrome_executable 必须是绝对路径")
    exe = Path(exe).resolve()
    if not exe.is_file() or not os.access(exe, os.X_OK):
        raise Problem("chrome_missing", "Chrome 可执行文件不存在或不可执行")
    s["chrome_executable"] = str(exe)
    return s


def conflicts(s: dict, rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        if row["status"] not in ACTIVE:
            continue
        old = row["spec"]
        fields = []
        if s["slot"] == old["slot"]:
            fields.append("slot")
        for key in ("browser_data_dir", "artifacts_dir"):
            for other in ("browser_data_dir", "artifacts_dir"):
                if overlap(s[key], old[other]):
                    fields.append(f"{key}:{other}")
        if "desktop" in (s["control_mode"], old["control_mode"]):
            fields.append("desktop")
        if s["environment"] == old["environment"]:
            if "exclusive" in (s["environment_access"], old["environment_access"]):
                fields.append("environment")
            if s["account"] == old["account"]:
                fields.append("account")
            if s["course_run"] and s["course_run"] == old["course_run"]:
                fields.append("course_run")
            for a in s["resources"]:
                for b in old["resources"]:
                    if (a["kind"], a["id"]) == (b["kind"], b["id"]) and (
                        "write" in (a["access"], b["access"])
                    ):
                        fields.append(f"resource:{a['kind']}:{a['id']}")
        if fields:
            result.append(
                {
                    "task": old["task"],
                    "slot": old["slot"],
                    "status": row["status"],
                    "fields": sorted(set(fields)),
                }
            )
    return result


class Manager:
    def __init__(self, root: Path = DEFAULT_ROOT, start_timeout=START_TIMEOUT):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.start_timeout = start_timeout
        self.db_path = self.root / "registry.sqlite3"
        # Schema initialization is short and transactional; reads remain available while another
        # CLI holds the lifecycle lock waiting for Chrome to start.
        with self.db() as db:
            db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS tasks (task TEXT PRIMARY KEY, record TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY, at REAL NOT NULL, task TEXT NOT NULL,
                        action TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('registry_id', ?)", (uuid.uuid4().hex,)
            )
            self.registry_id = db.execute(
                "SELECT value FROM metadata WHERE key='registry_id'"
            ).fetchone()[0]
        os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / "registry.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as f:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Problem("registry_busy", "另一条申报或清理正在执行，请稍后重试")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self) -> list[dict]:
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT record FROM tasks ORDER BY task")]

    def get(self, task: str) -> dict:
        with self.db() as db:
            r = db.execute("SELECT record FROM tasks WHERE task=?", (task,)).fetchone()
        if not r:
            raise Problem("not_found", "任务不存在", task=task)
        return json.loads(r[0])

    def save(self, row: dict, action: str, detail: dict | None = None):
        row["updated_at"] = time.time()
        task = row["spec"]["task"]
        with self.db() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?) ON CONFLICT(task) DO UPDATE SET record=excluded.record",
                (task, json.dumps(row, ensure_ascii=False)),
            )
            db.execute(
                "INSERT INTO events(at, task, action, detail) VALUES (?, ?, ?, ?)",
                (time.time(), task, action, json.dumps(detail or {}, ensure_ascii=False)),
            )

    def marker_data(self, spec: dict, kind: str) -> dict:
        data = {"registry": self.registry_id, "kind": kind}
        if kind == "profile":
            data.update(environment=spec["environment"], account=spec["account"])
        else:
            data["task"] = spec["task"]
        return data

    def directory(self, spec: dict, key: str, create: bool = False) -> dict | None:
        path = Path(spec[key])
        kind = "profile" if key == "browser_data_dir" else "artifacts"
        expected = self.marker_data(spec, kind)
        if path.resolve() != path or path.is_symlink():
            raise Problem("directory_changed", "目录出现符号链接或路径变化", field=key)
        if overlap(path, self.root):
            raise Problem("unsafe_directory", "任务目录不能与启动器状态目录相互包含", field=key)
        if path.exists():
            if not path.is_dir():
                raise Problem("unsafe_directory", "申报路径不是目录", field=key)
            marker = path / MARKER
            if marker.exists():
                try:
                    current = json.loads(marker.read_text())
                except (ValueError, OSError) as exc:
                    raise Problem("unsafe_directory", "目录归属标记无法读取", field=key) from exc
                if current != expected or marker.is_symlink():
                    raise Problem(
                        "directory_owner_conflict",
                        "目录属于其他账号、环境、任务或注册表",
                        field=key,
                    )
            elif any(path.iterdir()):
                raise Problem("unmanaged_directory", "只能使用空目录或本工具拥有的目录", field=key)
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
            if not (path / MARKER).exists():
                write_json(path / MARKER, expected)
            st = path.stat()
            return {"device": st.st_dev, "inode": st.st_ino}
        return None

    def check_directory_identity(self, row: dict, key: str):
        path = Path(row["spec"][key])
        self.directory(row["spec"], key)
        if not path.exists() or not (path / MARKER).is_file():
            raise Problem("directory_changed", "受管目录或归属标记缺失", field=key)
        st = path.stat()
        if row.get("directories", {}).get(key) != {"device": st.st_dev, "inode": st.st_ino}:
            raise Problem("directory_changed", "目录已被替换，拒绝自动操作", field=key)

    def browser_processes(self, row: dict, *, owned: bool = True) -> list[dict]:
        """Exact launch flags recover the spawn→persist crash window without matching all Chrome."""
        s = row["spec"]
        refs = []
        for proc in psutil.process_iter(["pid", "create_time", "cmdline", "status"]):
            try:
                info = proc.info
                args = info["cmdline"] or []
                executable = args[0] if args else ""
                if len(args) > 2 and args[:2] == ["/usr/bin/arch", "-arm64"]:
                    executable = args[2]
                if (
                    info["status"] != psutil.STATUS_ZOMBIE
                    and args
                    and str(Path(executable).resolve()) == s["chrome_executable"]
                    and f"--user-data-dir={s['browser_data_dir']}" in args
                    and (
                        not owned
                        or (
                            "--remote-debugging-port=0" in args
                            and f"--browser-lease-launch-id={row.get('launch_id')}" in args
                        )
                    )
                ):
                    refs.append({"pid": info["pid"], "create_time": info["create_time"]})
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return refs

    def endpoint(self, row: dict) -> dict:
        if not alive(row.get("browser_identity")):
            raise Problem("browser_unavailable", "浏览器进程已退出或身份变化")
        self.check_directory_identity(row, "browser_data_dir")
        try:
            parts = (
                (Path(row["spec"]["browser_data_dir"]) / "DevToolsActivePort")
                .read_text()
                .splitlines()
            )
            port = int(parts[0])
            ws_path = parts[1]
            if not 1 <= port <= 65535 or not re.fullmatch(r"/devtools/browser/[\w-]+", ws_path):
                raise ValueError("invalid endpoint")
            url = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url + "/json/version", timeout=1) as response:
                version = json.loads(response.read(65536))
            websocket = version["webSocketDebuggerUrl"]
            if websocket != f"ws://127.0.0.1:{port}{ws_path}":
                raise ValueError("endpoint identity mismatch")
            previous = row.get("connection")
            if previous and previous["websocket_url"] != websocket:
                raise ValueError("browser endpoint changed")
            return {
                "cdp_url": url,
                "websocket_url": websocket,
                "browser_version": version.get("Browser"),
                "transport": "cdp",
            }
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise Problem("browser_unavailable", "浏览器连接尚未就绪或身份不匹配") from exc

    def view(self, row: dict, probe: bool = False) -> dict:
        out = dict(row)
        out["owner_alive"] = alive(row["spec"]["owner_identity"])
        out["browser_alive"] = alive(row.get("browser_identity"))
        out["orphaned"] = row["status"] in ACTIVE and not out["owner_alive"]
        out["health"] = "not_checked"
        if probe and row["status"] in ACTIVE:
            try:
                out["connection"] = self.endpoint(row)
                out["health"] = "reachable"
            except Problem as exc:
                out["health"] = exc.code
                out["connection"] = None
        elif row["status"] != "ready" or not out["browser_alive"]:
            out["connection"] = None
        out["verification_required"] = ["environment", "account", "course_run"]
        return out

    def register(self, raw: dict) -> dict:
        spec = normalize(raw)
        with self.lock():
            rows = self.rows()
            previous = next((r for r in rows if r["spec"]["task"] == spec["task"]), None)
            if previous:
                if previous["spec"] != spec:
                    raise Problem("task_exists", "任务编号已存在且申报不同；请查询或使用新编号")
                if previous["status"] == "ready":
                    self.endpoint(previous)
                    return self.view(previous, probe=True)
                raise Problem(
                    "task_not_ready",
                    "已有申报未就绪；请查询并清理，勿重复启动",
                    status=previous["status"],
                )
            found = conflicts(spec, rows)
            if found:
                raise Problem("resource_conflict", "资源被占用，请修改申报后重试", conflicts=found)
            for key in ("browser_data_dir", "artifacts_dir"):
                self.directory(spec, key)
            # Reject externally started browsers even if the persistent directory is ours.
            if self.browser_processes({"spec": spec}, owned=False):
                raise Problem("browser_already_running", "申报目录已有浏览器，拒绝接管")
            row = {
                "spec": spec,
                "status": "starting",
                "created_at": time.time(),
                "browser_identity": None,
                "connection": None,
                "directories": {},
                "error": None,
                "launch_id": uuid.uuid4().hex,
            }
            self.save(row, "reserved")
            child = None
            try:
                for key in ("browser_data_dir", "artifacts_dir"):
                    row["directories"][key] = self.directory(spec, key, create=True)
                self.save(row, "directories_ready")
                # Set downloads inside the task's artifact tree before this private Chrome starts.
                downloads = Path(spec["artifacts_dir"]) / "downloads"
                downloads.mkdir(exist_ok=True, mode=0o700)
                prefs = Path(spec["browser_data_dir"]) / "Default" / "Preferences"
                prefs.parent.mkdir(exist_ok=True, mode=0o700)
                preferences = json.loads(prefs.read_text()) if prefs.exists() else {}
                preferences.setdefault("download", {}).update(
                    default_directory=str(downloads),
                    prompt_for_download=False,
                    directory_upgrade=True,
                )
                write_json(prefs, preferences)
                row["downloads_dir"] = str(downloads)
                active_port = Path(spec["browser_data_dir"]) / "DevToolsActivePort"
                active_port.unlink(missing_ok=True)
                args = [
                    spec["chrome_executable"],
                    f"--user-data-dir={spec['browser_data_dir']}",
                    "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-mode",
                    f"--browser-lease-launch-id={row['launch_id']}",
                ]
                if spec["headless"]:
                    args.append("--headless=new")
                args.append("about:blank")
                # Logs can contain page URLs: keep them private, never echo them to the agent.
                log = self.root / (
                    "chrome-" + hashlib.sha256(spec["task"].encode()).hexdigest() + ".log"
                )
                fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "ab") as output:
                    child = subprocess.Popen(
                        native_launch(args),
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=output,
                        start_new_session=True,
                    )
                row["browser_identity"] = identity(child.pid)
                row["log_file"] = str(log)
                self.save(row, "browser_spawned")
                deadline = time.monotonic() + self.start_timeout
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise Problem(
                            "browser_start_failed", "Chrome 启动后退出", exit_code=child.returncode
                        )
                    try:
                        row["connection"] = self.endpoint(row)
                        break
                    except Problem:
                        time.sleep(0.1)
                else:
                    raise Problem("browser_start_timeout", "Chrome 在启动时限内未就绪")
                row["status"] = "ready"
                self.save(row, "ready")
                return self.view(row)
            except BaseException as exc:
                row["error"] = exc.code if isinstance(exc, Problem) else type(exc).__name__
                row["status"] = "cleanup_required"
                self.save(row, "start_failed", {"code": row["error"]})
                try:
                    self._stop(row, final_status="failed")
                except Problem:
                    pass  # Keep the reservation until cleanup actually succeeds.
                if child:
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                raise

    def _stop(self, row: dict, final_status: str = "stopped"):
        row["status"] = "stopping"
        self.save(row, "stopping")
        try:
            # Verify the profile when it was created; no directories may exist on early failure.
            if "browser_data_dir" in row["directories"]:
                self.check_directory_identity(row, "browser_data_dir")
            refs = self.browser_processes(row)
            if any(ref not in refs for ref in self.browser_processes(row, owned=False)):
                raise Problem("process_identity_mismatch", "专属目录被其他启动来源占用，拒绝终止")
            recorded = row.get("browser_identity")
            if alive(recorded) and recorded not in refs:
                raise Problem("process_identity_mismatch", "进程身份无法与专属目录对应，拒绝终止")
            descendants = row.get("cleanup_identities", [])
            for ref in refs:
                try:
                    p = psutil.Process(ref["pid"])
                    if p.create_time() != ref["create_time"]:
                        continue
                    descendants.extend(
                        filter(None, (identity(c.pid) for c in p.children(recursive=True)))
                    )
                    row["cleanup_identities"] = descendants
                    self.save(row, "cleanup_processes_identified")
                    p.send_signal(signal.SIGTERM)
                except psutil.NoSuchProcess:
                    continue
            all_refs = refs + descendants
            deadline = time.monotonic() + STOP_TIMEOUT
            while time.monotonic() < deadline and any(alive(r) for r in all_refs):
                time.sleep(0.05)
            for ref in all_refs:
                try:
                    p = psutil.Process(ref["pid"])
                    if p.create_time() == ref["create_time"]:
                        p.kill()
                except psutil.NoSuchProcess:
                    pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and any(alive(r) for r in all_refs):
                time.sleep(0.05)
            if any(alive(r) for r in all_refs) or self.browser_processes(row):
                raise Problem("cleanup_incomplete", "仍有任务浏览器进程存活，占用保持锁定")
            row["status"] = final_status
            row["connection"] = None
            row["stopped_at"] = time.time()
            self.save(row, final_status)
        except (Problem, psutil.Error, OSError) as exc:
            row["status"] = "cleanup_required"
            self.save(row, "cleanup_failed", {"code": getattr(exc, "code", type(exc).__name__)})
            raise Problem(
                "cleanup_incomplete", "清理未完成，占用仍保留；请查询任务", task=row["spec"]["task"]
            ) from exc

    def stop(self, task: str) -> dict:
        with self.lock():
            row = self.get(task)
            if row["status"] in ACTIVE:
                self._stop(row)
            return self.view(row)

    def reconnect(self, task: str, agent_pid: int) -> dict:
        with self.lock():
            row = self.get(task)
            spec = row["spec"]
            owner = spec["owner_identity"]
            # Reuse validation for PID ownership and avoid trusting a recycled PID.
            request = {k: v for k, v in spec.items() if k != "owner_identity"}
            request["agent_pid"] = agent_pid
            new_owner = normalize(request)["owner_identity"]
            if owner != new_owner and alive(owner):
                raise Problem(
                    "owner_alive",
                    "原执行者仍存活，拒绝接管；先由原执行者结束任务",
                    agent_pid=owner["pid"],
                )
            if row["status"] not in ("ready", "starting"):
                raise Problem("task_not_ready", "此任务不可重连", status=row["status"])
            if not row.get("browser_identity"):
                refs = self.browser_processes(row)
                if len(refs) != 1:
                    raise Problem("browser_unavailable", "无法唯一恢复浏览器进程，请清理此任务")
                row["browser_identity"] = refs[0]
            row["connection"] = self.endpoint(row)
            spec["agent_pid"], spec["owner_identity"] = agent_pid, new_owner
            row["status"] = "ready"
            self.save(row, "reconnected", {"agent_pid": agent_pid})
            return self.view(row)

    def cleanup_orphans(self, apply: bool = False) -> list[dict]:
        with self.lock():
            found = [
                r
                for r in self.rows()
                if r["status"] in ACTIVE and not alive(r["spec"]["owner_identity"])
            ]
            results = []
            for row in found:
                error = None
                if apply:
                    try:
                        self._stop(row)
                    except Problem as exc:
                        error = exc.code
                results.append(
                    {"task": row["spec"]["task"], "status": row["status"], "error": error}
                )
            return results

    def purge_profile(self, task: str) -> dict:
        with self.lock():
            row = self.get(task)
            if row["status"] in ACTIVE:
                raise Problem("task_active", "先停止任务，再删除登录资料")
            path = Path(row["spec"]["browser_data_dir"])
            if row.get("profile_purged"):
                return self.view(row)
            for other in self.rows():
                if other["status"] in ACTIVE and any(
                    overlap(path, other["spec"][key])
                    for key in ("browser_data_dir", "artifacts_dir")
                ):
                    raise Problem(
                        "resource_conflict", "目录正被其他任务使用", task=other["spec"]["task"]
                    )
            self.check_directory_identity(row, "browser_data_dir")
            if self.browser_processes(row, owned=False):
                raise Problem("browser_already_running", "目录仍有浏览器进程，拒绝删除")
            # Move the verified inode before recursive deletion; symlinks inside are not followed.
            trash = path.with_name(path.name + ".purge-" + uuid.uuid4().hex)
            path.rename(trash)
            st = trash.stat()
            if {"device": st.st_dev, "inode": st.st_ino} != row["directories"]["browser_data_dir"]:
                raise Problem("directory_changed", "目录被替换，拒绝递归删除")
            shutil.rmtree(trash)
            row["profile_purged"] = True
            self.save(row, "profile_purged")
            return self.view(row)

    def events(self, task: str, limit: int = 50) -> list[dict]:
        self.get(task)
        with self.db() as db:
            return [
                {"seq": r[0], "at": r[1], "action": r[2], "detail": json.loads(r[3])}
                for r in db.execute(
                    "SELECT seq, at, action, detail FROM events WHERE task=? ORDER BY seq DESC LIMIT ?",
                    (task, limit),
                )
            ]
