#!/usr/bin/env python3
"""静态文件服务器 + 无密码用户名登录/注册 + 角色权限 + 房间投骰。"""

import base64
import binascii
import json
import os
import random
import re
import secrets
import socket
import threading
from datetime import datetime, timezone
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from character_import import parse_character_bytes, validate_character_xlsx
import tempfile
from pathlib import Path

PORT = 8080
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(DIRECTORY, "users.json")
CHARACTERS_FILE = os.path.join(DIRECTORY, "characters.json")
ROOMS_FILE = os.path.join(DIRECTORY, "rooms.json")
MONSTERS_FILE = os.path.join(DIRECTORY, "monsters.json")
SESSION_COOKIE = "session"
ROLE_PLAYER = "player"
ROLE_KP = "kp"
ROLE_ADMIN = "admin"
USERNAME_PATTERN = re.compile(
    r"^[\w\u4e00-\u9fff\u3400-\u4dbf\u20000-\u2a6df\u2a700-\u2b73f\u2b740-\u2b81f\u2b820-\u2ceaf\u2ceb0-\u2ebef\u30000-\u3134f-]{2,20}$"
)
ROOM_NAME_PATTERN = re.compile(
    r"^[\w\u4e00-\u9fff\u3400-\u4dbf\u20000-\u2a6df\u2a700-\u2b73f\u2b740-\u2b81f\u2b820-\u2ceaf\u2ceb0-\u2ebef\u30000-\u3134f -]{2,30}$"
)
DICE_SIDES = {"d100": 100, "d6": 6, "d4": 4}
CHECK_RESULT_LABELS = {
    "crit_success": "大成功",
    "extreme": "极难成功",
    "hard": "困难成功",
    "success": "成功",
    "fail": "失败",
    "crit_fail": "大失败",
}
MAP_LOCATIONS = {
    "hotel": "酒店",
    "dais": "戴斯家",
    "hospital": "医院",
    "police": "警察局",
    "church": "教堂",
    "bar": "酒吧",
}

sessions = {}
rooms = {}
user_rooms = {}
data_lock = threading.Lock()


def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def migrate_characters_data(raw):
    if isinstance(raw, dict) and raw.get("version") == 2:
        return raw
    accounts = {}
    for key, value in (raw or {}).items():
        if key in ("version", "accounts"):
            continue
        if not isinstance(value, dict) or "profile" not in value:
            continue
        owner = value.get("owner") or value.get("username") or key
        pc_id = value.get("pc_id") or f"pc_legacy_{key}"
        migrated = dict(value)
        migrated["owner"] = owner
        migrated["pc_id"] = pc_id
        migrated.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        accounts.setdefault(owner, {"active_pc_id": pc_id, "pcs": {}})
        accounts[owner]["pcs"][pc_id] = migrated
        if not accounts[owner].get("active_pc_id"):
            accounts[owner]["active_pc_id"] = pc_id
    return {"version": 2, "accounts": accounts}


def load_characters_db():
    if not os.path.exists(CHARACTERS_FILE):
        return {"version": 2, "accounts": {}}
    with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return migrate_characters_data(raw)


def save_characters_db(db):
    with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_account(db, pl_username):
    return db.get("accounts", {}).get(pl_username)


def ensure_account(db, pl_username):
    accounts = db.setdefault("accounts", {})
    if pl_username not in accounts:
        accounts[pl_username] = {"active_pc_id": None, "pcs": {}}
    return accounts[pl_username]


def find_pc(db, pc_id):
    for pl_username, account in db.get("accounts", {}).items():
        pc = account.get("pcs", {}).get(pc_id)
        if pc:
            return pc, account, pl_username
    return None, None, None


def list_pcs_for_pl(db, pl_username):
    account = get_account(db, pl_username)
    if not account:
        return []
    return list(account.get("pcs", {}).values())


def get_active_pc_for_pl(db, pl_username, active_pc_id=None):
    account = get_account(db, pl_username)
    if not account:
        return None
    pc_id = active_pc_id or account.get("active_pc_id")
    if not pc_id:
        return None
    return account.get("pcs", {}).get(pc_id)


def set_active_pc(db, pl_username, pc_id):
    account = ensure_account(db, pl_username)
    if pc_id not in account.get("pcs", {}):
        return False
    account["active_pc_id"] = pc_id
    return True


def iter_all_pcs(db):
    for pl_username, account in db.get("accounts", {}).items():
        for pc in account.get("pcs", {}).values():
            yield pl_username, pc


