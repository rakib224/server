#!/usr/bin/env python3
"""
Python Web IDE - Local server for Termux
Only uses Python standard library.
"""

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

# ============================================================
# CONFIG
# ============================================================

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
HOME_DIR = (BASE_DIR / "home").resolve()
INDEX_FILE = BASE_DIR / "index.html"

HOME_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# PROCESS REGISTRY
# ============================================================

PROCESSES = {}
PROCESSES_LOCK = threading.Lock()


def start_python_process(code: str) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(HOME_DIR),
        env=env,
        bufsize=0,
        shell=False,
    )

    entry = {
        "proc": proc,
        "output": bytearray(),
        "done": False,
        "returncode": None,
        "lock": threading.Lock(),
    }
    with PROCESSES_LOCK:
        PROCESSES[proc.pid] = entry

    def reader():
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            with entry["lock"]:
                entry["output"].extend(chunk)
        try:
            proc.wait()
        except Exception:
            pass
        with entry["lock"]:
            entry["done"] = True
            entry["returncode"] = proc.returncode

    threading.Thread(target=reader, daemon=True).start()
    return proc.pid


def get_process(pid: int):
    with PROCESSES_LOCK:
        return PROCESSES.get(pid)


def stop_process(pid: int) -> bool:
    entry = get_process(pid)
    if not entry:
        return False
    proc = entry["proc"]
    if proc.poll() is not None:
        return True
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=1.5)
        return True
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return True


def send_stdin(pid: int, text: str) -> bool:
    entry = get_process(pid)
    if not entry:
        return False
    proc = entry["proc"]
    if proc.poll() is not None:
        return False
    try:
        if not text.endswith("\n"):
            text = text + "\n"
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.flush()
        return True
    except Exception:
        return False


# ============================================================
# PATH SAFETY
# ============================================================

def safe_path(rel_path) -> Path | None:
    """Return resolved absolute path inside HOME_DIR, else None."""
    if rel_path is None:
        rel_path = ""
    if not isinstance(rel_path, str):
        return None

    rel_path = rel_path.replace("\\", "/")
    while rel_path.startswith("/"):
        rel_path = rel_path[1:]

    if rel_path in ("", "."):
        return HOME_DIR

    if "\x00" in rel_path:
        return None

    try:
        target = (HOME_DIR / rel_path).resolve()
    except Exception:
        return None

    try:
        target.relative_to(HOME_DIR)
    except ValueError:
        return None

    return target


def is_home(path) -> bool:
    try:
        return Path(path).resolve() == HOME_DIR
    except Exception:
        return False


# ============================================================
# FILE TREE
# ============================================================

def build_tree(abs_dir: Path, rel_dir: str = ""):
    items = []
    try:
        names = sorted(os.listdir(abs_dir))
    except Exception:
        return items

    dirs, files = [], []
    for name in names:
        full = abs_dir / name
        if full.is_symlink():
            continue
        rel = name if not rel_dir else f"{rel_dir}/{name}"
        if full.is_dir():
            dirs.append({
                "name": name,
                "type": "dir",
                "path": rel,
                "children": build_tree(full, rel),
            })
        elif full.is_file():
            files.append({
                "name": name,
                "type": "file",
                "path": rel,
            })
    return dirs + files


