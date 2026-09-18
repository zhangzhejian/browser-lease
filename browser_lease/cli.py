from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .manager import ACTIVE, Manager, Problem


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Problem("invalid_arguments", message)


def parser():
    p = Parser(description="浏览器任务申报、查询、重连与回收；所有业务结果输出 JSON")
    sub = p.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="原子申报资源并启动 Chrome；冲突时不创建浏览器")
    reg.add_argument("--file", required=True, help="申报 JSON 文件；- 表示标准输入")
    listing = sub.add_parser("list", help="查询任务、浏览器与资源占用")
    listing.add_argument("--active", action="store_true")
    listing.add_argument("--environment")
    listing.add_argument("--slot")
    listing.add_argument("--agent-pid", type=int)
    listing.add_argument("--probe", action="store_true", help="实际探测专属浏览器连接")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    show = sub.add_parser("show", help="查询任务详情和最新健康状态")
    show.add_argument("task")
    show.add_argument("--probe", action="store_true")
    reconnect = sub.add_parser("reconnect", help="恢复连接；原持有进程已退出时允许新执行者接管")
    reconnect.add_argument("task")
    reconnect.add_argument("--agent-pid", type=int, required=True)
    stop = sub.add_parser("stop", help="关闭任务浏览器并释放资源；保留登录资料和证据")
    stop.add_argument("task")
    cleanup = sub.add_parser("cleanup", help="查询或回收原执行者已退出的孤立任务")
    cleanup.add_argument("--apply", action="store_true", help="执行回收；默认只列候选")
    purge = sub.add_parser("purge-profile", help="显式删除已停止任务的受管登录资料；保留证据")
    purge.add_argument("task")
    events = sub.add_parser("events", help="查看任务生命周期审计记录")
    events.add_argument("task")
    events.add_argument("--limit", type=int, default=50)
    return p


def run(args, manager):
    if args.command == "register":
        try:
            text = sys.stdin.read(65537) if args.file == "-" else Path(args.file).read_text()
            if len(text) > 65536:
                raise Problem("invalid_request", "申报文件不能超过 64 KiB 字符")
            return manager.register(json.loads(text))
        except json.JSONDecodeError as exc:
            raise Problem("invalid_json", "申报文件不是有效 JSON", line=exc.lineno) from exc
    if args.command == "list":
        if not 1 <= args.limit <= 100 or args.offset < 0:
            raise Problem("invalid_arguments", "limit 范围为 1–100，offset 不能小于零")
        rows = manager.rows()
        rows = [
            r
            for r in rows
            if (not args.active or r["status"] in ACTIVE)
            and (not args.environment or r["spec"]["environment"] == args.environment)
            and (not args.slot or r["spec"]["slot"] == args.slot)
            and (args.agent_pid is None or r["spec"]["agent_pid"] == args.agent_pid)
        ]
        return {
            "tasks": [
                manager.view(r, args.probe) for r in rows[args.offset : args.offset + args.limit]
            ],
            "total": len(rows),
            "offset": args.offset,
            "limit": args.limit,
        }
    if args.command == "show":
        return manager.view(manager.get(args.task), args.probe)
    if args.command == "reconnect":
        return manager.reconnect(args.task, args.agent_pid)
    if args.command == "stop":
        return manager.stop(args.task)
    if args.command == "cleanup":
        rows = manager.cleanup_orphans(args.apply)
        if any(r["error"] for r in rows):
            raise Problem("cleanup_incomplete", "部分任务回收失败，占用继续保留", tasks=rows)
        return {"applied": args.apply, "tasks": rows}
    if args.command == "purge-profile":
        return manager.purge_profile(args.task)
    if args.command == "events":
        if not 1 <= args.limit <= 500:
            raise Problem("invalid_arguments", "limit 范围为 1–500")
        return {"events": manager.events(args.task, args.limit)}


def main():
    try:
        args = parser().parse_args()
        result = {
            "schema_version": "browser-lease/0.1",
            "ok": True,
            "code": args.command,
            "data": run(args, Manager()),
        }
        code = 0
    except Problem as exc:
        result = {
            "schema_version": "browser-lease/0.1",
            "ok": False,
            "code": exc.code,
            "error": {"message": exc.message, "detail": exc.detail},
        }
        code = (
            3 if exc.code in {"resource_conflict", "directory_owner_conflict", "owner_alive"} else 2
        )
    except KeyboardInterrupt:
        result = {
            "schema_version": "browser-lease/0.1",
            "ok": False,
            "code": "interrupted",
            "error": {"message": "操作被中断，请查询任务状态"},
        }
        code = 130
    except Exception as exc:  # noqa: BLE001 -- CLI boundary returns bounded, credential-free errors.
        # Do not leak browser logs, environment contents, or request text via a traceback.
        result = {
            "schema_version": "browser-lease/0.1",
            "ok": False,
            "code": "operation_failed",
            "error": {"message": "操作失败，请查询任务与目录权限", "type": type(exc).__name__},
        }
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
