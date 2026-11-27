# -*- coding: utf-8 -*-
"""程序入口：启动本地服务并自动打开浏览器。"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_TITLE = "PlusVCC"


def app_dir() -> Path:
    root = os.getenv("APPDATA") or str(Path.home())
    path = Path(root) / "PlusVCC"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging() -> Path:
    log_file = app_dir() / "app.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_file


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def say(text: str) -> None:
    """--noconsole 打包后 sys.stdout 为 None，写标准输出要做保护。"""
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        print(text, flush=True)
    except Exception:
        pass


def fatal(message: str) -> None:
    logging.error(message)
    say(message)
    if sys.platform.startswith("win") and getattr(sys, "frozen", False):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} 本地工具")
    parser.add_argument("--port", type=int, default=0, help="固定端口；默认随机空闲端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    log_file = setup_logging()

    # GUI 启动（双击 exe）时 PATH 可能不含 nvm/Program Files，先把 node 绝对路径固定下来。
    try:
        from vcc_tool.shortlink import resolve_node_executable

        node = resolve_node_executable()
        if node:
            os.environ["NODE_EXECUTABLE"] = node
            logging.info("Node.js: %s", node)
        else:
            logging.warning("未找到 Node.js；短链的 sentinel 将无法生成")
    except Exception:
        logging.exception("探测 Node.js 失败")

    from vcc_tool.app import create_app

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    app = create_app()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def serve() -> None:
        try:
            app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
        except Exception:
            logging.exception("本地服务启动失败")

    threading.Thread(target=serve, name="plusvcc-http", daemon=True).start()

    for _ in range(60):
        if port_open(port):
            break
        time.sleep(0.1)
    else:
        fatal(f"端口 {port} 未就绪，请查看日志：{log_file}")
        return 1

    logging.info("%s 已启动: %s (日志 %s)", APP_TITLE, url, log_file)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            logging.exception("打开浏览器失败")

    say(f"{APP_TITLE} 已启动: {url}")
    say("关闭此窗口或点页面右上角「退出程序」结束。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