# ============================================================
# HTTP HANDLER
# ============================================================

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PythonWebIDE/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------
    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _err(self, msg, status=400):
        self._send_json({"ok": False, "error": str(msg)}, status)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
        except Exception:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- routes ----------
    def do_GET(self):
        try:
            self._route_get()
        except Exception as e:
            self._err(f"Server error: {e}", 500)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            self._err(f"Server error: {e}", 500)

    def _route_get(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/tree":
            return self._api_tree()
        if path.startswith("/api/file/"):
            return self._api_read_file(path[len("/api/file/"):])
        if path.startswith("/api/output/"):
            return self._api_output(path[len("/api/output/"):])
        return self._err("Not found", 404)

    def _route_post(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/save":
            return self._api_save()
        if path == "/api/new-file":
            return self._api_new_file()
        if path == "/api/new-folder":
            return self._api_new_folder()
        if path == "/api/delete":
            return self._api_delete()
        if path == "/api/rename":
            return self._api_rename()
        if path == "/api/run":
            return self._api_run()
        if path == "/api/stop":
            return self._api_stop()
        if path == "/api/input":
            return self._api_input()
        if path == "/api/pip":
            return self._api_pip()
        return self._err("Not found", 404)

    # ---------- static ----------
    def _serve_index(self):
        if not INDEX_FILE.exists():
            msg = b"index.html not found next to server.py"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        data = INDEX_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- API ----------
    def _api_tree(self):
        try:
            tree = build_tree(HOME_DIR, "")
        except Exception as e:
            return self._err(f"Cannot read tree: {e}")
        return self._send_json({"ok": True, "tree": tree})

    def _api_read_file(self, rel):
        target = safe_path(rel)
        if target is None:
            return self._err("Invalid path")
        if not target.exists() or not target.is_file():
            return self._err("File not found", 404)
        try:
            data = target.read_bytes()
        except Exception as e:
            return self._err(f"Cannot read file: {e}")

        if b"\x00" in data[:8192]:
            return self._err("Binary file cannot be opened in editor")

        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                content = data.decode("latin-1")
            except Exception:
                return self._err("Cannot decode file as text")

        return self._send_json({"ok": True, "path": rel, "content": content})

    def _api_save(self):
        body = self._read_body()
        rel = body.get("path")
        content = body.get("content", "")

        if not isinstance(rel, str):
            return self._err("Invalid path")
        if not isinstance(content, str):
            return self._err("Invalid content")

        target = safe_path(rel)
        if target is None:
            return self._err("Invalid path")
        if is_home(target):
            return self._err("Cannot overwrite home root")
        if target.exists() and target.is_dir():
            return self._err("Target is a directory")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except Exception as e:
            return self._err(f"Cannot save: {e}")
        return self._send_json({"ok": True})

    def _api_new_file(self):
        body = self._read_body()
        rel = body.get("path")
        if not isinstance(rel, str) or not rel.strip():
            return self._err("Invalid path")
        target = safe_path(rel)
        if target is None:
            return self._err("Invalid path")
        if is_home(target):
            return self._err("Cannot create at home root")
        if target.exists():
            return self._err("Already exists")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        except Exception as e:
            return self._err(f"Cannot create file: {e}")
        return self._send_json({"ok": True})

    def _api_new_folder(self):
        body = self._read_body()
        rel = body.get("path")
        if not isinstance(rel, str) or not rel.strip():
            return self._err("Invalid path")
        target = safe_path(rel)
        if target is None:
            return self._err("Invalid path")
        if is_home(target):
            return self._err("Cannot create at home root")
        if target.exists():
            return self._err("Already exists")
        try:
            target.mkdir(parents=True, exist_ok=False)
        except Exception as e:
            return self._err(f"Cannot create folder: {e}")
        return self._send_json({"ok": True})

    def _api_delete(self):
        body = self._read_body()
        rel = body.get("path")
        if not isinstance(rel, str):
            return self._err("Invalid path")
        target = safe_path(rel)
        if target is None:
            return self._err("Invalid path")
        if is_home(target):
            return self._err("Cannot delete home root")
        if not target.exists():
            return self._err("Not found", 404)
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as e:
            return self._err(f"Cannot delete: {e}")
        return self._send_json({"ok": True})

    def _api_rename(self):
        body = self._read_body()
        src = body.get("path")
        new_name = body.get("name", "")

        if not isinstance(src, str) or not isinstance(new_name, str):
            return self._err("Invalid input")
        new_name = new_name.strip()
        if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            return self._err("Invalid name")

        src_abs = safe_path(src)
        if src_abs is None:
            return self._err("Invalid path")
        if is_home(src_abs):
            return self._err("Cannot rename home root")
        if not src_abs.exists():
            return self._err("Not found", 404)

        parent_rel = "" if not src else (src.rsplit("/", 1)[0] if "/" in src else "")
        new_rel = (parent_rel + "/" + new_name).lstrip("/")

        dst_abs = safe_path(new_rel)
        if dst_abs is None:
            return self._err("Invalid destination")
        if is_home(dst_abs):
            return self._err("Invalid destination")
        if dst_abs.exists():
            return self._err("Destination exists")

        try:
            src_abs.rename(dst_abs)
        except Exception as e:
            return self._err(f"Cannot rename: {e}")
        return self._send_json({"ok": True, "new_path": new_rel})

    def _api_run(self):
        body = self._read_body()
        code = body.get("code", "")
        if not isinstance(code, str):
            return self._err("Invalid code")
        try:
            pid = start_python_process(code)
        except Exception as e:
            return self._err(str(e))
        return self._send_json({"ok": True, "pid": pid})

    def _api_output(self, pid_str):
        try:
            pid = int(pid_str)
        except Exception:
            return self._err("Invalid pid")
        entry = get_process(pid)
        if not entry:
            return self._err("Unknown process", 404)
        with entry["lock"]:
            out = bytes(entry["output"]).decode("utf-8", errors="replace")
            done = entry["done"]
            rc = entry["returncode"]
        return self._send_json({
            "ok": True,
            "output": out,
            "done": done,
            "returncode": rc,
        })

    def _api_stop(self):
        body = self._read_body()
        pid = body.get("pid")
        try:
            pid = int(pid)
        except Exception:
            return self._err("Invalid pid")
        ok = stop_process(pid)
        return self._send_json({"ok": ok})

    def _api_input(self):
        body = self._read_body()
        pid = body.get("pid")
        text = body.get("text", "")
        try:
            pid = int(pid)
        except Exception:
            return self._err("Invalid pid")
        if not isinstance(text, str):
            return self._err("Invalid text")
        if not send_stdin(pid, text):
            return self._err("Process not running")
        return self._send_json({"ok": True})

    def _api_pip(self):
        body = self._read_body()
        pkg = body.get("package", "")
        if not isinstance(pkg, str) or not pkg.strip():
            return self._err("Invalid package")
        pkg = pkg.strip()

        if not re.match(r"^[A-Za-z0-9_.\-]+([=<>!~]=?[A-Za-z0-9_.\-*]+)?$", pkg):
            return self._err("Invalid package name")

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                cwd=str(HOME_DIR),
                capture_output=True,
                timeout=600,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return self._err("pip install timed out")
        except Exception as e:
            return self._err(f"pip failed: {e}")

        out = (result.stdout or b"").decode("utf-8", errors="replace")
        err = (result.stderr or b"").decode("utf-8", errors="replace")
        combined = out
        if err:
            combined += ("\n" if combined else "") + err

        return self._send_json({
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": combined,
        })


# ============================================================
# MAIN
# ============================================================

def main():
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    print("=" * 40)
    print("          PYTHON WEB IDE")
    print("=" * 40)
    print(f"Home : {HOME_DIR}")
    print(f"URL  : http://{HOST}:{PORT}")
    print("=" * 40)
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()