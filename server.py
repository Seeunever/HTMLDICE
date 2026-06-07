#!/usr/bin/env python3
"""静态文件服务器 + 无密码用户名登录/注册 + 角色权限。"""

import json
import os
import re
import secrets
import socket
import threading
from datetime import datetime, timezone
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8080
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(DIRECTORY, "users.json")
CHARACTERS_FILE = os.path.join(DIRECTORY, "characters.json")
SESSION_COOKIE = "session"
ROLE_PLAYER = "player"
ROLE_KP = "kp"
ROLE_ADMIN = "admin"
USERNAME_PATTERN = re.compile(
    r"^[\w\u4e00-\u9fff\u3400-\u4dbf\u20000-\u2a6df\u2a700-\u2b73f\u2b740-\u2b81f\u2b820-\u2ceaf\u2ceb0-\u2ebef\u30000-\u3134f-]{2,20}$"
)

sessions = {}
data_lock = threading.Lock()


def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def load_characters():
    if not os.path.exists(CHARACTERS_FILE):
        return {}
    with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_characters(characters):
    with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(characters, f, ensure_ascii=False, indent=2)


def normalize_username(username):
    return username.strip()


def validate_username(username):
    if not USERNAME_PATTERN.match(username):
        return "用户名需为 2–20 个字符，仅支持字母、数字、下划线、连字符及中文"
    return None


def get_user_role(username):
    users = load_users()
    user = users.get(username, {})
    return user.get("role", ROLE_PLAYER)


def can_view_all_characters(role):
    return role in (ROLE_ADMIN, ROLE_KP)


def can_edit_character_status(role):
    return role == ROLE_KP


def json_response(handler, status, payload, set_cookie=None, clear_cookie=False):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if set_cookie:
        handler.send_header("Set-Cookie", set_cookie)
    if clear_cookie:
        handler.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def get_session_id(handler):
    cookie_header = handler.headers.get("Cookie")
    if not cookie_header:
        return None
    jar = cookies.SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def get_current_user(handler):
    session_id = get_session_id(handler)
    if not session_id:
        return None
    return sessions.get(session_id)


def create_session(username):
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = username
    return (
        f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
    )


def character_summary(username, character):
    profile = character.get("profile", {})
    status = character.get("status", {})
    return {
        "username": username,
        "name": profile.get("name") or username,
        "occupation": profile.get("occupation") or "",
        "hp": status.get("hp"),
        "san": status.get("san"),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/me":
            user = get_current_user(self)
            if user:
                role = get_user_role(user)
                json_response(
                    self,
                    200,
                    {"logged_in": True, "username": user, "role": role},
                )
            else:
                json_response(self, 200, {"logged_in": False})
            return

        if path == "/api/players":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            if not can_view_all_characters(role):
                json_response(self, 403, {"ok": False, "error": "无权访问"})
                return
            characters = load_characters()
            players = [
                character_summary(name, data)
                for name, data in characters.items()
            ]
            players.sort(key=lambda item: item["username"])
            json_response(
                self,
                200,
                {"ok": True, "players": players, "can_edit_status": can_edit_character_status(role)},
            )
            return

        if path == "/api/character":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return

            role = get_user_role(user)
            target = normalize_username(query.get("target", [""])[0] or user)
            if target != user and not can_view_all_characters(role):
                json_response(self, 403, {"ok": False, "error": "无权查看他人物卡"})
                return

            characters = load_characters()
            if target not in characters:
                json_response(self, 404, {"ok": False, "error": "尚未录入人物卡"})
                return

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "character": characters[target],
                    "target": target,
                    "can_edit_status": can_edit_character_status(role),
                },
            )
            return

        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = read_json_body(self)
        if body is None:
            json_response(self, 400, {"ok": False, "error": "请求格式无效"})
            return

        if path == "/api/character/status":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            if not can_edit_character_status(role):
                json_response(self, 403, {"ok": False, "error": "仅 KP 可调整状态"})
                return

            target = normalize_username(body.get("target", ""))
            if not target:
                json_response(self, 400, {"ok": False, "error": "请指定角色"})
                return

            hp_delta = body.get("hp_delta", 0)
            san_delta = body.get("san_delta", 0)
            try:
                hp_delta = int(hp_delta)
                san_delta = int(san_delta)
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "调整值无效"})
                return

            if hp_delta == 0 and san_delta == 0:
                json_response(self, 400, {"ok": False, "error": "未指定调整"})
                return

            with data_lock:
                characters = load_characters()
                if target not in characters:
                    json_response(self, 404, {"ok": False, "error": "角色不存在"})
                    return
                status = characters[target].setdefault("status", {})
                if hp_delta:
                    current_hp = status.get("hp", 0) or 0
                    status["hp"] = max(0, int(current_hp) + hp_delta)
                if san_delta:
                    current_san = status.get("san", 0) or 0
                    status["san"] = max(0, min(99, int(current_san) + san_delta))
                save_characters(characters)
                updated = characters[target]

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "target": target,
                    "status": updated.get("status", {}),
                },
            )
            return

        username = normalize_username(body.get("username", ""))
        error = validate_username(username)
        if error:
            json_response(self, 400, {"ok": False, "error": error})
            return

        if path == "/api/register":
            with data_lock:
                users = load_users()
                if username in users:
                    json_response(self, 409, {"ok": False, "error": "用户名已被注册"})
                    return
                users[username] = {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "role": ROLE_PLAYER,
                }
                save_users(users)

            cookie = create_session(username)
            json_response(
                self,
                200,
                {"ok": True, "username": username, "role": ROLE_PLAYER},
                set_cookie=cookie,
            )
            return

        if path == "/api/login":
            with data_lock:
                users = load_users()
                if username not in users:
                    json_response(self, 404, {"ok": False, "error": "用户不存在，请先注册"})
                    return
                role = users[username].get("role", ROLE_PLAYER)

            cookie = create_session(username)
            json_response(
                self,
                200,
                {"ok": True, "username": username, "role": role},
                set_cookie=cookie,
            )
            return

        if path == "/api/logout":
            session_id = get_session_id(self)
            if session_id and session_id in sessions:
                del sessions[session_id]
            json_response(self, 200, {"ok": True}, clear_cookie=True)
            return

        json_response(self, 404, {"ok": False, "error": "接口不存在"})

    def log_message(self, format, *args):
        if args and isinstance(args[0], str) and args[0].startswith("GET /api"):
            return
        super().log_message(format, *args)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        local_ip = get_local_ip()
        print("服务已启动")
        print(f"  本机访问: http://127.0.0.1:{PORT}")
        print(f"  局域网访问: http://{local_ip}:{PORT}")
        print("  公网访问: http://<你的公网IP>:8080")
        print("按 Ctrl+C 停止")
        httpd.serve_forever()