def load_rooms():
    if not os.path.exists(ROOMS_FILE):
        return {}
    with open(ROOMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_rooms():
    with open(ROOMS_FILE, "w", encoding="utf-8") as f:
        json.dump(rooms, f, ensure_ascii=False, indent=2)


def rebuild_user_rooms():
    user_rooms.clear()
    for room_id, room in rooms.items():
        kp = room.get("kp")
        if kp:
            user_rooms[kp] = room_id
        for player in room.get("players", []):
            user_rooms[player] = room_id


def init_rooms():
    global rooms
    rooms = load_rooms()
    rebuild_user_rooms()


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


def can_be_room_kp(role):
    return role in (ROLE_KP, ROLE_ADMIN)


def validate_room_name(name):
    name = name.strip()
    if not ROOM_NAME_PATTERN.match(name):
        return None, "房间名需为 2–30 个字符，仅支持字母、数字、空格、下划线、连字符及中文"
    return name, None


def evaluate_skill_check(roll, skill_value):
    skill_value = int(skill_value)
    half = skill_value // 2
    quarter = skill_value // 4
    if roll >= 96:
        check_type = "crit_fail"
    elif roll <= 5:
        check_type = "crit_success"
    elif roll <= quarter:
        check_type = "extreme"
    elif roll <= half:
        check_type = "hard"
    elif roll <= skill_value:
        check_type = "success"
    else:
        check_type = "fail"
    return {
        "type": check_type,
        "label": CHECK_RESULT_LABELS[check_type],
        "skill_value": skill_value,
        "half": half,
        "quarter": quarter,
    }


def roll_die(dice_key):
    sides = DICE_SIDES.get(dice_key)
    if not sides:
        return None
    return random.randint(1, sides)


def get_user_room_id(username):
    return user_rooms.get(username)


def room_summary(room_id, room):
    return {
        "id": room_id,
        "name": room.get("name", room_id),
        "kp": room.get("kp"),
        "players": list(room.get("players", [])),
        "player_count": len(room.get("players", [])),
        "created_at": room.get("created_at"),
    }


def can_see_roll_result(roll, username, role):
    if not roll.get("hidden"):
        return True
    if roll.get("roller") == username:
        return True
    if can_be_room_kp(role):
        return True
    return False


def filter_roll_for_user(roll, username, role):
    reveal = can_see_roll_result(roll, username, role)
    return roll_to_client(roll, reveal_result=reveal)


def roll_to_client(roll, reveal_result=True):
    payload = {
        "id": roll["id"],
        "roller": roll["roller"],
        "dice": roll["dice"],
        "label": roll["label"],
        "hidden": roll.get("hidden", False),
        "timestamp": roll.get("timestamp"),
    }
    if reveal_result:
        payload["value"] = roll["value"]
        payload["value_hidden"] = False
        payload["check_type"] = roll.get("check_type")
        payload["check_label"] = roll.get("check_label")
        payload["skill_value"] = roll.get("skill_value")
    else:
        payload["value"] = None
        payload["value_hidden"] = True
        payload["check_type"] = None
        payload["check_label"] = None
        payload["skill_value"] = None
    return payload


def get_attribute_value(character, key):
    for attr in character.get("attributes", []):
        if attr.get("key") == key:
            try:
                return int(attr.get("value", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def get_player_combat_stats(pl_username, db):
    account = get_account(db, pl_username)
    if not account:
        return None
    pc = get_active_pc_for_pl(db, pl_username, account.get("active_pc_id"))
    if not pc:
        return None
    ensure_status_max(pc)
    profile = pc.get("profile", {})
    status = pc.get("status", {})
    stats = status_display_values(status)
    try:
        hp = int(stats["hp"] or 0)
    except (TypeError, ValueError):
        hp = 0
    try:
        hp_max = int(stats["hp_max"] or hp)
    except (TypeError, ValueError):
        hp_max = hp
    return {
        "username": pl_username,
        "pc_id": pc.get("pc_id"),
        "name": profile.get("name") or pl_username,
        "dex": get_attribute_value(pc, "DEX"),
        "hp": hp,
        "max_hp": hp_max,
        "alive": hp > 0,
    }


def build_turn_order(players, monsters):
    entries = []
    for player in players:
        if player.get("alive", True):
            entries.append(
                {
                    "type": "player",
                    "id": player["username"],
                    "name": player.get("name") or player["username"],
                    "dex": int(player.get("dex", 0) or 0),
                }
            )
    for monster in monsters:
        if monster.get("alive", True) and not monster.get("dead", False):
            entries.append(
                {
                    "type": "monster",
                    "id": monster["id"],
                    "name": monster.get("name") or monster["id"],
                    "dex": int(monster.get("dex", 0) or 0),
                }
            )
    entries.sort(key=lambda item: (-item["dex"], 0 if item["type"] == "player" else 1, item["name"]))
    return entries


_monsters_cache = None


def load_monsters():
    global _monsters_cache
    if _monsters_cache is not None:
        return _monsters_cache
    if not os.path.exists(MONSTERS_FILE):
        print("警告: monsters.json 不存在，怪物图鉴为空")
        _monsters_cache = []
        return _monsters_cache
    try:
        with open(MONSTERS_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"警告: 无法加载 monsters.json: {exc}")
        _monsters_cache = []
        return _monsters_cache
    if raw.get("version") != 1:
        print("警告: monsters.json 版本不匹配")
        _monsters_cache = []
        return _monsters_cache
    _monsters_cache = list(raw.get("monsters", []))
    return _monsters_cache


def get_monster_template(template_id):
    if not template_id:
        return None
    for monster in load_monsters():
        if monster.get("id") == template_id:
            return monster
    return None


def monsters_to_client():
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "hp": item.get("hp"),
            "dex": item.get("dex"),
            "skills": list(item.get("skills", [])),
        }
        for item in load_monsters()
    ]


def normalize_monster_skills(skills):
    result = []
    if not isinstance(skills, list):
        return result
    for skill in skills:
        name = str(skill.get("name", "")).strip()
        if not name:
            continue
        try:
            value = int(skill.get("value", 0))
        except (TypeError, ValueError):
            continue
        result.append({"name": name, "value": value})
    return result


def build_combat_monster_entry(monster_id, item):
    template_id = str(item.get("template_id", "")).strip() or None
    template = get_monster_template(template_id) if template_id else None

    name = str(item.get("name", "")).strip()
    if not name and template:
        name = str(template.get("name", "")).strip()

    skills = normalize_monster_skills(item.get("skills"))
    if not skills and template:
        skills = normalize_monster_skills(template.get("skills", []))

    dex_raw = item.get("dex")
    hp_raw = item.get("hp")
    if dex_raw in (None, ""):
        dex_raw = template.get("dex") if template else 0
    if hp_raw in (None, ""):
        hp_raw = template.get("hp") if template else 0

    try:
        dex = int(dex_raw)
        hp = int(hp_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("怪物属性无效") from exc

    entry = {
        "id": monster_id,
        "name": name,
        "dex": dex,
        "hp": hp,
        "max_hp": hp,
        "alive": True,
        "dead": False,
        "skills": skills,
    }
    if template_id:
        entry["template_id"] = template_id
    return entry


def writeback_combat_hp_to_pcs(combat, db):
    applied = []
    skipped = []
    changed = False
    for player in combat.get("players", []):
        username = player.get("username")
        name = player.get("name") or username
        pc_id = player.get("pc_id")
        if not pc_id:
            skipped.append(
                {"username": username, "name": name, "reason": "无人物卡 ID"}
            )
            continue
        pc, _, _ = find_pc(db, pc_id)
        if not pc:
            skipped.append(
                {
                    "username": username,
                    "name": name,
                    "pc_id": pc_id,
                    "reason": "人物卡不存在",
                }
            )
            continue
        try:
            new_hp = max(0, int(player.get("hp", 0) or 0))
        except (TypeError, ValueError):
            skipped.append(
                {"username": username, "name": name, "pc_id": pc_id, "reason": "HP 无效"}
            )
            continue
        ensure_status_max(pc)
        pc.setdefault("status", {})["hp"] = new_hp
        changed = True
        applied.append(
            {
                "username": username,
                "name": name,
                "pc_id": pc_id,
                "hp": new_hp,
            }
        )
    if changed:
        save_characters_db(db)
    return {"applied": applied, "skipped": skipped}


def apply_hp_delta(entity, delta):
    delta = int(delta)
    current = int(entity.get("hp", 0) or 0)
    max_hp = int(entity.get("max_hp", current) or current)
    new_hp = max(0, current + delta)
    entity["hp"] = new_hp
    entity["max_hp"] = max(max_hp, new_hp)
    entity["alive"] = new_hp > 0
    return new_hp


def mark_monster_dead(monster):
    monster["hp"] = 0
    monster["alive"] = False
    monster["dead"] = True


def append_combat_log(combat, message):
    log = combat.setdefault("log", [])
    entry = {
        "id": len(log) + 1,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    log.append(entry)
    if len(log) > 100:
        combat["log"] = log[-100:]
    return entry


def normalize_combat_turn_index(combat):
    turn_order = combat.get("turn_order", [])
    if not turn_order:
        combat["current_turn_index"] = 0
        return
    index = int(combat.get("current_turn_index", 0) or 0)
    if index >= len(turn_order):
        combat["current_turn_index"] = 0


def refresh_combat_state(combat):
    combat["turn_order"] = build_turn_order(combat.get("players", []), combat.get("monsters", []))
    normalize_combat_turn_index(combat)
    combat["updated_at"] = datetime.now(timezone.utc).isoformat()


def combat_to_client(combat):
    if not combat or not combat.get("active"):
        return None
    turn_order = combat.get("turn_order", [])
    current_index = int(combat.get("current_turn_index", 0) or 0)
    current_actor = turn_order[current_index] if turn_order else None
    return {
        "active": True,
        "map": combat.get("map"),
        "map_label": combat.get("map_label"),
        "players": combat.get("players", []),
        "monsters": combat.get("monsters", []),
        "turn_order": turn_order,
        "current_turn_index": current_index,
        "current_actor": current_actor,
        "log": combat.get("log", [])[-30:],
        "updated_at": combat.get("updated_at"),
    }


def get_room_combat(room):
    combat = room.get("combat")
    if combat and combat.get("active"):
        return combat
    return None


def user_in_combat(combat, username):
    if not combat:
        return False
    return any(player.get("username") == username for player in combat.get("players", []))


def require_room_kp(user, room_id):
    room = rooms.get(room_id)
    if not room:
        return None, "房间不存在"
    if room.get("kp") != user:
        return None, "仅房间 KP 可操作"
    return room, None


SCENES_DIR = os.path.join(DIRECTORY, "assets", "scenes")
HANDOUTS_DIR = os.path.join(DIRECTORY, "assets", "handouts")
SCENE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SCENE_MAX_BYTES = 5 * 1024 * 1024
HANDOUT_TITLE_MAX = 80
HANDOUT_BODY_MAX = 8000
SCENE_MIME_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def ensure_room_scene(room):
    return room.setdefault("scene", {"image": None, "tokens": [], "updated_at": None})


def clamp_coord(value, default=0.5):
    try:
        coord = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, coord))


def scene_to_client(scene):
    if not scene:
        return None
    image = scene.get("image")
    return {
        "image": f"/assets/{image}" if image else None,
        "tokens": list(scene.get("tokens", [])),
        "updated_at": scene.get("updated_at"),
    }


def user_in_room(room, username):
    return room.get("kp") == username or username in room.get("players", [])


def get_pc_label_for_pl(db, pl_username):
    pc = get_active_pc_for_pl(db, pl_username)
    if not pc:
        return pl_username
    return pc.get("profile", {}).get("name") or pl_username


def detect_scene_ext(filename, raw):
    ext = Path(filename or "").suffix.lower()
    if ext in SCENE_IMAGE_EXTS:
        return ext
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None


def delete_room_scene_files(room_id):
    room_dir = os.path.join(SCENES_DIR, room_id)
    if os.path.isdir(room_dir):
        for old_name in os.listdir(room_dir):
            old_path = os.path.join(room_dir, old_name)
            if os.path.isfile(old_path):
                os.remove(old_path)


def get_kp_room_ids(username):
    return [room_id for room_id, room in rooms.items() if room.get("kp") == username]


def clear_room_member_sessions(room):
    kp = room.get("kp")
    if kp:
        user_rooms.pop(kp, None)
    for player in room.get("players", []):
        user_rooms.pop(player, None)


def save_scene_image(room_id, filename, raw):
    ext = detect_scene_ext(filename, raw)
    if not ext:
        return None, "请上传 png、jpg 或 webp 格式的场景图"
    if len(raw) > SCENE_MAX_BYTES:
        return None, "图片过大，请上传 5MB 以内的场景图"
    room_dir = os.path.join(SCENES_DIR, room_id)
    os.makedirs(room_dir, exist_ok=True)
    for old_name in os.listdir(room_dir):
        old_path = os.path.join(room_dir, old_name)
        if os.path.isfile(old_path):
            os.remove(old_path)
    file_name = f"scene{ext}"
    file_path = os.path.join(room_dir, file_name)
    with open(file_path, "wb") as handle:
        handle.write(raw)
    rel_path = f"scenes/{room_id}/{file_name}"
    return rel_path, None


def ensure_room_handouts(room):
    room.setdefault("handouts", [])
    room.setdefault("next_handout_id", 1)
    room.setdefault("last_handout_id", 0)
    room.setdefault("handout_reads", {})
    return room


def handout_visible_to(handout, username, is_kp):
    if is_kp:
        return True
    if handout.get("revoked"):
        return False
    targets = handout.get("targets") or []
    return "*" in targets or username in targets


def handout_to_client(handout, username, room):
    reads = room.get("handout_reads", {}).get(username, [])
    image = handout.get("image")
    return {
        "id": handout.get("id"),
        "title": handout.get("title", ""),
        "body": handout.get("body", ""),
        "image": f"/assets/{image}" if image else None,
        "targets": list(handout.get("targets") or []),
        "created_at": handout.get("created_at"),
        "created_by": handout.get("created_by"),
        "revoked": bool(handout.get("revoked")),
        "read": handout.get("id") in reads,
    }


def mark_handout_read(room, username, handout_id):
    ensure_room_handouts(room)
    reads = room.setdefault("handout_reads", {}).setdefault(username, [])
    if handout_id not in reads:
        reads.append(handout_id)


def delete_room_handout_files(room_id):
    room_dir = os.path.join(HANDOUTS_DIR, room_id)
    if os.path.isdir(room_dir):
        for old_name in os.listdir(room_dir):
            old_path = os.path.join(room_dir, old_name)
            if os.path.isfile(old_path):
                os.remove(old_path)
        try:
            os.rmdir(room_dir)
        except OSError:
            pass


def save_handout_image(room_id, handout_id, filename, raw):
    ext = detect_scene_ext(filename, raw)
    if not ext:
        return None, "请上传 png、jpg 或 webp 格式的线索图"
    if len(raw) > SCENE_MAX_BYTES:
        return None, "图片过大，请上传 5MB 以内的线索图"
    room_dir = os.path.join(HANDOUTS_DIR, room_id)
    os.makedirs(room_dir, exist_ok=True)
    file_name = f"h{handout_id}{ext}"
    file_path = os.path.join(room_dir, file_name)
    with open(file_path, "wb") as handle:
        handle.write(raw)
    rel_path = f"handouts/{room_id}/{file_name}"
    return rel_path, None


def normalize_handout_targets(raw_targets, room):
    if raw_targets == "*" or raw_targets == ["*"]:
        return ["*"], None
    if not isinstance(raw_targets, list) or not raw_targets:
        return None, "请指定发放对象"
    players = set(room.get("players", []))
    normalized = []
    for item in raw_targets:
        name = normalize_username(str(item))
        if not name:
            continue
        if name not in players:
            return None, f"玩家 {name} 不在房间内"
        normalized.append(name)
    if not normalized:
        return None, "请指定发放对象"
    return normalized, None


def token_to_client(token):
    return {
        "id": token.get("id"),
        "type": token.get("type"),
        "label": token.get("label"),
        "x": token.get("x"),
        "y": token.get("y"),
        "pl_username": token.get("pl_username"),
    }


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


def get_session(handler):
    session_id = get_session_id(handler)
    if not session_id:
        return None
    data = sessions.get(session_id)
    if isinstance(data, str):
        return {"username": data, "active_pc_id": None}
    return data


def get_current_user(handler):
    session = get_session(handler)
    return session.get("username") if session else None


def get_session_active_pc_id(handler):
    session = get_session(handler)
    if not session:
        return None
    return session.get("active_pc_id")


def set_session_active_pc_id(handler, pc_id):
    session_id = get_session_id(handler)
    if not session_id or session_id not in sessions:
        return False
    data = sessions[session_id]
    if isinstance(data, str):
        sessions[session_id] = {"username": data, "active_pc_id": pc_id}
    else:
        data["active_pc_id"] = pc_id
    return True


def create_session(username):
    session_id = secrets.token_urlsafe(32)
    db = load_characters_db()
    account = get_account(db, username)
    active_pc_id = account.get("active_pc_id") if account else None
    session_data = {"username": username, "active_pc_id": active_pc_id}
    sessions[session_id] = session_data
    cookie = (
        f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
    )
    return cookie, session_data


def user_pc_payload(username, role, active_pc_id=None):
    payload = {
        "active_pc_id": active_pc_id,
        "active_pc": None,
        "needs_pc_selection": False,
    }
    if role != ROLE_PLAYER:
        return payload
    db = load_characters_db()
    pc = get_active_pc_for_pl(db, username, active_pc_id)
    if pc:
        payload["active_pc"] = pc_summary(pc)
    else:
        payload["needs_pc_selection"] = True
    return payload


def ensure_status_max(character):
    status = character.setdefault("status", {})
    changed = False
    try:
        hp = int(status.get("hp", 0) or 0)
    except (TypeError, ValueError):
        hp = 0
    try:
        san = int(status.get("san", 0) or 0)
    except (TypeError, ValueError):
        san = 0

    if "hp_max" not in status:
        status["hp_max"] = hp
        changed = True
    if "san_max" not in status:
        status["san_max"] = max(san, 99)
        changed = True

    try:
        hp_max = int(status.get("hp_max", hp) or hp)
    except (TypeError, ValueError):
        hp_max = hp
    if hp_max < hp:
        status["hp_max"] = hp
        changed = True

    try:
        san_max = int(status.get("san_max", 99) or 99)
    except (TypeError, ValueError):
        san_max = 99
    if san_max < san:
        status["san_max"] = san
        changed = True

    return changed


def status_display_values(status):
    hp = status.get("hp")
    san = status.get("san")
    hp_max = status.get("hp_max", hp)
    san_max = status.get("san_max", 99)
    return {
        "hp": hp,
        "hp_max": hp_max,
        "san": san,
        "san_max": san_max,
    }


def pc_summary(pc):
    profile = pc.get("profile", {})
    status = pc.get("status", {})
    stats = status_display_values(status)
    return {
        "pc_id": pc.get("pc_id"),
        "owner": pc.get("owner"),
        "pl_username": pc.get("owner"),
        "name": profile.get("name") or pc.get("pc_id"),
        "occupation": profile.get("occupation") or "",
        "hp": stats["hp"],
        "hp_max": stats["hp_max"],
        "san": stats["san"],
        "san_max": stats["san_max"],
        "created_at": pc.get("created_at"),
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
                room_id = get_user_room_id(user)
                active_pc_id = get_session_active_pc_id(self)
                payload = {
                    "logged_in": True,
                    "username": user,
                    "role": role,
                    **user_pc_payload(user, role, active_pc_id),
                }
                if room_id and room_id in rooms:
                    payload["room"] = room_summary(room_id, rooms[room_id])
                else:
                    payload["room"] = None
                json_response(self, 200, payload)
            else:
                json_response(self, 200, {"logged_in": False})
            return

        if path == "/api/pcs":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            if role != ROLE_PLAYER:
                json_response(self, 403, {"ok": False, "error": "仅玩家账号管理人物卡"})
                return
            db = load_characters_db()
            account = get_account(db, user)
            pcs = []
            active_pc_id = get_session_active_pc_id(self) or (account or {}).get("active_pc_id")
            if account:
                for pc in account.get("pcs", {}).values():
                    ensure_status_max(pc)
                    summary = pc_summary(pc)
                    summary["is_active"] = summary["pc_id"] == active_pc_id
                    pcs.append(summary)
            pcs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "pl_username": user,
                    "active_pc_id": active_pc_id,
                    "pcs": pcs,
                },
            )
            return

        if path == "/api/rooms":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            room_list = [
                room_summary(room_id, room)
                for room_id, room in rooms.items()
            ]
            room_list.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "rooms": room_list,
                    "can_create": can_be_room_kp(role),
                    "current_room_id": get_user_room_id(user),
                    "my_kp_room_ids": get_kp_room_ids(user) if can_be_room_kp(role) else [],
                },
            )
            return

        if path == "/api/room":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 200, {"ok": True, "in_room": False})
                return
            role = get_user_role(user)
            room = rooms[room_id]
            is_kp = room.get("kp") == user
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "in_room": True,
                    "room": room_summary(room_id, room),
                    "is_kp": is_kp,
                    "can_secret_roll": can_be_room_kp(role) and is_kp,
                    "last_roll_id": room.get("last_roll_id", 0),
                    "last_handout_id": ensure_room_handouts(room).get("last_handout_id", 0),
                },
            )
            return

        if path == "/api/handouts":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "尚未加入房间"})
                return
            role = get_user_role(user)
            room = rooms[room_id]
            is_kp = room.get("kp") == user
            since = 0
            try:
                since = int(query.get("since", ["0"])[0])
            except (TypeError, ValueError):
                since = 0
            ensure_room_handouts(room)
            visible = []
            for handout in room.get("handouts", []):
                if handout.get("id", 0) <= since:
                    continue
                if not handout_visible_to(handout, user, is_kp):
                    continue
                visible.append(handout_to_client(handout, user, room))
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "handouts": visible,
                    "last_handout_id": room.get("last_handout_id", 0),
                },
            )
            return

        if path == "/api/room/rolls":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "尚未加入房间"})
                return
            role = get_user_role(user)
            since = 0
            try:
                since = int(query.get("since", ["0"])[0])
            except (TypeError, ValueError):
                since = 0
            visible = []
            for roll in rooms[room_id].get("rolls", []):
                if roll["id"] <= since:
                    continue
                visible.append(filter_roll_for_user(roll, user, role))
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "rolls": visible,
                    "last_roll_id": rooms[room_id].get("last_roll_id", 0),
                },
            )
            return

        if path == "/api/combat":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "请先加入房间"})
                return
            room = rooms[room_id]
            role = get_user_role(user)
            is_kp = room.get("kp") == user
            combat = get_room_combat(room)
            can_view = is_kp or user_in_combat(combat, user)
            payload = {
                "ok": True,
                "is_kp": is_kp,
                "can_manage": is_kp,
                "can_view": can_view,
                "maps": MAP_LOCATIONS,
                "combat": combat_to_client(combat) if can_view else None,
            }
            if is_kp and not combat:
                payload["room_players"] = list(room.get("players", []))
            if is_kp:
                payload["monster_templates"] = monsters_to_client()
            json_response(self, 200, payload)
            return

        if path == "/api/monsters":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            json_response(self, 200, {"ok": True, "monsters": monsters_to_client()})
            return

        if path == "/api/scene":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "请先加入房间"})
                return
            room = rooms[room_id]
            if not user_in_room(room, user):
                json_response(self, 403, {"ok": False, "error": "不在此房间"})
                return
            is_kp = room.get("kp") == user
            scene = room.get("scene")
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "is_kp": is_kp,
                    "can_manage": is_kp,
                    "scene": scene_to_client(scene),
                },
            )
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
            with data_lock:
                db = load_characters_db()
                changed = False
                players = []
                for pl_username, pc in iter_all_pcs(db):
                    if ensure_status_max(pc):
                        changed = True
                    summary = pc_summary(pc)
                    summary["username"] = pl_username
                    players.append(summary)
                if changed:
                    save_characters_db(db)
            players.sort(key=lambda item: (item.get("owner") or "", item.get("name") or ""))
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
            target_pc_id = query.get("target", [""])[0].strip()
            with data_lock:
                db = load_characters_db()
                if can_view_all_characters(role) and target_pc_id:
                    pc, _, owner = find_pc(db, target_pc_id)
                    if not pc:
                        json_response(self, 404, {"ok": False, "error": "人物卡不存在"})
                        return
                else:
                    if role != ROLE_PLAYER:
                        json_response(self, 400, {"ok": False, "error": "请先选择调查员人物卡"})
                        return
                    active_pc_id = get_session_active_pc_id(self)
                    pc = get_active_pc_for_pl(db, user, active_pc_id)
                    owner = user
                    if not pc:
                        json_response(self, 404, {"ok": False, "error": "请先选择或新建人物卡"})
                        return
                    target_pc_id = pc.get("pc_id")
                if ensure_status_max(pc):
                    save_characters_db(db)

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "character": pc,
                    "target": target_pc_id,
                    "owner": owner,
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

        if path == "/api/rooms":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            if not can_be_room_kp(role):
                json_response(self, 403, {"ok": False, "error": "仅 KP 可创建房间"})
                return
            if get_user_room_id(user):
                json_response(self, 400, {"ok": False, "error": "请先离开当前房间"})
                return

            name, error = validate_room_name(body.get("name", ""))
            if error:
                json_response(self, 400, {"ok": False, "error": error})
                return

            with data_lock:
                room_id = secrets.token_urlsafe(6)
                while room_id in rooms:
                    room_id = secrets.token_urlsafe(6)
                rooms[room_id] = {
                    "name": name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "kp": user,
                    "players": [],
                    "rolls": [],
                    "last_roll_id": 0,
                    "next_roll_id": 1,
                }
                user_rooms[user] = room_id
                save_rooms()

            json_response(
                self,
                200,
                {"ok": True, "room": room_summary(room_id, rooms[room_id])},
            )
            return

        if path == "/api/room/join":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            if can_be_room_kp(role):
                json_response(self, 403, {"ok": False, "error": "KP 请创建房间，不能作为玩家加入"})
                return
            if get_user_room_id(user):
                json_response(self, 400, {"ok": False, "error": "请先离开当前房间"})
                return

            room_id = normalize_username(body.get("room_id", ""))
            if not room_id or room_id not in rooms:
                json_response(self, 404, {"ok": False, "error": "房间不存在"})
                return

            with data_lock:
                room = rooms[room_id]
                if user in room.get("players", []):
                    json_response(self, 400, {"ok": False, "error": "已在房间中"})
                    return
                room.setdefault("players", []).append(user)
                user_rooms[user] = room_id
                save_rooms()

            json_response(
                self,
                200,
                {"ok": True, "room": room_summary(room_id, rooms[room_id])},
            )
            return

        if path == "/api/room/leave":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return

            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "当前不在任何房间"})
                return

            was_kp = False
            with data_lock:
                room = rooms[room_id]
                if room.get("kp") == user:
                    was_kp = True
                    user_rooms.pop(user, None)
                else:
                    players = room.get("players", [])
                    if user in players:
                        players.remove(user)
                    user_rooms.pop(user, None)
                    if not room.get("kp") and not room.get("players"):
                        rooms.pop(room_id, None)
                save_rooms()

            json_response(self, 200, {"ok": True, "left": True, "was_kp": was_kp})
            return

        if path == "/api/room/rejoin":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            role = get_user_role(user)
            if not can_be_room_kp(role):
                json_response(self, 403, {"ok": False, "error": "仅 KP 可回到自己创建的房间"})
                return
            if get_user_room_id(user):
                json_response(self, 400, {"ok": False, "error": "请先离开当前房间"})
                return

            room_id = normalize_username(str(body.get("room_id", "")))
            if not room_id or room_id not in rooms:
                json_response(self, 404, {"ok": False, "error": "房间不存在"})
                return

            room = rooms[room_id]
            if room.get("kp") != user:
                json_response(self, 403, {"ok": False, "error": "只能回到自己创建的房间"})
                return

            with data_lock:
                user_rooms[user] = room_id

            json_response(
                self,
                200,
                {"ok": True, "room": room_summary(room_id, rooms[room_id])},
            )
            return

        if path == "/api/room/delete":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return

            room_id = normalize_username(str(body.get("room_id", "")))
            if not room_id or room_id not in rooms:
                json_response(self, 404, {"ok": False, "error": "房间不存在"})
                return

            with data_lock:
                room = rooms[room_id]
                if room.get("kp") != user:
                    json_response(self, 403, {"ok": False, "error": "仅房间 KP 可解散房间"})
                    return
                clear_room_member_sessions(room)
                delete_room_scene_files(room_id)
                delete_room_handout_files(room_id)
                del rooms[room_id]
                save_rooms()

            json_response(self, 200, {"ok": True})
            return

        if path == "/api/room/roll":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return

            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "请先加入房间"})
                return

            role = get_user_role(user)
            room = rooms[room_id]
            is_kp = room.get("kp") == user
            is_player = user in room.get("players", [])
            if not is_kp and not is_player:
                json_response(self, 403, {"ok": False, "error": "不在此房间"})
                return

            dice_key = body.get("dice", "d100")
            if dice_key not in DICE_SIDES:
                json_response(self, 400, {"ok": False, "error": "无效骰子类型"})
                return

            hidden = bool(body.get("hidden", False))
            if hidden:
                if not (can_be_room_kp(role) and is_kp):
                    json_response(self, 403, {"ok": False, "error": "仅 KP 可暗骰"})
                    return

            roll_type = body.get("type", "free")
            if roll_type in ("skill", "attr"):
                label = str(body.get("label", "")).strip()
                if not label:
                    json_response(self, 400, {"ok": False, "error": "请指定检定名称"})
                    return
                try:
                    skill_value = int(body.get("skill_value"))
                except (TypeError, ValueError):
                    json_response(self, 400, {"ok": False, "error": "鉴定值无效"})
                    return
            else:
                label = body.get("label") or f"1{dice_key[1:].upper()}"
                skill_value = None

            value = roll_die(dice_key)
            if value is None:
                json_response(self, 400, {"ok": False, "error": "掷骰失败"})
                return

            roll_record = {
                "dice": dice_key,
                "label": label,
                "value": value,
                "hidden": hidden,
                "roller": user,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if roll_type in ("skill", "attr") and dice_key == "d100":
                outcome = evaluate_skill_check(value, skill_value)
                roll_record["check_type"] = outcome["type"]
                roll_record["check_label"] = outcome["label"]
                roll_record["skill_value"] = outcome["skill_value"]

            with data_lock:
                room = rooms[room_id]
                roll_id = room.get("next_roll_id", 1)
                roll_record["id"] = roll_id
                room["next_roll_id"] = roll_id + 1
                room["last_roll_id"] = roll_id
                room.setdefault("rolls", []).append(roll_record)
                if len(room["rolls"]) > 200:
                    room["rolls"] = room["rolls"][-200:]
                save_rooms()

            json_response(self, 200, {"ok": True, "roll": roll_to_client(roll_record)})
            return

        if path == "/api/combat/start":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            use_scene_map = bool(body.get("use_scene_map"))
            map_key = str(body.get("map", "")).strip()
            if use_scene_map:
                scene = room.get("scene")
                if not scene or not scene.get("image"):
                    json_response(self, 400, {"ok": False, "error": "请先设置场景地图"})
                    return
                map_key = "scene"
                map_label = "当前场景"
            else:
                if map_key not in MAP_LOCATIONS:
                    json_response(self, 400, {"ok": False, "error": "请选择有效地图"})
                    return
                map_label = MAP_LOCATIONS[map_key]

            selected_players = body.get("players", [])
            if not isinstance(selected_players, list) or not selected_players:
                json_response(self, 400, {"ok": False, "error": "请选择参战玩家"})
                return

            room_players = set(room.get("players", []))
            monsters_input = body.get("monsters", [])
            if not isinstance(monsters_input, list) or not monsters_input:
                json_response(self, 400, {"ok": False, "error": "请至少创建一个怪物"})
                return

            with data_lock:
                db = load_characters_db()
                combat_players = []
                for username in selected_players:
                    username = normalize_username(str(username))
                    if username not in room_players:
                        json_response(self, 400, {"ok": False, "error": f"玩家 {username} 不在房间中"})
                        return
                    stats = get_player_combat_stats(username, db)
                    if not stats:
                        json_response(
                            self,
                            400,
                            {"ok": False, "error": f"玩家 {username} 尚未选择调查员人物卡（PC）"},
                        )
                        return
                    combat_players.append(stats)

            monsters = []
            next_monster_id = 1
            for item in monsters_input:
                monster_id = f"m{next_monster_id}"
                try:
                    monster = build_combat_monster_entry(monster_id, item)
                except ValueError:
                    json_response(self, 400, {"ok": False, "error": "怪物属性无效"})
                    return
                if not monster.get("name"):
                    json_response(self, 400, {"ok": False, "error": "怪物名称不能为空"})
                    return
                if int(monster.get("hp", 0) or 0) <= 0:
                    json_response(self, 400, {"ok": False, "error": "怪物血量必须大于 0"})
                    return
                next_monster_id += 1
                monsters.append(monster)

            with data_lock:
                room = rooms[room_id]
                combat = {
                    "active": True,
                    "map": map_key,
                    "map_label": map_label,
                    "players": combat_players,
                    "monsters": monsters,
                    "current_turn_index": 0,
                    "log": [],
                    "next_monster_id": next_monster_id,
                }
                refresh_combat_state(combat)
                append_combat_log(
                    combat,
                    f"战斗开始 · 地图 {map_label} · 参战玩家 {len(combat_players)} 人 · 怪物 {len(monsters)} 只",
                )
                room["combat"] = combat
                save_rooms()

            json_response(self, 200, {"ok": True, "combat": combat_to_client(room["combat"])})
            return

        if path == "/api/combat/end":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            writeback_hp = bool(body.get("writeback_hp"))
            writeback = None
            with data_lock:
                room = rooms[room_id]
                combat = get_room_combat(room)
                if writeback_hp:
                    if not combat:
                        json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                        return
                    db = load_characters_db()
                    writeback = writeback_combat_hp_to_pcs(combat, db)
                room.pop("combat", None)
                save_rooms()

            json_response(self, 200, {"ok": True, "writeback": writeback})
            return

        if path == "/api/combat/monster":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            combat = get_room_combat(room)
            if not combat:
                json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                return

            with data_lock:
                room = rooms[room_id]
                combat = room["combat"]
                next_id = int(combat.get("next_monster_id", 1))
                try:
                    monster = build_combat_monster_entry(f"m{next_id}", body)
                except ValueError:
                    json_response(self, 400, {"ok": False, "error": "怪物属性无效"})
                    return
                if not monster.get("name"):
                    json_response(self, 400, {"ok": False, "error": "怪物名称不能为空"})
                    return
                if int(monster.get("hp", 0) or 0) <= 0:
                    json_response(self, 400, {"ok": False, "error": "怪物血量必须大于 0"})
                    return
                combat["next_monster_id"] = next_id + 1
                combat.setdefault("monsters", []).append(monster)
                refresh_combat_state(combat)
                append_combat_log(
                    combat,
                    f"新增怪物 {monster['name']}（敏捷 {monster['dex']} · HP {monster['hp']}）",
                )
                save_rooms()

            json_response(self, 200, {"ok": True, "combat": combat_to_client(room["combat"])})
            return

        if path == "/api/combat/skill-roll":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            combat = get_room_combat(room)
            if not combat:
                json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                return

            monster_id = str(body.get("monster_id", "")).strip()
            skill_name = str(body.get("skill_name", "")).strip()
            if not monster_id or not skill_name:
                json_response(self, 400, {"ok": False, "error": "请指定怪物与技能"})
                return

            with data_lock:
                room = rooms[room_id]
                combat = room["combat"]
                monster = next(
                    (item for item in combat.get("monsters", []) if item.get("id") == monster_id),
                    None,
                )
                if not monster:
                    json_response(self, 404, {"ok": False, "error": "怪物不存在"})
                    return
                skill = next(
                    (item for item in monster.get("skills", []) if item.get("name") == skill_name),
                    None,
                )
                if not skill:
                    json_response(self, 404, {"ok": False, "error": "技能不存在"})
                    return

                try:
                    skill_value = int(skill.get("value", 0))
                except (TypeError, ValueError):
                    json_response(self, 400, {"ok": False, "error": "技能值无效"})
                    return

                roll_value = roll_die("d100")
                if roll_value is None:
                    json_response(self, 400, {"ok": False, "error": "掷骰失败"})
                    return

                outcome = evaluate_skill_check(roll_value, skill_value)
                monster_name = monster.get("name") or monster_id
                message = (
                    f"{monster_name} · {skill_name}检定：{roll_value} / {skill_value} → "
                    f"{outcome['label']}"
                )
                append_combat_log(combat, message)
                combat["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "roll": {
                        "monster_id": monster_id,
                        "monster_name": monster_name,
                        "skill_name": skill_name,
                        "value": roll_value,
                        "skill_value": outcome["skill_value"],
                        "check_type": outcome["type"],
                        "check_label": outcome["label"],
                    },
                    "combat": combat_to_client(room["combat"]),
                },
            )
            return

        if path == "/api/combat/hp":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            combat = get_room_combat(room)
            if not combat:
                json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                return

            target_type = body.get("target_type")
            target_id = str(body.get("target_id", "")).strip()
            try:
                delta = int(body.get("delta", 0))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "调整值无效"})
                return
            if delta == 0:
                json_response(self, 400, {"ok": False, "error": "调整值不能为 0"})
                return

            with data_lock:
                room = rooms[room_id]
                combat = room["combat"]
                label = ""
                if target_type == "player":
                    entity = next((p for p in combat.get("players", []) if p.get("username") == target_id), None)
                    if not entity:
                        json_response(self, 404, {"ok": False, "error": "玩家不存在"})
                        return
                    new_hp = apply_hp_delta(entity, delta)
                    label = entity.get("name") or target_id
                elif target_type == "monster":
                    entity = next((m for m in combat.get("monsters", []) if m.get("id") == target_id), None)
                    if not entity:
                        json_response(self, 404, {"ok": False, "error": "怪物不存在"})
                        return
                    new_hp = apply_hp_delta(entity, delta)
                    if new_hp <= 0:
                        mark_monster_dead(entity)
                    label = entity.get("name") or target_id
                else:
                    json_response(self, 400, {"ok": False, "error": "无效目标类型"})
                    return

                refresh_combat_state(combat)
                sign = f"+{delta}" if delta > 0 else str(delta)
                max_hp = entity.get("max_hp", entity["hp"])
                append_combat_log(combat, f"{label} HP {sign} → {entity['hp']}/{max_hp}")
                save_rooms()

            json_response(self, 200, {"ok": True, "combat": combat_to_client(room["combat"])})
            return

        if path == "/api/combat/kill":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            combat = get_room_combat(room)
            if not combat:
                json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                return

            monster_id = str(body.get("monster_id", "")).strip()
            with data_lock:
                room = rooms[room_id]
                combat = room["combat"]
                monster = next((m for m in combat.get("monsters", []) if m.get("id") == monster_id), None)
                if not monster:
                    json_response(self, 404, {"ok": False, "error": "怪物不存在"})
                    return
                name = monster.get("name") or monster_id
                mark_monster_dead(monster)
                refresh_combat_state(combat)
                append_combat_log(combat, f"{name} 被 KP 标记死亡")
                save_rooms()

            json_response(self, 200, {"ok": True, "combat": combat_to_client(room["combat"])})
            return

        if path == "/api/combat/next-turn":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            combat = get_room_combat(room)
            if not combat:
                json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                return

            with data_lock:
                room = rooms[room_id]
                combat = room["combat"]
                refresh_combat_state(combat)
                turn_order = combat.get("turn_order", [])
                if not turn_order:
                    json_response(self, 400, {"ok": False, "error": "没有可行动单位"})
                    return
                combat["current_turn_index"] = (int(combat.get("current_turn_index", 0)) + 1) % len(turn_order)
                actor = turn_order[combat["current_turn_index"]]
                append_combat_log(combat, f"轮到 {actor['name']} 行动")
                combat["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(self, 200, {"ok": True, "combat": combat_to_client(room["combat"])})
            return

        if path == "/api/combat/random-target":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            combat = get_room_combat(room)
            if not combat:
                json_response(self, 400, {"ok": False, "error": "当前没有进行中的战斗"})
                return

            monster_id = str(body.get("monster_id", "")).strip()
            with data_lock:
                room = rooms[room_id]
                combat = room["combat"]
                monster = next((m for m in combat.get("monsters", []) if m.get("id") == monster_id), None)
                if not monster or not monster.get("alive", True) or monster.get("dead", False):
                    json_response(self, 400, {"ok": False, "error": "怪物不存在或已死亡"})
                    return

                alive_players = [p for p in combat.get("players", []) if p.get("alive", True) and int(p.get("hp", 0) or 0) > 0]
                if not alive_players:
                    json_response(self, 400, {"ok": False, "error": "没有可攻击的存活玩家"})
                    return

                target = random.choice(alive_players)
                monster_name = monster.get("name") or monster_id
                target_name = target.get("name") or target.get("username")
                message = f"{monster_name} 随机攻击 → {target_name}"
                append_combat_log(combat, message)
                combat["last_target"] = {
                    "monster_id": monster_id,
                    "monster_name": monster_name,
                    "target_username": target.get("username"),
                    "target_name": target_name,
                }
                combat["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "target": combat["last_target"],
                    "combat": combat_to_client(room["combat"]),
                },
            )
            return

        if path == "/api/handouts":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            title = str(body.get("title", "")).strip()
            handout_body = str(body.get("body", "")).strip()
            if not title:
                json_response(self, 400, {"ok": False, "error": "请输入线索标题"})
                return
            if len(title) > HANDOUT_TITLE_MAX:
                json_response(self, 400, {"ok": False, "error": f"标题不能超过 {HANDOUT_TITLE_MAX} 字"})
                return
            if len(handout_body) > HANDOUT_BODY_MAX:
                json_response(self, 400, {"ok": False, "error": f"正文不能超过 {HANDOUT_BODY_MAX} 字"})
                return

            targets, target_error = normalize_handout_targets(body.get("targets"), room)
            if target_error:
                json_response(self, 400, {"ok": False, "error": target_error})
                return

            file_b64 = body.get("image_b64", "")
            filename = str(body.get("filename", "handout.png")).strip() or "handout.png"
            image_raw = None
            if file_b64:
                try:
                    image_raw = base64.b64decode(file_b64, validate=True)
                except (binascii.Error, ValueError):
                    json_response(self, 400, {"ok": False, "error": "图片内容无效"})
                    return

            with data_lock:
                room = rooms[room_id]
                ensure_room_handouts(room)
                handout_id = room["next_handout_id"]
                room["next_handout_id"] = handout_id + 1
                handout = {
                    "id": handout_id,
                    "title": title,
                    "body": handout_body,
                    "image": None,
                    "targets": targets,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "created_by": user,
                    "revoked": False,
                }
                if image_raw:
                    rel_path, upload_error = save_handout_image(room_id, handout_id, filename, image_raw)
                    if upload_error:
                        json_response(self, 400, {"ok": False, "error": upload_error})
                        return
                    handout["image"] = rel_path
                room["handouts"].append(handout)
                room["last_handout_id"] = handout_id
                save_rooms()
                saved = handout_to_client(handout, user, room)

            json_response(self, 200, {"ok": True, "handout": saved})
            return

        if path == "/api/handouts/revoke":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            try:
                handout_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效的线索 ID"})
                return

            with data_lock:
                room = rooms[room_id]
                ensure_room_handouts(room)
                handout = next((item for item in room.get("handouts", []) if item.get("id") == handout_id), None)
                if not handout:
                    json_response(self, 404, {"ok": False, "error": "线索不存在"})
                    return
                handout["revoked"] = True
                save_rooms()

            json_response(self, 200, {"ok": True})
            return

        if path == "/api/handouts/read":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            if not room_id or room_id not in rooms:
                json_response(self, 400, {"ok": False, "error": "尚未加入房间"})
                return

            try:
                handout_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效的线索 ID"})
                return

            with data_lock:
                room = rooms[room_id]
                ensure_room_handouts(room)
                handout = next((item for item in room.get("handouts", []) if item.get("id") == handout_id), None)
                if not handout:
                    json_response(self, 404, {"ok": False, "error": "线索不存在"})
                    return
                is_kp = room.get("kp") == user
                if not handout_visible_to(handout, user, is_kp):
                    json_response(self, 403, {"ok": False, "error": "无权查看该线索"})
                    return
                mark_handout_read(room, user, handout_id)
                save_rooms()

            json_response(self, 200, {"ok": True})
            return

        if path == "/api/scene/image":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            filename = str(body.get("filename", "scene.png")).strip() or "scene.png"
            file_b64 = body.get("file", "")
            if not file_b64:
                json_response(self, 400, {"ok": False, "error": "未收到图片内容"})
                return
            try:
                raw = base64.b64decode(file_b64, validate=True)
            except (binascii.Error, ValueError):
                json_response(self, 400, {"ok": False, "error": "图片内容无效"})
                return

            rel_path, upload_error = save_scene_image(room_id, filename, raw)
            if upload_error:
                json_response(self, 400, {"ok": False, "error": upload_error})
                return

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                scene["image"] = rel_path
                scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(self, 200, {"ok": True, "scene": scene_to_client(room["scene"])})
            return

        if path == "/api/scene/token":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            token_id = str(body.get("id", "")).strip()
            token_type = str(body.get("type", "npc")).strip()
            label = str(body.get("label", "")).strip()
            pl_username = normalize_username(str(body.get("pl_username", "")))
            x = clamp_coord(body.get("x", 0.5))
            y = clamp_coord(body.get("y", 0.5))

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                tokens = scene.setdefault("tokens", [])

                if token_id:
                    token = next((item for item in tokens if item.get("id") == token_id), None)
                    if not token:
                        json_response(self, 404, {"ok": False, "error": "棋子不存在"})
                        return
                    token["x"] = x
                    token["y"] = y
                    if label:
                        token["label"] = label
                else:
                    if token_type not in ("pc", "npc"):
                        json_response(self, 400, {"ok": False, "error": "无效棋子类型"})
                        return
                    if token_type == "pc":
                        if not pl_username or pl_username not in room.get("players", []):
                            json_response(self, 400, {"ok": False, "error": "请指定房间内的玩家"})
                            return
                        db = load_characters_db()
                        label = label or get_pc_label_for_pl(db, pl_username)
                    else:
                        if not label:
                            json_response(self, 400, {"ok": False, "error": "请输入 NPC 名称"})
                            return
                    token_id = f"t{secrets.token_urlsafe(4)}"
                    while any(item.get("id") == token_id for item in tokens):
                        token_id = f"t{secrets.token_urlsafe(4)}"
                    token = {
                        "id": token_id,
                        "type": token_type,
                        "label": label,
                        "x": x,
                        "y": y,
                    }
                    if token_type == "pc":
                        token["pl_username"] = pl_username
                    tokens.append(token)

                scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()
                saved = token_to_client(token)

            json_response(self, 200, {"ok": True, "token": saved, "scene": scene_to_client(room["scene"])})
            return

        if path == "/api/scene/sync-pcs":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                tokens = scene.setdefault("tokens", [])
                db = load_characters_db()
                existing_pl = {item.get("pl_username") for item in tokens if item.get("type") == "pc"}
                added = 0
                for index, pl_username in enumerate(room.get("players", [])):
                    if pl_username in existing_pl:
                        continue
                    label = get_pc_label_for_pl(db, pl_username)
                    token_id = f"t{secrets.token_urlsafe(4)}"
                    while any(item.get("id") == token_id for item in tokens):
                        token_id = f"t{secrets.token_urlsafe(4)}"
                    tokens.append(
                        {
                            "id": token_id,
                            "type": "pc",
                            "label": label,
                            "pl_username": pl_username,
                            "x": clamp_coord(0.2 + (index % 4) * 0.15),
                            "y": clamp_coord(0.3 + (index // 4) * 0.15),
                        }
                    )
                    added += 1
                scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(
                self,
                200,
                {"ok": True, "added": added, "scene": scene_to_client(room["scene"])},
            )
            return

        if path == "/api/scene/clear":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            with data_lock:
                room = rooms[room_id]
                room.pop("scene", None)
                delete_room_scene_files(room_id)
                save_rooms()

            json_response(self, 200, {"ok": True, "scene": None})
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

            target_pc_id = str(body.get("target", "")).strip()
            if not target_pc_id:
                json_response(self, 400, {"ok": False, "error": "请指定人物卡"})
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
                db = load_characters_db()
                pc, _, _ = find_pc(db, target_pc_id)
                if not pc:
                    json_response(self, 404, {"ok": False, "error": "人物卡不存在"})
                    return
                ensure_status_max(pc)
                status = pc.setdefault("status", {})
                if hp_delta:
                    current_hp = status.get("hp", 0) or 0
                    status["hp"] = max(0, int(current_hp) + hp_delta)
                if san_delta:
                    current_san = status.get("san", 0) or 0
                    status["san"] = max(0, min(99, int(current_san) + san_delta))
                save_characters_db(db)

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "target": target_pc_id,
                    "status": pc.get("status", {}),
                },
            )
            return

        if path == "/api/pcs/select":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            if get_user_role(user) != ROLE_PLAYER:
                json_response(self, 403, {"ok": False, "error": "仅玩家可选择人物卡"})
                return

            pc_id = str(body.get("pc_id", "")).strip()
            if not pc_id:
                json_response(self, 400, {"ok": False, "error": "请指定人物卡"})
                return

            with data_lock:
                db = load_characters_db()
                if not set_active_pc(db, user, pc_id):
                    json_response(self, 404, {"ok": False, "error": "人物卡不存在"})
                    return
                save_characters_db(db)
                pc = get_active_pc_for_pl(db, user, pc_id)

            set_session_active_pc_id(self, pc_id)
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "active_pc_id": pc_id,
                    "active_pc": pc_summary(pc),
                    "needs_pc_selection": False,
                },
            )
            return

        if path == "/api/pcs/upload":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            if get_user_role(user) != ROLE_PLAYER:
                json_response(self, 403, {"ok": False, "error": "仅玩家可上传人物卡"})
                return

            filename = str(body.get("filename", "upload.xlsx")).strip() or "upload.xlsx"
            if not filename.lower().endswith(".xlsx"):
                json_response(self, 400, {"ok": False, "error": "请上传 .xlsx 格式的 Excel 人物卡"})
                return

            file_b64 = body.get("file", "")
            if not file_b64:
                json_response(self, 400, {"ok": False, "error": "未收到文件内容"})
                return

            try:
                raw = base64.b64decode(file_b64, validate=True)
            except (binascii.Error, ValueError):
                json_response(self, 400, {"ok": False, "error": "文件内容无效"})
                return

            if len(raw) > 5 * 1024 * 1024:
                json_response(self, 400, {"ok": False, "error": "文件过大，请上传 5MB 以内的 xlsx"})
                return

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                ok, errors = validate_character_xlsx(tmp_path)
                if not ok:
                    json_response(
                        self,
                        400,
                        {
                            "ok": False,
                            "error": "人物卡格式不符合标准模板（需与莉莉娅人物卡相同布局）",
                            "errors": errors,
                        },
                    )
                    return

                pc_id = f"pc_{secrets.token_urlsafe(8)}"
                with data_lock:
                    db = load_characters_db()
                    character = parse_character_bytes(raw, user, pc_id, filename)
                    character["created_at"] = datetime.now(timezone.utc).isoformat()
                    account = ensure_account(db, user)
                    account["pcs"][pc_id] = character
                    account["active_pc_id"] = pc_id
                    save_characters_db(db)
            finally:
                if tmp_path:
                    Path(tmp_path).unlink(missing_ok=True)

            set_session_active_pc_id(self, pc_id)
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "pc_id": pc_id,
                    "active_pc": pc_summary(character),
                    "needs_pc_selection": False,
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

            cookie, session_data = create_session(username)
            room_id = get_user_room_id(username)
            room_payload = None
            if room_id and room_id in rooms:
                room_payload = room_summary(room_id, rooms[room_id])
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "username": username,
                    "role": ROLE_PLAYER,
                    "room": room_payload,
                    **user_pc_payload(username, ROLE_PLAYER, session_data.get("active_pc_id")),
                },
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

            cookie, session_data = create_session(username)
            room_id = get_user_room_id(username)
            room_payload = None
            if room_id and room_id in rooms:
                room_payload = room_summary(room_id, rooms[room_id])
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "username": username,
                    "role": role,
                    "room": room_payload,
                    **user_pc_payload(username, role, session_data.get("active_pc_id")),
                },
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

    def do_DELETE(self):
        path = urlparse(self.path).path
        body = read_json_body(self)
        if body is None:
            json_response(self, 400, {"ok": False, "error": "请求体无效"})
            return

        if path == "/api/scene/token":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            token_id = str(body.get("id", "")).strip()
            if not token_id:
                json_response(self, 400, {"ok": False, "error": "请指定棋子"})
                return

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                tokens = scene.get("tokens", [])
                new_tokens = [item for item in tokens if item.get("id") != token_id]
                if len(new_tokens) == len(tokens):
                    json_response(self, 404, {"ok": False, "error": "棋子不存在"})
                    return
                scene["tokens"] = new_tokens
                scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(self, 200, {"ok": True, "scene": scene_to_client(room["scene"])})
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


def init_characters_db_file():
    with data_lock:
        db = load_characters_db()
        if os.path.exists(CHARACTERS_FILE):
            with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not (isinstance(raw, dict) and raw.get("version") == 2):
                save_characters_db(db)


if __name__ == "__main__":
    os.makedirs(SCENES_DIR, exist_ok=True)
    os.makedirs(HANDOUTS_DIR, exist_ok=True)
    init_characters_db_file()
    init_rooms()
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        local_ip = get_local_ip()
        print("服务已启动")
        print(f"  本机访问: http://127.0.0.1:{PORT}")
        print(f"  局域网访问: http://{local_ip}:{PORT}")
        print("  公网访问: http://<你的公网IP>:8080")
        print("按 Ctrl+C 停止")
        httpd.serve_forever()
