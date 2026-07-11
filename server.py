#!/usr/bin/env python3
"""静态文件服务器 + 无密码用户名登录/注册 + 角色权限 + 房间投骰。"""

import base64
import binascii
import hashlib
import html
import json
import math
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
BUILTIN_TEST_KP_USERNAME = "上帝KP"
BUILTIN_TEST_ROOM_ID = "ark"
BUILTIN_TEST_ROOM_NAME = "方舟"
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


def ensure_builtin_test_user():
    users = load_users()
    user = users.get(BUILTIN_TEST_KP_USERNAME)
    changed = False
    if not user:
        users[BUILTIN_TEST_KP_USERNAME] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "role": ROLE_KP,
        }
        changed = True
    elif user.get("role") != ROLE_KP:
        user["role"] = ROLE_KP
        changed = True
    if changed:
        save_users(users)


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


def new_room_record(name, kp, permanent=False):
    room = {
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kp": kp,
        "players": [],
        "rolls": [],
        "last_roll_id": 0,
        "next_roll_id": 1,
        "timeline": {
            "entries": [],
            "next_entry_id": 1,
            "current_entry_id": None,
            "updated_at": None,
        },
        "backpacks": {},
        "next_item_id": 1,
        "last_item_id": 0,
        "npcs": [],
        "next_npc_id": 1,
    }
    if permanent:
        room["permanent"] = True
    return room


def find_builtin_test_room_id():
    if BUILTIN_TEST_ROOM_ID in rooms:
        return BUILTIN_TEST_ROOM_ID
    for room_id, room in rooms.items():
        if room.get("kp") == BUILTIN_TEST_KP_USERNAME and room.get("name") == BUILTIN_TEST_ROOM_NAME:
            return room_id
    return BUILTIN_TEST_ROOM_ID


def ensure_builtin_test_room():
    room_id = find_builtin_test_room_id()
    changed = False
    if room_id not in rooms:
        rooms[room_id] = new_room_record(BUILTIN_TEST_ROOM_NAME, BUILTIN_TEST_KP_USERNAME, permanent=True)
        return True

    room = rooms[room_id]
    if room.get("name") != BUILTIN_TEST_ROOM_NAME:
        room["name"] = BUILTIN_TEST_ROOM_NAME
        changed = True
    if room.get("kp") != BUILTIN_TEST_KP_USERNAME:
        room["kp"] = BUILTIN_TEST_KP_USERNAME
        changed = True
    if not room.get("permanent"):
        room["permanent"] = True
        changed = True
    if not room.get("created_at"):
        room["created_at"] = datetime.now(timezone.utc).isoformat()
        changed = True
    if not isinstance(room.get("players"), list):
        room["players"] = []
        changed = True
    if BUILTIN_TEST_KP_USERNAME in room.get("players", []):
        room["players"] = [player for player in room["players"] if player != BUILTIN_TEST_KP_USERNAME]
        changed = True
    if not isinstance(room.get("rolls"), list):
        room["rolls"] = []
        changed = True
    if "last_roll_id" not in room:
        room["last_roll_id"] = 0
        changed = True
    if "next_roll_id" not in room:
        room["next_roll_id"] = 1
        changed = True
    if not isinstance(room.get("timeline"), dict):
        room["timeline"] = {
            "entries": [],
            "next_entry_id": 1,
            "current_entry_id": None,
            "updated_at": None,
        }
        changed = True
    return changed


def rebuild_user_rooms():
    user_rooms.clear()
    for room_id, room in rooms.items():
        kp = room.get("kp")
        if kp and (kp not in user_rooms or room.get("permanent")):
            user_rooms[kp] = room_id
        for player in room.get("players", []):
            user_rooms[player] = room_id


def init_rooms():
    global rooms
    rooms = load_rooms()
    if ensure_builtin_test_room():
        save_rooms()
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


OPPOSED_RANK = {
    "crit_fail": 0,
    "fail": 1,
    "success": 2,
    "hard": 3,
    "extreme": 4,
    "crit_success": 5,
}


def resolve_opposed_side(side, db):
    if not isinstance(side, dict):
        return None, "参数无效"
    side_type = side.get("type")
    skill_name = str(side.get("skill_name", "")).strip()

    if side_type == "player":
        pc_id = str(side.get("pc_id", "")).strip()
        if not pc_id:
            return None, "请选择调查员"
        pc, _, owner = find_pc(db, pc_id)
        if not pc:
            return None, "人物卡不存在"
        if not skill_name:
            return None, "请选择技能"
        skills = pc.get("skills", [])
        skill = next((item for item in skills if item.get("name") == skill_name), None)
        if not skill:
            return None, "技能不存在"
        try:
            skill_value = int(skill.get("value", 0))
        except (TypeError, ValueError):
            return None, "技能值无效"
        name = profile_pc_name(pc, pc_id)
        return {"label": f"{name}·{skill_name}", "skill_value": skill_value, "owner": owner}, None

    if side_type == "monster":
        template_id = str(side.get("template_id", "")).strip()
        if not template_id:
            return None, "请选择怪物/NPC"
        template = get_monster_template(template_id)
        if not template:
            return None, "怪物模板不存在"
        if not skill_name:
            return None, "请选择技能"
        skills = template.get("skills", [])
        skill = next((item for item in skills if item.get("name") == skill_name), None)
        if not skill:
            return None, "技能不存在"
        try:
            skill_value = int(skill.get("value", 0))
        except (TypeError, ValueError):
            return None, "技能值无效"
        name = str(template.get("name") or template_id)
        return {"label": f"{name}·{skill_name}", "skill_value": skill_value, "owner": None}, None

    if side_type == "custom":
        label = str(side.get("label", "")).strip()
        if not label:
            return None, "请输入名称"
        try:
            skill_value = int(side.get("skill_value"))
        except (TypeError, ValueError):
            return None, "数值无效"
        if skill_value < 0 or skill_value > 999:
            return None, "数值需在 0-999 之间"
        return {"label": label, "skill_value": skill_value, "owner": None}, None

    return None, "无效类型"


def profile_pc_name(pc, pc_id):
    profile = pc.get("profile", {})
    return str(profile.get("name") or pc_id)


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
        "permanent": bool(room.get("permanent")),
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
        "opposed_group_id": roll.get("opposed_group_id"),
        "opposed_result": roll.get("opposed_result"),
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
ITEMS_DIR = os.path.join(DIRECTORY, "assets", "items")
PORTRAITS_GEN_DIR = os.path.join(DIRECTORY, "assets", "portraits_gen")
PORTRAIT_DESCRIPTION_MAX = 200
SCENE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SCENE_MAX_BYTES = 5 * 1024 * 1024
SCENE_PROMPT_MAX = 1000
GENERATED_SCENE_WIDTH = 1280
GENERATED_SCENE_HEIGHT = 800
HANDOUT_TITLE_MAX = 80
HANDOUT_BODY_MAX = 8000
TIMELINE_TITLE_MAX = 80
TIMELINE_BODY_MAX = 4000
TIMELINE_MAX_ENTRIES = 200
ITEM_DESCRIPTION_MAX = 600
ITEM_NAME_MAX = 40
ITEM_MAX_PER_ROOM = 300
GENERATED_ITEM_SIZE = 512
SCENE_MIME_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def ensure_room_scene(room):
    return room.setdefault("scene", {"image": None, "tokens": [], "updated_at": None})


FOG_COLS = 20
FOG_ROWS = 14


def ensure_scene_fog(scene):
    fog = scene.get("fog")
    if not fog:
        fog = {
            "enabled": False,
            "cols": FOG_COLS,
            "rows": FOG_ROWS,
            "revealed": "0" * (FOG_COLS * FOG_ROWS),
        }
        scene["fog"] = fog
    return fog


def fog_to_client(fog):
    if not fog or not fog.get("enabled"):
        return None
    return {
        "cols": fog.get("cols", FOG_COLS),
        "rows": fog.get("rows", FOG_ROWS),
        "revealed": fog.get("revealed", ""),
    }


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
        "generated": bool(scene.get("generated")),
        "generated_prompt": scene.get("generated_prompt", ""),
        "fog": fog_to_client(scene.get("fog")),
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


MAP_AREA_KEYWORDS = [
    (("入口", "大门", "门厅", "玄关"), "入口"),
    (("大厅", "大堂", "主厅", "宴会厅"), "大厅"),
    (("走廊", "长廊", "通道", "甬道"), "走廊"),
    (("书房", "图书馆", "档案室"), "书房"),
    (("卧室", "寝室", "客房"), "卧室"),
    (("厨房", "后厨"), "厨房"),
    (("餐厅", "饭厅"), "餐厅"),
    (("仓库", "储藏室", "库房"), "仓库"),
    (("地下室", "地窖", "地下"), "地下室"),
    (("密室", "暗室", "隐藏房间"), "密室"),
    (("祭坛", "仪式", "神龛"), "祭坛"),
    (("医院", "病房", "诊室", "护士站", "药房"), "病房"),
    (("警局", "审讯室", "牢房", "拘留室"), "警局"),
    (("教堂", "礼拜堂", "钟楼"), "教堂"),
    (("酒吧", "吧台", "舞台"), "酒吧"),
    (("码头", "港口", "船坞"), "码头"),
    (("庭院", "花园", "院子"), "庭院"),
    (("墓地", "坟场", "陵园"), "墓地"),
    (("洞穴", "矿洞", "洞窟"), "洞穴"),
    (("研究室", "实验室", "手术室"), "实验室"),
    (("办公室", "会客室"), "办公室"),
]


def normalize_scene_prompt(prompt):
    prompt = re.sub(r"\s+", " ", str(prompt or "")).strip()
    return prompt[:SCENE_PROMPT_MAX]


def add_unique_label(labels, label):
    label = label.strip()
    if label and label not in labels:
        labels.append(label)


def compact_map_label(text, max_len=10):
    text = re.sub(r"[\s　]+", "", text)
    text = re.sub(r"^(这里|这个|这座|这间|一个|一座|一间|有|包括|包含|以及|还有|和|并且)", "", text)
    text = re.sub(r"(区域|房间|地点|地图|场景|附近|尽头)$", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff-]", "", text)
    if len(text) > max_len:
        text = text[:max_len]
    return text


def extract_scene_map_labels(prompt):
    hits = []
    for keywords, label in MAP_AREA_KEYWORDS:
        positions = [prompt.find(word) for word in keywords if word in prompt]
        if positions:
            hits.append((min(positions), label))
    labels = []
    for _, label in sorted(hits, key=lambda item: item[0]):
        add_unique_label(labels, label)

    for chunk in re.split(r"[，,。；;、\n\r]+", prompt):
        label = compact_map_label(chunk)
        if 2 <= len(label) <= 10 and not any(word in label for word in ("生成", "描述", "地图", "需要", "可以")):
            add_unique_label(labels, label)

    for label in ("入口", "大厅", "走廊", "密室", "出口"):
        if len(labels) >= 5:
            break
        add_unique_label(labels, label)
    return labels[:9]


def scene_prompt_has(prompt, *keywords):
    return any(word in prompt for word in keywords)


def wrap_svg_text(text, line_len=38, max_lines=3):
    text = normalize_scene_prompt(text)
    lines = []
    while text and len(lines) < max_lines:
        lines.append(text[:line_len])
        text = text[line_len:]
    if text and lines:
        lines[-1] = f"{lines[-1][:-1]}…"
    return lines


def build_generated_scene_svg(prompt):
    prompt = normalize_scene_prompt(prompt)
    seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    labels = extract_scene_map_labels(prompt)
    width = GENERATED_SCENE_WIDTH
    height = GENERATED_SCENE_HEIGHT
    margin_x = 72
    top_y = 118
    bottom_y = 108
    cols = 3 if len(labels) <= 6 else 4
    rows = (len(labels) + cols - 1) // cols
    cell_w = (width - margin_x * 2) / cols
    cell_h = (height - top_y - bottom_y) / rows
    rooms_layout = []

    for index, label in enumerate(labels):
        col = index % cols
        row = index // cols
        room_w = cell_w * rng.uniform(0.62, 0.82)
        room_h = cell_h * rng.uniform(0.52, 0.72)
        x = margin_x + col * cell_w + (cell_w - room_w) / 2 + rng.uniform(-cell_w * 0.05, cell_w * 0.05)
        y = top_y + row * cell_h + (cell_h - room_h) / 2 + rng.uniform(-cell_h * 0.06, cell_h * 0.06)
        x = max(36, min(width - room_w - 36, x))
        y = max(top_y - 28, min(height - bottom_y - room_h + 24, y))
        rooms_layout.append(
            {
                "label": label,
                "x": x,
                "y": y,
                "w": room_w,
                "h": room_h,
                "cx": x + room_w / 2,
                "cy": y + room_h / 2,
            }
        )

    def esc(value):
        return html.escape(str(value), quote=True)

    def fmt(value):
        return f"{value:.1f}"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        "<defs>",
        '<radialGradient id="paperGlow" cx="50%" cy="45%" r="75%">',
        '<stop offset="0%" stop-color="#3b3023"/>',
        '<stop offset="65%" stop-color="#191712"/>',
        '<stop offset="100%" stop-color="#080806"/>',
        "</radialGradient>",
        '<filter id="roughen"><feTurbulence type="fractalNoise" baseFrequency="0.018" numOctaves="3" seed="8"/><feColorMatrix type="saturate" values="0.18"/><feBlend mode="multiply" in2="SourceGraphic"/></filter>',
        "</defs>",
        '<rect width="1280" height="800" fill="url(#paperGlow)"/>',
        '<rect x="24" y="24" width="1232" height="752" rx="18" fill="none" stroke="#8a6f3c" stroke-width="3" opacity="0.75"/>',
        '<g opacity="0.13" stroke="#d7bd76" stroke-width="1">',
    ]

    for x in range(80, width, 80):
        parts.append(f'<line x1="{x}" y1="72" x2="{x}" y2="{height - 72}"/>')
    for y in range(120, height - 80, 80):
        parts.append(f'<line x1="48" y1="{y}" x2="{width - 48}" y2="{y}"/>')
    parts.append("</g>")

    title = compact_map_label(prompt, 16) or "生成地图"
    parts.extend(
        [
            f'<text x="64" y="66" fill="#f2d98c" font-size="32" font-family="Noto Serif SC, SimSun, serif" font-weight="700">{esc(title)}</text>',
            '<text x="64" y="94" fill="#9fb9a8" font-size="18" font-family="Noto Serif SC, SimSun, serif">KP 描述生成 · 可继续拖拽 PC / NPC 棋子</text>',
        ]
    )

    for first, second in zip(rooms_layout, rooms_layout[1:]):
        mid_x = (first["cx"] + second["cx"]) / 2
        path = (
            f'M {fmt(first["cx"])} {fmt(first["cy"])} '
            f'L {fmt(mid_x)} {fmt(first["cy"])} '
            f'L {fmt(mid_x)} {fmt(second["cy"])} '
            f'L {fmt(second["cx"])} {fmt(second["cy"])}'
        )
        parts.append(f'<path d="{path}" fill="none" stroke="#8d7845" stroke-width="34" stroke-linecap="round" stroke-linejoin="round" opacity="0.58"/>')
        parts.append(f'<path d="{path}" fill="none" stroke="#18140f" stroke-width="20" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>')

    if len(rooms_layout) > 4:
        first = rooms_layout[0]
        last = rooms_layout[-1]
        path = f'M {fmt(first["cx"])} {fmt(first["cy"])} Q {fmt(width / 2)} {fmt(height - 92)} {fmt(last["cx"])} {fmt(last["cy"])}'
        parts.append(f'<path d="{path}" fill="none" stroke="#b8964d" stroke-width="8" stroke-dasharray="16 16" opacity="0.55"/>')

    for room in rooms_layout:
        parts.append(
            f'<rect x="{fmt(room["x"])}" y="{fmt(room["y"])}" width="{fmt(room["w"])}" height="{fmt(room["h"])}" rx="12" fill="#2b261d" stroke="#b89855" stroke-width="5" filter="url(#roughen)"/>'
        )
        parts.append(
            f'<rect x="{fmt(room["x"] + 12)}" y="{fmt(room["y"] + 12)}" width="{fmt(room["w"] - 24)}" height="{fmt(room["h"] - 24)}" rx="7" fill="none" stroke="#e0c577" stroke-width="2" opacity="0.28"/>'
        )
        parts.append(
            f'<text x="{fmt(room["cx"])}" y="{fmt(room["cy"] + 6)}" fill="#f6e8b7" font-size="27" font-family="Noto Serif SC, SimSun, serif" font-weight="700" text-anchor="middle">{esc(room["label"])}</text>'
        )

    feature_room = rooms_layout[min(1, len(rooms_layout) - 1)]
    if scene_prompt_has(prompt, "水", "河", "湖", "海", "池", "井", "下水道", "深潜"):
        parts.append(
            f'<ellipse cx="{fmt(feature_room["cx"])}" cy="{fmt(feature_room["cy"] + feature_room["h"] * 0.22)}" rx="{fmt(feature_room["w"] * 0.23)}" ry="{fmt(feature_room["h"] * 0.13)}" fill="#244d61" stroke="#7ac2d1" stroke-width="3" opacity="0.9"/>'
        )
        parts.append(f'<text x="{fmt(feature_room["cx"])}" y="{fmt(feature_room["cy"] + feature_room["h"] * 0.25)}" fill="#c9eef2" font-size="15" font-family="Noto Serif SC, SimSun, serif" text-anchor="middle">水域</text>')

    if scene_prompt_has(prompt, "森林", "树林", "树", "花园", "庭院", "墓地"):
        for index in range(8):
            cx = 90 + index * 38 + rng.uniform(-8, 8)
            cy = height - 176 + rng.uniform(-18, 18)
            parts.append(f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{rng.randint(12, 20)}" fill="#23482f" stroke="#5e8a58" stroke-width="2" opacity="0.9"/>')
        parts.append('<text x="95" y="674" fill="#a7d09e" font-size="18" font-family="Noto Serif SC, SimSun, serif">树影 / 遮蔽</text>')

    if scene_prompt_has(prompt, "祭坛", "仪式", "邪神", "神龛", "法阵"):
        room = rooms_layout[-1]
        points = []
        for index in range(6):
            angle = -1.5708 + index * 1.0472
            radius = min(room["w"], room["h"]) * 0.18
            points.append(f'{fmt(room["cx"] + radius * math.cos(angle))},{fmt(room["cy"] + radius * math.sin(angle))}')
        parts.append(f'<polygon points="{" ".join(points)}" fill="#3a0d12" stroke="#d6a84d" stroke-width="4" opacity="0.92"/>')
        parts.append(f'<circle cx="{fmt(room["cx"])}" cy="{fmt(room["cy"])}" r="{fmt(min(room["w"], room["h"]) * 0.09)}" fill="none" stroke="#e0c577" stroke-width="3"/>')

    if scene_prompt_has(prompt, "楼梯", "地下", "二楼", "上楼", "下楼", "地窖"):
        room = rooms_layout[0]
        sx = room["x"] + room["w"] * 0.12
        sy = room["y"] + room["h"] * 0.18
        for index in range(7):
            parts.append(f'<line x1="{fmt(sx)}" y1="{fmt(sy + index * 10)}" x2="{fmt(sx + 76)}" y2="{fmt(sy + index * 10)}" stroke="#d2bb6c" stroke-width="4" opacity="0.72"/>')
        parts.append(f'<text x="{fmt(sx + 38)}" y="{fmt(sy + 88)}" fill="#ead68f" font-size="15" font-family="Noto Serif SC, SimSun, serif" text-anchor="middle">楼梯</text>')

    if scene_prompt_has(prompt, "陷阱", "危险", "坍塌", "血迹", "封锁"):
        room = rooms_layout[len(rooms_layout) // 2]
        for offset in range(-30, 40, 15):
            parts.append(f'<line x1="{fmt(room["cx"] + offset)}" y1="{fmt(room["cy"] - 34)}" x2="{fmt(room["cx"] + offset + 46)}" y2="{fmt(room["cy"] + 34)}" stroke="#8f2e2e" stroke-width="5" opacity="0.65"/>')
        parts.append(f'<text x="{fmt(room["cx"])}" y="{fmt(room["cy"] + 58)}" fill="#f2a09a" font-size="16" font-family="Noto Serif SC, SimSun, serif" text-anchor="middle">危险区域</text>')

    parts.append('<g font-family="Noto Serif SC, SimSun, serif" font-size="15" fill="#cdbf99" opacity="0.82">')
    for index, line in enumerate(wrap_svg_text(prompt)):
        parts.append(f'<text x="64" y="{height - 58 + index * 20}">{esc(line)}</text>')
    parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts)


def save_generated_scene_map(room_id, prompt):
    room_dir = os.path.join(SCENES_DIR, room_id)
    os.makedirs(room_dir, exist_ok=True)
    for old_name in os.listdir(room_dir):
        old_path = os.path.join(room_dir, old_name)
        if os.path.isfile(old_path):
            os.remove(old_path)
    file_name = "generated_scene.svg"
    file_path = os.path.join(room_dir, file_name)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(build_generated_scene_svg(prompt))
    return f"scenes/{room_id}/{file_name}"


def ensure_room_handouts(room):
    room.setdefault("handouts", [])
    room.setdefault("next_handout_id", 1)
    room.setdefault("last_handout_id", 0)
    room.setdefault("handout_reads", {})
    return room


def ensure_room_timeline(room):
    timeline = room.get("timeline")
    if not isinstance(timeline, dict):
        timeline = {"entries": timeline if isinstance(timeline, list) else []}
        room["timeline"] = timeline

    entries = timeline.get("entries")
    if not isinstance(entries, list):
        entries = []
        timeline["entries"] = entries

    normalized = []
    max_id = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        try:
            entry_id = int(entry.get("id", index + 1))
        except (TypeError, ValueError):
            entry_id = index + 1
        entry["id"] = entry_id
        entry.setdefault("title", "未命名节点")
        entry.setdefault("body", "")
        entry.setdefault("created_at", None)
        entry.setdefault("created_by", "")
        entry.setdefault("updated_at", None)
        normalized.append(entry)
        max_id = max(max_id, entry_id)

    timeline["entries"] = normalized
    try:
        next_id = int(timeline.get("next_entry_id", 1))
    except (TypeError, ValueError):
        next_id = 1
    timeline["next_entry_id"] = max(next_id, max_id + 1)

    current_id = timeline.get("current_entry_id")
    if current_id is not None:
        try:
            current_id = int(current_id)
        except (TypeError, ValueError):
            current_id = None
    if current_id is not None and not any(entry.get("id") == current_id for entry in normalized):
        current_id = None
    timeline["current_entry_id"] = current_id
    timeline.setdefault("updated_at", None)
    return timeline


def timeline_entry_to_client(entry):
    return {
        "id": entry.get("id"),
        "title": entry.get("title", ""),
        "body": entry.get("body", ""),
        "created_at": entry.get("created_at"),
        "created_by": entry.get("created_by"),
        "updated_at": entry.get("updated_at"),
    }


def timeline_to_client(timeline):
    timeline = timeline or {}
    return {
        "entries": [timeline_entry_to_client(entry) for entry in timeline.get("entries", [])],
        "current_entry_id": timeline.get("current_entry_id"),
        "updated_at": timeline.get("updated_at"),
    }


def normalize_timeline_entry_payload(body):
    title = str(body.get("title", "")).strip()
    entry_body = str(body.get("body", "")).strip()
    if not title:
        return None, None, "请输入时间线标题"
    if len(title) > TIMELINE_TITLE_MAX:
        return None, None, f"标题不能超过 {TIMELINE_TITLE_MAX} 字"
    if len(entry_body) > TIMELINE_BODY_MAX:
        return None, None, f"正文不能超过 {TIMELINE_BODY_MAX} 字"
    return title, entry_body, None


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


def normalize_item_description(description):
    description = re.sub(r"\s+", " ", str(description or "")).strip()
    return description[:ITEM_DESCRIPTION_MAX]


def item_prompt_has(description, *keywords):
    return any(word in description for word in keywords)


def detect_item_kind(description):
    kind_keywords = [
        ("key", ("钥匙", "钥", "锁匙")),
        ("letter", ("信", "信件", "纸条", "纸", "笔记", "日记", "文件")),
        ("book", ("书", "书本", "典籍", "册子", "笔记本")),
        ("bottle", ("瓶", "药瓶", "药水", "试管", "酒瓶", "香水")),
        ("blade", ("刀", "匕首", "短剑", "剑", "刃")),
        ("coin", ("硬币", "金币", "银币", "徽章", "奖章")),
        ("gem", ("宝石", "水晶", "晶体", "戒指")),
        ("amulet", ("护符", "吊坠", "项链", "符咒")),
        ("map", ("地图", "海图", "藏宝图")),
        ("gun", ("枪", "手枪", "左轮")),
    ]
    for kind, keywords in kind_keywords:
        if item_prompt_has(description, *keywords):
            return kind
    return "artifact"


def extract_item_name(description):
    description = normalize_item_description(description)
    kind = detect_item_kind(description)
    adjective = ""
    if item_prompt_has(description, "锈", "铁锈", "腐蚀", "斑斑"):
        adjective = "锈迹"
    elif item_prompt_has(description, "破旧", "古旧", "古老", "陈旧", "泛黄"):
        adjective = "旧"
    elif item_prompt_has(description, "血", "血迹", "血色"):
        adjective = "血迹"
    elif item_prompt_has(description, "发光", "微光", "幽光", "荧光"):
        adjective = "微光"
    base_names = {
        "key": "钥匙",
        "letter": "信件",
        "book": "书本",
        "bottle": "药瓶",
        "blade": "匕首",
        "coin": "硬币",
        "gem": "宝石",
        "amulet": "护符",
        "map": "地图",
        "gun": "手枪",
        "artifact": "物品",
    }
    if adjective:
        return f"{adjective}{base_names.get(kind, '物品')}"[:ITEM_NAME_MAX]
    compact = re.sub(r"[^\w\u4e00-\u9fff-]", "", description)
    compact = re.sub(r"^(一把|一个|一枚|一张|一本|一瓶|一件|这把|这个|这枚|这张|这本|这瓶)", "", compact)
    if 2 <= len(compact) <= ITEM_NAME_MAX:
        return compact
    return base_names.get(kind, "物品")


def wrap_item_svg_text(description, line_len=18, max_lines=3):
    text = normalize_item_description(description)
    lines = []
    while text and len(lines) < max_lines:
        lines.append(text[:line_len])
        text = text[line_len:]
    if text and lines and len(lines[-1]) > 1:
        lines[-1] = f"{lines[-1][:-1]}…"
    return lines


def ensure_room_backpacks(room):
    backpacks = room.get("backpacks")
    if not isinstance(backpacks, dict):
        backpacks = {}
        room["backpacks"] = backpacks

    max_id = 0
    normalized = {}
    for owner, items in list(backpacks.items()):
        owner_name = normalize_username(str(owner))
        if not owner_name:
            continue
        if not isinstance(items, list):
            items = []
        clean_items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item.get("id", index + 1))
            except (TypeError, ValueError):
                item_id = index + 1
            item["id"] = item_id
            item.setdefault("owner", owner_name)
            item.setdefault("name", extract_item_name(item.get("description", "")))
            item.setdefault("description", "")
            item.setdefault("image", None)
            item.setdefault("created_at", None)
            item.setdefault("created_by", "")
            clean_items.append(item)
            max_id = max(max_id, item_id)
        normalized[owner_name] = clean_items

    for player in room.get("players", []):
        normalized.setdefault(player, [])

    room["backpacks"] = normalized
    try:
        next_item_id = int(room.get("next_item_id", 1))
    except (TypeError, ValueError):
        next_item_id = 1
    room["next_item_id"] = max(next_item_id, max_id + 1)
    room.setdefault("last_item_id", max_id)
    return room


def backpack_item_to_client(item):
    image = item.get("image")
    return {
        "id": item.get("id"),
        "name": item.get("name") or extract_item_name(item.get("description", "")),
        "description": item.get("description", ""),
        "image": f"/assets/{image}" if image else None,
        "owner": item.get("owner", ""),
        "created_at": item.get("created_at"),
        "created_by": item.get("created_by"),
    }


def backpack_payload(room, username, can_manage):
    ensure_room_backpacks(room)
    owners = list(room.get("players", []))
    for owner, items in room.get("backpacks", {}).items():
        if owner not in owners and items:
            owners.append(owner)
    if not can_manage:
        owners = [username]
    return {
        "can_manage": bool(can_manage),
        "players": list(room.get("players", [])),
        "backpacks": [
            {
                "owner": owner,
                "items": [backpack_item_to_client(item) for item in room.get("backpacks", {}).get(owner, [])],
            }
            for owner in owners
        ],
        "last_item_id": room.get("last_item_id", 0),
    }


def delete_item_image_file(item):
    image = item.get("image") if isinstance(item, dict) else None
    if not image or not str(image).startswith("items/"):
        return
    base_dir = os.path.abspath(ITEMS_DIR)
    file_path = os.path.abspath(os.path.join(DIRECTORY, "assets", image))
    if not (file_path == base_dir or file_path.startswith(base_dir + os.sep)):
        return
    if os.path.isfile(file_path):
        os.remove(file_path)


def delete_room_item_files(room_id):
    room_dir = os.path.join(ITEMS_DIR, room_id)
    if os.path.isdir(room_dir):
        for old_name in os.listdir(room_dir):
            old_path = os.path.join(room_dir, old_name)
            if os.path.isfile(old_path):
                os.remove(old_path)
        try:
            os.rmdir(room_dir)
        except OSError:
            pass


def build_generated_item_svg(description):
    description = normalize_item_description(description)
    name = extract_item_name(description)
    kind = detect_item_kind(description)
    seed = int(hashlib.sha256(f"item:{description}".encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    size = GENERATED_ITEM_SIZE
    rusty = item_prompt_has(description, "锈", "铁锈", "腐蚀", "斑斑")
    bloody = item_prompt_has(description, "血", "血迹", "血色")
    glowing = item_prompt_has(description, "发光", "微光", "幽光", "荧光", "魔法")
    old = item_prompt_has(description, "破旧", "古旧", "古老", "陈旧", "泛黄", "磨损")

    def esc(value):
        return html.escape(str(value), quote=True)

    def fmt(value):
        return f"{value:.1f}"

    metal = "#aa9b78" if old or rusty else "#d6c28c"
    accent = "#c9a227"
    glow = "#65d8a9" if glowing else "#d7b663"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">',
        "<defs>",
        '<radialGradient id="bg" cx="48%" cy="38%" r="72%">',
        '<stop offset="0%" stop-color="#20372d"/>',
        '<stop offset="58%" stop-color="#0d1c16"/>',
        '<stop offset="100%" stop-color="#050807"/>',
        "</radialGradient>",
        '<filter id="softShadow" x="-25%" y="-25%" width="150%" height="150%"><feDropShadow dx="0" dy="18" stdDeviation="13" flood-color="#000" flood-opacity="0.55"/></filter>',
        '<filter id="paperNoise"><feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="3" seed="5"/><feColorMatrix type="saturate" values="0.25"/><feBlend mode="multiply" in2="SourceGraphic"/></filter>',
        "</defs>",
        '<rect width="512" height="512" fill="url(#bg)"/>',
        '<rect x="24" y="24" width="464" height="464" rx="18" fill="none" stroke="#8a6f3c" stroke-width="3" opacity="0.72"/>',
        '<rect x="42" y="42" width="428" height="428" rx="12" fill="none" stroke="#c9a227" stroke-width="1.3" opacity="0.22"/>',
    ]

    if glowing:
        parts.append(f'<circle cx="256" cy="237" r="145" fill="{glow}" opacity="0.09"/>')
        parts.append(f'<circle cx="256" cy="237" r="82" fill="{glow}" opacity="0.12"/>')

    angle = rng.uniform(-8, 8)
    parts.append(f'<g filter="url(#softShadow)" transform="rotate({fmt(angle)} 256 245)">')

    if kind == "key":
        parts.extend(
            [
                f'<circle cx="172" cy="230" r="55" fill="none" stroke="{metal}" stroke-width="22"/>',
                '<circle cx="172" cy="230" r="22" fill="none" stroke="#0b1712" stroke-width="16"/>',
                f'<rect x="213" y="218" width="170" height="25" rx="11" fill="{metal}"/>',
                f'<rect x="350" y="238" width="25" height="54" rx="4" fill="{metal}"/>',
                f'<rect x="384" y="238" width="21" height="39" rx="4" fill="{metal}"/>',
                '<line x1="236" y1="230" x2="330" y2="230" stroke="#f0dcaa" stroke-width="5" opacity="0.42"/>',
            ]
        )
    elif kind == "letter":
        parts.extend(
            [
                '<rect x="132" y="155" width="248" height="170" rx="8" fill="#c7b483" stroke="#80683a" stroke-width="5" filter="url(#paperNoise)"/>',
                '<path d="M140 166 L256 256 L372 166" fill="none" stroke="#7d673c" stroke-width="5" opacity="0.72"/>',
                '<path d="M140 315 L224 238 M372 315 L288 238" fill="none" stroke="#7d673c" stroke-width="4" opacity="0.42"/>',
                '<circle cx="256" cy="248" r="21" fill="#7b1d24" stroke="#d7a651" stroke-width="3"/>',
            ]
        )
    elif kind == "book":
        parts.extend(
            [
                '<path d="M142 126 H330 Q370 126 370 166 V366 Q370 388 348 388 H150 Q128 388 128 366 V148 Q128 126 142 126 Z" fill="#4b151c" stroke="#b68d46" stroke-width="6"/>',
                '<path d="M166 144 H335 Q348 144 348 160 V360 Q348 372 335 372 H166 Z" fill="#251a14" opacity="0.3"/>',
                '<rect x="168" y="164" width="145" height="190" rx="6" fill="none" stroke="#c9a227" stroke-width="4" opacity="0.74"/>',
                '<path d="M128 352 Q212 333 370 352" fill="none" stroke="#ead8a0" stroke-width="7" opacity="0.62"/>',
            ]
        )
    elif kind == "bottle":
        parts.extend(
            [
                '<rect x="228" y="118" width="56" height="74" rx="12" fill="#83b5a0" stroke="#d7c591" stroke-width="5" opacity="0.82"/>',
                '<path d="M194 196 Q194 166 228 166 H284 Q318 166 318 196 L342 354 Q346 386 314 396 H198 Q166 386 170 354 Z" fill="#2c6f62" stroke="#d7c591" stroke-width="7" opacity="0.9"/>',
                '<path d="M190 318 Q246 300 326 318 L314 378 H198 Z" fill="#8c1d2c" opacity="0.55"/>',
                '<rect x="220" y="104" width="72" height="26" rx="8" fill="#6b4b2a" stroke="#c9a227" stroke-width="4"/>',
            ]
        )
    elif kind == "blade":
        parts.extend(
            [
                f'<path d="M166 350 L318 104 Q346 76 360 92 Q376 110 348 138 L206 376 Z" fill="{metal}" stroke="#f1dfb1" stroke-width="5"/>',
                '<path d="M204 316 L252 362 L222 394 L174 348 Z" fill="#5b321f" stroke="#c9a227" stroke-width="5"/>',
                '<rect x="168" y="344" width="105" height="30" rx="12" fill="#1d1611" stroke="#b6904a" stroke-width="5" transform="rotate(43 220 358)"/>',
                '<path d="M230 276 L324 120" stroke="#fff1c9" stroke-width="5" opacity="0.45"/>',
            ]
        )
    elif kind == "coin":
        parts.extend(
            [
                '<circle cx="256" cy="242" r="104" fill="#a8782c" stroke="#f1d58c" stroke-width="9"/>',
                '<circle cx="256" cy="242" r="76" fill="none" stroke="#4f3412" stroke-width="6" opacity="0.45"/>',
                '<path d="M256 171 L274 221 L328 222 L285 254 L301 306 L256 276 L211 306 L227 254 L184 222 L238 221 Z" fill="#d7b663" stroke="#6a4618" stroke-width="4"/>',
            ]
        )
    elif kind == "gem":
        points = "256,98 350,178 318,348 256,406 194,348 162,178"
        parts.extend(
            [
                f'<polygon points="{points}" fill="#4fb39b" stroke="#d8f6e8" stroke-width="6"/>',
                '<path d="M256 98 L256 406 M162 178 H350 M194 348 L256 178 L318 348" stroke="#12352f" stroke-width="5" opacity="0.35"/>',
                '<path d="M210 172 L246 124 L288 172 Z" fill="#b4fff0" opacity="0.35"/>',
            ]
        )
    elif kind == "map":
        parts.extend(
            [
                '<path d="M122 146 Q168 120 214 146 Q258 170 306 146 Q352 124 390 146 V354 Q348 330 306 354 Q258 378 214 354 Q166 328 122 354 Z" fill="#c5b27d" stroke="#7e6538" stroke-width="6" filter="url(#paperNoise)"/>',
                '<path d="M214 146 V354 M306 146 V354" stroke="#7e6538" stroke-width="4" opacity="0.35"/>',
                '<path d="M158 250 C212 212 244 300 302 254 C326 236 350 238 370 260" fill="none" stroke="#50351c" stroke-width="5" stroke-dasharray="10 9"/>',
                '<path d="M180 198 L204 222 M204 198 L180 222" stroke="#83232b" stroke-width="8" stroke-linecap="round"/>',
            ]
        )
    elif kind == "gun":
        parts.extend(
            [
                f'<path d="M152 220 H344 Q372 220 378 242 L386 270 H326 L314 250 H244 L220 342 H174 L192 250 H152 Z" fill="{metal}" stroke="#ead7a4" stroke-width="6"/>',
                '<rect x="312" y="192" width="76" height="32" rx="8" fill="#2d2418" stroke="#b6904a" stroke-width="5"/>',
                '<path d="M207 250 L188 340" stroke="#3f2618" stroke-width="11"/>',
                '<circle cx="282" cy="242" r="13" fill="#101010" stroke="#c9a227" stroke-width="4"/>',
            ]
        )
    else:
        sides = []
        for index in range(8):
            radius = 116 if index % 2 == 0 else 72
            angle_rad = -math.pi / 2 + index * math.pi / 4
            sides.append(f"{fmt(256 + math.cos(angle_rad) * radius)},{fmt(238 + math.sin(angle_rad) * radius)}")
        parts.extend(
            [
                f'<polygon points="{" ".join(sides)}" fill="#3b2720" stroke="{accent}" stroke-width="7"/>',
                '<circle cx="256" cy="238" r="58" fill="none" stroke="#73d6ad" stroke-width="7" opacity="0.78"/>',
                f'<path d="M256 178 L273 224 L322 224 L282 252 L298 300 L256 270 L214 300 L230 252 L190 224 L239 224 Z" fill="{accent}" opacity="0.55"/>',
            ]
        )

    if rusty:
        for _ in range(42):
            cx = rng.uniform(135, 385)
            cy = rng.uniform(120, 385)
            radius = rng.uniform(2.0, 6.5)
            parts.append(f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(radius)}" fill="#8a3d1e" opacity="{rng.uniform(0.32, 0.78):.2f}"/>')
    if bloody:
        for _ in range(7):
            cx = rng.uniform(168, 344)
            cy = rng.uniform(178, 348)
            parts.append(f'<path d="M{fmt(cx)} {fmt(cy)} C{fmt(cx + rng.uniform(-12, 12))} {fmt(cy + 20)} {fmt(cx + rng.uniform(-8, 10))} {fmt(cy + 34)} {fmt(cx + rng.uniform(-2, 8))} {fmt(cy + 48)}" stroke="#791821" stroke-width="{fmt(rng.uniform(5, 10))}" stroke-linecap="round" opacity="0.68"/>')
    parts.append("</g>")

    parts.extend(
        [
            f'<text x="256" y="438" text-anchor="middle" fill="#f0e1b6" font-size="28" font-family="Noto Serif SC, SimSun, serif" font-weight="700">{esc(name)}</text>',
            '<g fill="#b9c6bb" font-size="15" font-family="Noto Serif SC, SimSun, serif" opacity="0.82">',
        ]
    )
    for index, line in enumerate(wrap_item_svg_text(description)):
        parts.append(f'<text x="256" y="{464 + index * 18}" text-anchor="middle">{esc(line)}</text>')
    parts.extend(["</g>", "</svg>"])
    return "\n".join(parts)


def save_generated_item_image(room_id, item_id, description):
    room_dir = os.path.join(ITEMS_DIR, room_id)
    os.makedirs(room_dir, exist_ok=True)
    file_name = f"i{item_id}.svg"
    file_path = os.path.join(room_dir, file_name)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(build_generated_item_svg(description))
    return f"items/{room_id}/{file_name}"


PORTRAIT_SKIN_KEYWORDS = [
    (("苍白", "惨白", "病态"), ("#e6d7c3", "#c9b79c")),
    (("黝黑", "古铜", "小麦色"), ("#8a5a3a", "#6a4127")),
    (("红润", "健康", "红光"), ("#e8b49a", "#d99678")),
    (("灰败", "死灰", "青灰"), ("#9aa08f", "#767c6c")),
]
PORTRAIT_SKIN_DEFAULT = [("#d9b58f", "#c19b70"), ("#e0c19f", "#c7a37e"), ("#c68f66", "#a87450")]

PORTRAIT_HAIR_KEYWORDS = [
    (("金发",), "#c9a227"),
    (("黑发", "乌黑"), "#1c1a17"),
    (("白发", "花白", "银发"), "#d8d4c8"),
    (("红发", "赤发"), "#8a3324"),
    (("褐发", "棕发"), "#5b3a24"),
]
PORTRAIT_BALD_KEYWORDS = ("秃头", "光头", "秃顶")

PORTRAIT_EYE_KEYWORDS = [
    (("血红", "赤红", "猩红"), "#b3352c"),
    (("发光", "荧光", "幽光"), "#6fe0c8"),
    (("金色", "琥珀"), "#c9a227"),
    (("漆黑", "深邃"), "#0c0b0a"),
]
PORTRAIT_EYE_DEFAULT = "#2a2420"

PORTRAIT_MOOD_KEYWORDS = [
    (("邪恶", "阴森", "诡异", "凶恶", "狰狞"), ("#1a1030", "#3a1f4a", "#8a4fd8")),
    (("温和", "慈祥", "和善", "亲切"), ("#3a2f18", "#5a4526", "#c9a227")),
    (("冷酷", "严肃", "冰冷"), ("#101820", "#1e2f3d", "#5fa8c9")),
    (("神秘", "古怪"), ("#141225", "#241f3f", "#7a5fc9")),
]
PORTRAIT_MOOD_DEFAULT = ("#14120f", "#2a2419", "#c9a227")

PORTRAIT_MONSTER_PALETTE = [
    ("#0c1f16", "#1e4530", "#4fd88a"),
    ("#1f0c1f", "#451e3f", "#d84fc0"),
    ("#1c1508", "#3f2e0e", "#d8a54f"),
    ("#0a1420", "#122b45", "#4f9fd8"),
]


def portrait_prompt_has(description, *keywords):
    return any(word in description for word in keywords)


def portrait_pick(description, keyword_table, default, rng):
    for keywords, value in keyword_table:
        if portrait_prompt_has(description, *keywords):
            return value
    if isinstance(default, list):
        return rng.choice(default)
    return default


def build_human_portrait_svg(description, rng):
    skin, skin_shadow = portrait_pick(description, PORTRAIT_SKIN_KEYWORDS, PORTRAIT_SKIN_DEFAULT, rng)
    hair = portrait_pick(description, PORTRAIT_HAIR_KEYWORDS, rng.choice(["#1c1a17", "#3a2a1a", "#5b3a24"]), rng)
    bald = portrait_prompt_has(description, *PORTRAIT_BALD_KEYWORDS)
    eye = portrait_pick(description, PORTRAIT_EYE_KEYWORDS, PORTRAIT_EYE_DEFAULT, rng)
    bg1, bg2, accent = portrait_pick(description, PORTRAIT_MOOD_KEYWORDS, PORTRAIT_MOOD_DEFAULT, rng)
    has_beard = portrait_prompt_has(description, "胡须", "胡子", "络腮")
    has_scar = portrait_prompt_has(description, "伤疤", "疤痕", "刀疤")
    has_glasses = portrait_prompt_has(description, "眼镜", "墨镜")
    has_eyepatch = portrait_prompt_has(description, "眼罩", "独眼")
    has_hood = portrait_prompt_has(description, "兜帽", "斗篷", "头巾")

    face_w = 88 + rng.randint(-6, 6)
    face_h = 108 + rng.randint(-6, 6)
    cx, cy = 150, 150

    parts = [
        '<svg viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        f'<radialGradient id="bg" cx="50%" cy="42%" r="75%">'
        f'<stop offset="0%" stop-color="{bg2}"/><stop offset="100%" stop-color="{bg1}"/></radialGradient>',
        "</defs>",
        '<rect x="0" y="0" width="300" height="300" fill="url(#bg)"/>',
        f'<rect x="6" y="6" width="288" height="288" fill="none" stroke="{accent}" stroke-width="2" opacity="0.5"/>',
    ]

    if not bald:
        parts.append(
            f'<path d="M {cx - face_w / 2 - 6} {cy - face_h / 2 + 18} '
            f'Q {cx - face_w / 2 - 10} {cy - face_h / 2 - 40}, {cx} {cy - face_h / 2 - 34} '
            f'Q {cx + face_w / 2 + 10} {cy - face_h / 2 - 40}, {cx + face_w / 2 + 6} {cy - face_h / 2 + 18} '
            f'Q {cx} {cy - face_h / 2 - 6}, {cx - face_w / 2 - 6} {cy - face_h / 2 + 18} Z" fill="{hair}"/>'
        )

    if has_hood:
        parts.append(
            f'<path d="M {cx - face_w / 2 - 30} {cy + face_h / 2 - 10} '
            f'Q {cx - face_w / 2 - 34} {cy - face_h / 2 - 50}, {cx} {cy - face_h / 2 - 58} '
            f'Q {cx + face_w / 2 + 34} {cy - face_h / 2 - 50}, {cx + face_w / 2 + 30} {cy + face_h / 2 - 10} '
            f'Q {cx} {cy - face_h / 2 + 6}, {cx - face_w / 2 - 30} {cy + face_h / 2 - 10} Z" '
            f'fill="{bg1}" opacity="0.92"/>'
        )

    parts.append(
        f'<ellipse cx="{cx}" cy="{cy}" rx="{face_w / 2}" ry="{face_h / 2}" fill="{skin}" stroke="{skin_shadow}" stroke-width="2"/>'
    )

    eye_y = cy - 6
    eye_dx = face_w * 0.24
    for sign in (-1, 1):
        ex = cx + sign * eye_dx
        parts.append(f'<ellipse cx="{ex}" cy="{eye_y}" rx="7" ry="5" fill="#fff" opacity="0.92"/>')
        parts.append(f'<circle cx="{ex}" cy="{eye_y}" r="3.4" fill="{eye}"/>')
        parts.append(
            f'<path d="M {ex - 9} {eye_y - 9} Q {ex} {eye_y - 14}, {ex + 9} {eye_y - 9}" '
            f'stroke="{hair if not bald else skin_shadow}" stroke-width="2.4" fill="none" stroke-linecap="round"/>'
        )
        if has_eyepatch and sign == 1:
            parts.append(f'<circle cx="{ex}" cy="{eye_y}" r="9" fill="#161311"/>')
            parts.append(
                f'<line x1="{cx - face_w / 2}" y1="{eye_y - 12}" x2="{cx + face_w / 2}" y2="{eye_y + 4}" '
                f'stroke="#161311" stroke-width="4"/>'
            )

    if has_glasses:
        for sign in (-1, 1):
            ex = cx + sign * eye_dx
            parts.append(f'<circle cx="{ex}" cy="{eye_y}" r="12" fill="none" stroke="{accent}" stroke-width="2.4"/>')
        parts.append(
            f'<line x1="{cx - eye_dx + 12}" y1="{eye_y}" x2="{cx + eye_dx - 12}" y2="{eye_y}" '
            f'stroke="{accent}" stroke-width="2.4"/>'
        )

    parts.append(
        f'<path d="M {cx - 4} {cy + 10} L {cx + 3} {cy + 22} Q {cx} {cy + 27}, {cx - 5} {cy + 24}" '
        f'fill="none" stroke="{skin_shadow}" stroke-width="2" stroke-linecap="round"/>'
    )
    parts.append(
        f'<path d="M {cx - 16} {cy + face_h * 0.28} Q {cx} {cy + face_h * 0.34}, {cx + 16} {cy + face_h * 0.28}" '
        f'fill="none" stroke="{skin_shadow}" stroke-width="2.6" stroke-linecap="round"/>'
    )

    if has_scar:
        parts.append(
            f'<line x1="{cx + face_w * 0.1}" y1="{cy - face_h * 0.32}" x2="{cx + face_w * 0.22}" y2="{cy + face_h * 0.18}" '
            f'stroke="#8a2f2f" stroke-width="2.2" opacity="0.85"/>'
        )

    if has_beard:
        parts.append(
            f'<path d="M {cx - face_w / 2 + 6} {cy + face_h * 0.05} '
            f'Q {cx} {cy + face_h * 0.62}, {cx + face_w / 2 - 6} {cy + face_h * 0.05} '
            f'Q {cx} {cy + face_h * 0.5}, {cx - face_w / 2 + 6} {cy + face_h * 0.05} Z" '
            f'fill="{hair}" opacity="0.92"/>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def build_monster_portrait_svg(description, rng):
    bg1, bg2, accent = rng.choice(PORTRAIT_MONSTER_PALETTE)
    if portrait_prompt_has(description, "邪恶", "阴森", "诡异"):
        bg1, bg2, accent = PORTRAIT_MOOD_KEYWORDS[0][1]

    eye_count = 1 if portrait_prompt_has(description, "独眼") else (
        rng.randint(5, 9) if portrait_prompt_has(description, "多眼", "千眼", "复眼") else rng.randint(2, 3)
    )
    has_tentacles = portrait_prompt_has(description, "触手", "章鱼", "克苏鲁")
    has_teeth = portrait_prompt_has(description, "尖牙", "獠牙", "利齿")
    has_scales = portrait_prompt_has(description, "鳞片", "甲壳", "鱼鳞")

    cx, cy = 150, 155
    parts = [
        '<svg viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        f'<radialGradient id="bg" cx="50%" cy="40%" r="80%">'
        f'<stop offset="0%" stop-color="{bg2}"/><stop offset="100%" stop-color="{bg1}"/></radialGradient>',
        "</defs>",
        '<rect x="0" y="0" width="300" height="300" fill="url(#bg)"/>',
        f'<rect x="6" y="6" width="288" height="288" fill="none" stroke="{accent}" stroke-width="2" opacity="0.5"/>',
    ]

    if has_tentacles:
        for i in range(6):
            angle = i / 6 * 6.283
            tx = cx + math.cos(angle) * 95
            ty = cy + math.sin(angle) * 95 + 20
            mx = cx + math.cos(angle) * 55
            my = cy + math.sin(angle) * 55 + 10
            parts.append(
                f'<path d="M {cx} {cy + 30} Q {mx + rng.randint(-15, 15)} {my}, {tx} {ty}" '
                f'stroke="{accent}" stroke-width="{5 + rng.randint(0, 4)}" fill="none" '
                f'stroke-linecap="round" opacity="0.75"/>'
            )

    blob_points = []
    spikes = 9
    for i in range(spikes):
        angle = i / spikes * 6.283
        radius = 78 + rng.randint(-16, 20)
        blob_points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius * 0.92))
    path_d = f"M {blob_points[0][0]:.1f} {blob_points[0][1]:.1f} "
    for px, py in blob_points[1:]:
        path_d += f"L {px:.1f} {py:.1f} "
    path_d += "Z"
    parts.append(f'<path d="{path_d}" fill="{accent}" opacity="0.28"/>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="70" fill="{bg2}" stroke="{accent}" stroke-width="2.4" opacity="0.95"/>')

    if has_scales:
        for _ in range(24):
            sx = cx + rng.randint(-55, 55)
            sy = cy + rng.randint(-45, 55)
            parts.append(f'<circle cx="{sx}" cy="{sy}" r="4" fill="{accent}" opacity="0.25"/>')

    for i in range(eye_count):
        angle = rng.uniform(0, 6.283) if eye_count > 3 else (i - (eye_count - 1) / 2) * 0.55
        radius = 10 if eye_count > 3 else 0
        ex = cx + math.cos(angle) * (30 if eye_count > 3 else 0) + (0 if eye_count > 3 else angle * 40)
        ex = cx + (math.cos(angle) * 32 if eye_count > 3 else angle * 40)
        ey = cy + (math.sin(angle) * 26 if eye_count > 3 else 0)
        r = 6 if eye_count > 4 else 10
        parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="{r}" fill="#0c0b0a"/>')
        parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="{r * 0.45:.1f}" fill="{accent}"/>')

    if has_teeth:
        for i in range(7):
            tx = cx - 36 + i * 12
            parts.append(f'<path d="M {tx} {cy + 34} L {tx + 5} {cy + 48} L {tx + 10} {cy + 34} Z" fill="#e8e2d0"/>')

    parts.append("</svg>")
    return "\n".join(parts)


def build_generated_portrait_svg(description, mode):
    description = normalize_item_description(description)
    seed = hashlib.sha256(f"portrait:{mode}:{description}".encode("utf-8")).hexdigest()
    rng = random.Random(seed)
    if mode == "monster":
        return build_monster_portrait_svg(description, rng)
    return build_human_portrait_svg(description, rng)


def save_generated_portrait(subdir, file_stem, description, mode):
    target_dir = os.path.join(PORTRAITS_GEN_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    file_name = f"{file_stem}.svg"
    file_path = os.path.join(target_dir, file_name)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(build_generated_portrait_svg(description, mode))
    return f"portraits_gen/{subdir}/{file_name}"


NPC_DESCRIPTION_MAX = 300
NPC_NAME_MAX = 30
NPC_MAX_PER_ROOM = 60

NPC_ATTR_LABELS = {
    "STR": "力量",
    "CON": "体质",
    "SIZ": "体型",
    "DEX": "敏捷",
    "APP": "外貌",
    "INT": "智力",
    "POW": "意志",
    "EDU": "教育",
    "LUK": "幸运",
}

NPC_ATTR_KEYWORDS = [
    (("强壮", "壮硕", "健壮", "魁梧", "肌肉"), "STR", 15),
    (("瘦弱", "虚弱", "病弱", "羸弱"), "STR", -15),
    (("肥胖", "臃肿", "壮实"), "SIZ", 15),
    (("矮小", "瘦小", "娇小"), "SIZ", -15),
    (("敏捷", "灵活", "矫健", "身手"), "DEX", 15),
    (("笨拙", "迟缓", "臃肿"), "DEX", -10),
    (("英俊", "美丽", "漂亮", "迷人", "优雅"), "APP", 15),
    (("丑陋", "邋遢", "猥琐", "肮脏"), "APP", -15),
    (("聪明", "博学", "睿智", "精明", "机智"), "INT", 15),
    (("愚笨", "头脑简单", "迟钝"), "INT", -15),
    (("意志坚定", "冷静", "坚毅", "沉着"), "POW", 15),
    (("胆小", "懦弱", "神经质", "怯懦"), "POW", -10),
    (("高学历", "教授", "学者", "博士"), "EDU", 15),
    (("辍学", "文盲", "粗鄙"), "EDU", -15),
    (("幸运", "走运"), "LUK", 15),
    (("倒霉", "不幸", "厄运"), "LUK", -15),
]

NPC_OCCUPATION_PROFILES = [
    (("医生", "大夫", "外科", "医师", "护士"), "医生", {"医学": 70, "急救": 60, "心理学": 40}),
    (("学者", "教授", "图书馆", "考古", "研究员"), "学者", {"图书馆使用": 65, "历史": 55, "神秘学": 40}),
    (("水手", "船员", "渔夫", "航海", "船长"), "水手", {"游泳": 70, "驾驶：船艇": 60, "导航": 55}),
    (("警察", "警探", "警官", "探员", "刑警"), "警探", {"侦查": 65, "心理学": 50, "格斗：斗殴": 55, "法律": 40}),
    (("打手", "保镖", "黑帮", "暴徒", "打手"), "打手", {"格斗：斗殴": 70, "恐吓": 60, "闪避": 50}),
    (("记者", "编辑", "作家"), "记者", {"话术": 60, "图书馆使用": 55, "心理学": 45}),
    (("神职", "牧师", "修女", "教士", "僧侣"), "神职人员", {"话术": 55, "神秘学": 45, "心理学": 40}),
    (("贵族", "富商", "商人", "富豪", "老板"), "商人", {"信用评级": 75, "话术": 55, "会计": 50}),
    (("工人", "劳工", "矿工", "农夫", "苦力"), "劳工", {"操作重型机械": 50, "急救": 30, "电气维修": 40}),
    (("艺术家", "画家", "音乐家", "演员"), "艺术家", {"话术": 50, "心理学": 40, "妙手": 45}),
    (("司机", "出租车", "车夫", "货运"), "司机", {"驾驶：汽车": 65, "导航": 45}),
    (("士兵", "军人", "退伍", "佣兵"), "军人", {"格斗：斗殴": 60, "射击：步枪": 55, "闪避": 45}),
    (("小孩", "孩子", "儿童", "少年"), "孩童", {"闪避": 40, "潜行": 35}),
    (("老人", "老者", "长者", "老太"), "老者", {"图书馆使用": 45, "历史": 40}),
    (("教师", "老师", "校长"), "教师", {"话术": 55, "图书馆使用": 50, "心理学": 40}),
]

NPC_PERSONALITY_KEYWORDS = [
    (("多疑", "警惕", "戒备"), "生性多疑，对陌生人抱有戒心，很难轻易信任别人。"),
    (("善良", "温柔", "热心"), "心地善良，乐于助人，容易同情弱者。"),
    (("冷酷", "无情", "狠毒"), "性格冷酷，做事不择手段，很少表露真实情感。"),
    (("贪婪", "势利", "爱财"), "贪图钱财与利益，容易被金钱收买。"),
    (("勇敢", "无畏", "果断"), "勇敢果决，面对危险毫不退缩。"),
    (("胆小", "懦弱", "畏缩"), "胆小怕事，遇到麻烦第一反应是逃避。"),
    (("固执", "顽固", "倔强"), "性格固执己见，一旦认定的事很难被说服改变。"),
    (("幽默", "风趣", "开朗"), "性格开朗风趣，喜欢用玩笑化解紧张气氛。"),
    (("神秘", "古怪", "诡异"), "行事神秘古怪，让人捉摸不透其真实意图。"),
    (("忠诚", "可靠", "值得信赖"), "忠诚可靠，一旦认定的人或事便会全力守护。"),
    (("孤僻", "冷漠", "沉默"), "性格孤僻沉默，不喜与人深交，独来独往。"),
    (("野心", "权欲", "算计"), "野心勃勃，善于算计，一切行动都服务于自己的目标。"),
]

NPC_DEFAULT_PERSONALITIES = [
    "看似普通，实则心思深藏，具体秉性还需在互动中细细揣摩。",
    "喜怒不形于色，很难从表面看出真实想法。",
    "为人处世自有一套原则，外人难以轻易读懂。",
]

NPC_BASE_SKILLS = {
    "侦查": 25,
    "聆听": 20,
    "潜行": 20,
    "说服": 10,
    "恐吓": 15,
    "心理学": 10,
}


def roll_nd6(count):
    return sum(random.randint(1, 6) for _ in range(count))


def roll_npc_attribute(key):
    if key in ("SIZ", "INT", "EDU"):
        return (roll_nd6(2) + 6) * 5
    return roll_nd6(3) * 5


def generate_npc_from_description(name, description, created_by):
    description = normalize_item_description(description)[:NPC_DESCRIPTION_MAX]

    values = {key: roll_npc_attribute(key) for key in NPC_ATTR_LABELS}
    for keywords, attr_key, bonus in NPC_ATTR_KEYWORDS:
        if item_prompt_has(description, *keywords):
            values[attr_key] = values[attr_key] + bonus
    for key in values:
        values[key] = max(15, min(90, values[key]))

    attributes = [
        {"key": key, "label": NPC_ATTR_LABELS[key], "value": values[key], "half": values[key] // 2}
        for key in ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUK")
    ]

    skills = dict(NPC_BASE_SKILLS)
    skills["母语"] = values["EDU"]
    skills["闪避"] = values["DEX"] // 2

    occupation = "平民"
    for keywords, occ_label, occ_skills in NPC_OCCUPATION_PROFILES:
        if item_prompt_has(description, *keywords):
            occupation = occ_label
            for skill_name, skill_value in occ_skills.items():
                skills[skill_name] = max(skills.get(skill_name, 0), skill_value)
            break

    skill_list = [
        {"name": skill_name, "value": skill_value}
        for skill_name, skill_value in sorted(skills.items(), key=lambda item: -item[1])
    ]

    traits = [
        sentence
        for keywords, sentence in NPC_PERSONALITY_KEYWORDS
        if item_prompt_has(description, *keywords)
    ]
    if not traits:
        traits = [random.choice(NPC_DEFAULT_PERSONALITIES)]
    personality = " ".join(dict.fromkeys(traits))

    hp = max(1, round((values["CON"] + values["SIZ"]) / 10))
    san = min(99, values["POW"])
    mp = max(0, round(values["POW"] / 5))

    return {
        "name": (name or "无名之人")[:NPC_NAME_MAX],
        "description": description,
        "occupation": occupation,
        "attributes": attributes,
        "skills": skill_list,
        "personality": personality,
        "hp": hp,
        "hp_max": hp,
        "san": san,
        "san_max": san,
        "mp": mp,
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def npc_to_client(npc):
    return {
        "id": npc.get("id"),
        "name": npc.get("name"),
        "description": npc.get("description"),
        "occupation": npc.get("occupation"),
        "attributes": npc.get("attributes", []),
        "skills": npc.get("skills", []),
        "personality": npc.get("personality"),
        "portrait": npc.get("portrait"),
        "hp": npc.get("hp"),
        "hp_max": npc.get("hp_max"),
        "san": npc.get("san"),
        "san_max": npc.get("san_max"),
        "mp": npc.get("mp"),
        "created_by": npc.get("created_by"),
        "created_at": npc.get("created_at"),
    }


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

    def end_headers(self):
        path = urlparse(getattr(self, "path", "") or "").path
        if path == "/" or path.endswith((".html", ".css", ".js")):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

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
            room_list.sort(key=lambda item: not item.get("permanent"))
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

        if path == "/api/backpack":
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
            json_response(self, 200, {"ok": True, **backpack_payload(room, user, is_kp)})
            return

        if path == "/api/timeline":
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
            timeline = ensure_room_timeline(room)
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "is_kp": is_kp,
                    "can_manage": is_kp,
                    "timeline": timeline_to_client(timeline),
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

        if path == "/api/room/npcs":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 404 if error == "房间不存在" else 403, {"ok": False, "error": error})
                return
            npcs = room.get("npcs", [])
            json_response(self, 200, {"ok": True, "npcs": [npc_to_client(item) for item in npcs]})
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
                rooms[room_id] = new_room_record(name, user)
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
                if room.get("permanent"):
                    json_response(self, 400, {"ok": False, "error": "方舟是常驻测试房，不能解散"})
                    return
                clear_room_member_sessions(room)
                delete_room_scene_files(room_id)
                delete_room_handout_files(room_id)
                delete_room_item_files(room_id)
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

        if path == "/api/room/opposed":
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
                db = load_characters_db()
                side_a, err_a = resolve_opposed_side(body.get("side_a"), db)
                if err_a:
                    json_response(self, 400, {"ok": False, "error": f"甲方：{err_a}"})
                    return
                side_b, err_b = resolve_opposed_side(body.get("side_b"), db)
                if err_b:
                    json_response(self, 400, {"ok": False, "error": f"乙方：{err_b}"})
                    return

                value_a = roll_die("d100")
                value_b = roll_die("d100")
                outcome_a = evaluate_skill_check(value_a, side_a["skill_value"])
                outcome_b = evaluate_skill_check(value_b, side_b["skill_value"])

                rank_a = OPPOSED_RANK[outcome_a["type"]]
                rank_b = OPPOSED_RANK[outcome_b["type"]]
                if rank_a > rank_b:
                    winner = "a"
                elif rank_b > rank_a:
                    winner = "b"
                elif side_a["skill_value"] > side_b["skill_value"]:
                    winner = "a"
                elif side_b["skill_value"] > side_a["skill_value"]:
                    winner = "b"
                else:
                    winner = "draw"

                def result_for(is_winning_side):
                    if winner == "draw":
                        return "draw"
                    return "win" if is_winning_side else "lose"

                timestamp = datetime.now(timezone.utc).isoformat()
                room = rooms[room_id]
                group_id = room.get("next_roll_id", 1)

                record_a = {
                    "dice": "d100",
                    "label": f"对抗检定：{side_a['label']}",
                    "value": value_a,
                    "hidden": False,
                    "roller": user,
                    "timestamp": timestamp,
                    "check_type": outcome_a["type"],
                    "check_label": outcome_a["label"],
                    "skill_value": outcome_a["skill_value"],
                    "opposed_group_id": group_id,
                    "opposed_result": result_for(winner == "a"),
                }
                record_b = {
                    "dice": "d100",
                    "label": f"对抗检定：{side_b['label']}",
                    "value": value_b,
                    "hidden": False,
                    "roller": user,
                    "timestamp": timestamp,
                    "check_type": outcome_b["type"],
                    "check_label": outcome_b["label"],
                    "skill_value": outcome_b["skill_value"],
                    "opposed_group_id": group_id,
                    "opposed_result": result_for(winner == "b"),
                }

                roll_id_a = room.get("next_roll_id", 1)
                record_a["id"] = roll_id_a
                room["next_roll_id"] = roll_id_a + 1
                roll_id_b = room["next_roll_id"]
                record_b["id"] = roll_id_b
                room["next_roll_id"] = roll_id_b + 1
                room["last_roll_id"] = roll_id_b

                room.setdefault("rolls", []).append(record_a)
                room["rolls"].append(record_b)
                if len(room["rolls"]) > 200:
                    room["rolls"] = room["rolls"][-200:]
                save_rooms()

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "winner": winner,
                    "side_a": roll_to_client(record_a),
                    "side_b": roll_to_client(record_b),
                },
            )
            return

        if path == "/api/timeline":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            title, entry_body, payload_error = normalize_timeline_entry_payload(body)
            if payload_error:
                json_response(self, 400, {"ok": False, "error": payload_error})
                return

            with data_lock:
                room = rooms[room_id]
                timeline = ensure_room_timeline(room)
                entries = timeline.setdefault("entries", [])
                if len(entries) >= TIMELINE_MAX_ENTRIES:
                    json_response(self, 400, {"ok": False, "error": f"时间线最多保留 {TIMELINE_MAX_ENTRIES} 个节点"})
                    return
                entry_id = int(timeline.get("next_entry_id", 1))
                now = datetime.now(timezone.utc).isoformat()
                entry = {
                    "id": entry_id,
                    "title": title,
                    "body": entry_body,
                    "created_at": now,
                    "created_by": user,
                    "updated_at": now,
                }
                entries.append(entry)
                timeline["next_entry_id"] = entry_id + 1
                if timeline.get("current_entry_id") is None:
                    timeline["current_entry_id"] = entry_id
                timeline["updated_at"] = now
                save_rooms()
                saved = timeline_to_client(timeline)

            json_response(self, 200, {"ok": True, "timeline": saved})
            return

        if path == "/api/timeline/update":
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
                entry_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效的时间线节点 ID"})
                return

            title, entry_body, payload_error = normalize_timeline_entry_payload(body)
            if payload_error:
                json_response(self, 400, {"ok": False, "error": payload_error})
                return

            with data_lock:
                room = rooms[room_id]
                timeline = ensure_room_timeline(room)
                entry = next((item for item in timeline.get("entries", []) if item.get("id") == entry_id), None)
                if not entry:
                    json_response(self, 404, {"ok": False, "error": "时间线节点不存在"})
                    return
                now = datetime.now(timezone.utc).isoformat()
                entry["title"] = title
                entry["body"] = entry_body
                entry["updated_at"] = now
                timeline["updated_at"] = now
                save_rooms()
                saved = timeline_to_client(timeline)

            json_response(self, 200, {"ok": True, "timeline": saved})
            return

        if path == "/api/timeline/delete":
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
                entry_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效的时间线节点 ID"})
                return

            with data_lock:
                room = rooms[room_id]
                timeline = ensure_room_timeline(room)
                entries = timeline.get("entries", [])
                new_entries = [item for item in entries if item.get("id") != entry_id]
                if len(new_entries) == len(entries):
                    json_response(self, 404, {"ok": False, "error": "时间线节点不存在"})
                    return
                timeline["entries"] = new_entries
                if timeline.get("current_entry_id") == entry_id:
                    timeline["current_entry_id"] = new_entries[-1].get("id") if new_entries else None
                timeline["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()
                saved = timeline_to_client(timeline)

            json_response(self, 200, {"ok": True, "timeline": saved})
            return

        if path == "/api/timeline/current":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            raw_id = body.get("id")
            entry_id = None
            if raw_id not in (None, ""):
                try:
                    entry_id = int(raw_id)
                except (TypeError, ValueError):
                    json_response(self, 400, {"ok": False, "error": "无效的时间线节点 ID"})
                    return

            with data_lock:
                room = rooms[room_id]
                timeline = ensure_room_timeline(room)
                if entry_id is not None and not any(item.get("id") == entry_id for item in timeline.get("entries", [])):
                    json_response(self, 404, {"ok": False, "error": "时间线节点不存在"})
                    return
                timeline["current_entry_id"] = entry_id
                timeline["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()
                saved = timeline_to_client(timeline)

            json_response(self, 200, {"ok": True, "timeline": saved})
            return

        if path == "/api/timeline/move":
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
                entry_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效的时间线节点 ID"})
                return
            direction = str(body.get("direction", "")).strip()
            if direction not in ("up", "down"):
                json_response(self, 400, {"ok": False, "error": "无效的移动方向"})
                return

            with data_lock:
                room = rooms[room_id]
                timeline = ensure_room_timeline(room)
                entries = timeline.get("entries", [])
                index = next((idx for idx, item in enumerate(entries) if item.get("id") == entry_id), None)
                if index is None:
                    json_response(self, 404, {"ok": False, "error": "时间线节点不存在"})
                    return
                target_index = index - 1 if direction == "up" else index + 1
                if 0 <= target_index < len(entries):
                    entries[index], entries[target_index] = entries[target_index], entries[index]
                    timeline["updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_rooms()
                saved = timeline_to_client(timeline)

            json_response(self, 200, {"ok": True, "timeline": saved})
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
                portrait_desc = normalize_item_description(str(item.get("portrait_description", "")))[:PORTRAIT_DESCRIPTION_MAX]
                if portrait_desc:
                    monster["image"] = save_generated_portrait(room_id, monster_id, portrait_desc, "monster")
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
                portrait_desc = normalize_item_description(str(body.get("portrait_description", "")))[:PORTRAIT_DESCRIPTION_MAX]
                if portrait_desc:
                    monster["image"] = save_generated_portrait(room_id, monster["id"], portrait_desc, "monster")
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

        if path == "/api/backpack/items":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 404 if error == "房间不存在" else 403, {"ok": False, "error": error})
                return

            owner = normalize_username(str(body.get("owner", "")))
            description_raw = str(body.get("description", ""))
            if len(description_raw) > ITEM_DESCRIPTION_MAX:
                json_response(self, 400, {"ok": False, "error": f"物品描述不能超过 {ITEM_DESCRIPTION_MAX} 字"})
                return
            description = normalize_item_description(description_raw)
            if not description:
                json_response(self, 400, {"ok": False, "error": "请输入物品描述"})
                return

            players = list(room.get("players", []))
            if not players:
                json_response(self, 400, {"ok": False, "error": "房间内暂无玩家，先让玩家加入房间"})
                return
            if owner not in players:
                json_response(self, 400, {"ok": False, "error": "请选择房间内的玩家背包"})
                return

            with data_lock:
                room = rooms[room_id]
                ensure_room_backpacks(room)
                total_items = sum(len(items) for items in room.get("backpacks", {}).values())
                if total_items >= ITEM_MAX_PER_ROOM:
                    json_response(self, 400, {"ok": False, "error": f"本房间物品最多保留 {ITEM_MAX_PER_ROOM} 件"})
                    return
                item_id = int(room.get("next_item_id", 1))
                try:
                    rel_path = save_generated_item_image(room_id, item_id, description)
                except OSError as exc:
                    json_response(self, 500, {"ok": False, "error": f"生成物品图片失败：{exc}"})
                    return
                now = datetime.now(timezone.utc).isoformat()
                item = {
                    "id": item_id,
                    "name": extract_item_name(description),
                    "description": description,
                    "image": rel_path,
                    "owner": owner,
                    "created_at": now,
                    "created_by": user,
                }
                room["backpacks"].setdefault(owner, []).append(item)
                room["next_item_id"] = item_id + 1
                room["last_item_id"] = item_id
                save_rooms()
                payload = backpack_payload(room, user, True)
                saved = backpack_item_to_client(item)

            json_response(self, 200, {"ok": True, "item": saved, **payload})
            return

        if path == "/api/room/npc/generate":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 404 if error == "房间不存在" else 403, {"ok": False, "error": error})
                return

            name_raw = str(body.get("name", "")).strip()
            description_raw = str(body.get("description", ""))
            if len(description_raw) > NPC_DESCRIPTION_MAX:
                json_response(self, 400, {"ok": False, "error": f"描述不能超过 {NPC_DESCRIPTION_MAX} 字"})
                return
            description = normalize_item_description(description_raw)
            if not description:
                json_response(self, 400, {"ok": False, "error": "请输入 NPC 描述"})
                return
            if len(name_raw) > NPC_NAME_MAX:
                json_response(self, 400, {"ok": False, "error": f"名称不能超过 {NPC_NAME_MAX} 字"})
                return

            with data_lock:
                room = rooms[room_id]
                room.setdefault("npcs", [])
                room.setdefault("next_npc_id", 1)
                if len(room["npcs"]) >= NPC_MAX_PER_ROOM:
                    json_response(self, 400, {"ok": False, "error": f"本房间 NPC 最多保留 {NPC_MAX_PER_ROOM} 个"})
                    return
                npc = generate_npc_from_description(name_raw, description, user)
                npc_id = int(room["next_npc_id"])
                npc["id"] = npc_id
                room["next_npc_id"] = npc_id + 1
                npc["portrait"] = save_generated_portrait(room_id, f"npc_{npc_id}", description, "human")
                room["npcs"].append(npc)
                save_rooms()
                saved = npc_to_client(npc)

            json_response(self, 200, {"ok": True, "npc": saved})
            return

        if path == "/api/room/npc/delete":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 404 if error == "房间不存在" else 403, {"ok": False, "error": error})
                return

            try:
                npc_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效 NPC ID"})
                return

            with data_lock:
                room = rooms[room_id]
                npcs = room.setdefault("npcs", [])
                before = len(npcs)
                room["npcs"] = [item for item in npcs if item.get("id") != npc_id]
                if len(room["npcs"]) == before:
                    json_response(self, 404, {"ok": False, "error": "NPC 不存在"})
                    return
                save_rooms()

            json_response(self, 200, {"ok": True})
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

        if path == "/api/scene/generate":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            raw_prompt = str(body.get("prompt", ""))
            if len(raw_prompt) > SCENE_PROMPT_MAX:
                json_response(self, 400, {"ok": False, "error": f"地图描述不能超过 {SCENE_PROMPT_MAX} 字"})
                return
            prompt = normalize_scene_prompt(raw_prompt)
            if not prompt:
                json_response(self, 400, {"ok": False, "error": "请输入地图描述"})
                return

            try:
                rel_path = save_generated_scene_map(room_id, prompt)
            except OSError as exc:
                json_response(self, 500, {"ok": False, "error": f"生成地图失败：{exc}"})
                return

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                scene["image"] = rel_path
                scene["generated"] = True
                scene["generated_prompt"] = prompt
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

        if path == "/api/scene/fog/toggle":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            enabled = bool(body.get("enabled"))

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                fog = ensure_scene_fog(scene)
                fog["enabled"] = enabled
                scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(self, 200, {"ok": True, "scene": scene_to_client(room["scene"])})
            return

        if path == "/api/scene/fog/paint":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            reveal = bool(body.get("reveal"))
            raw_cells = body.get("cells")
            if not isinstance(raw_cells, list):
                json_response(self, 400, {"ok": False, "error": "无效的格子列表"})
                return

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                fog = ensure_scene_fog(scene)
                cols = fog.get("cols", FOG_COLS)
                rows = fog.get("rows", FOG_ROWS)
                revealed = list(fog.get("revealed", "0" * (cols * rows)))
                changed = False
                for cell in raw_cells:
                    if not isinstance(cell, (list, tuple)) or len(cell) != 2:
                        continue
                    try:
                        cx, cy = int(cell[0]), int(cell[1])
                    except (TypeError, ValueError):
                        continue
                    if not (0 <= cx < cols and 0 <= cy < rows):
                        continue
                    index = cy * cols + cx
                    new_val = "1" if reveal else "0"
                    if revealed[index] != new_val:
                        revealed[index] = new_val
                        changed = True
                if changed:
                    fog["revealed"] = "".join(revealed)
                    scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_rooms()

            json_response(self, 200, {"ok": True, "scene": scene_to_client(room["scene"])})
            return

        if path == "/api/scene/fog/reset":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 403 if error == "仅房间 KP 可操作" else 404, {"ok": False, "error": error})
                return

            reveal = bool(body.get("reveal"))

            with data_lock:
                room = rooms[room_id]
                scene = ensure_room_scene(room)
                fog = ensure_scene_fog(scene)
                cols = fog.get("cols", FOG_COLS)
                rows = fog.get("rows", FOG_ROWS)
                fog["revealed"] = ("1" if reveal else "0") * (cols * rows)
                scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_rooms()

            json_response(self, 200, {"ok": True, "scene": scene_to_client(room["scene"])})
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

        if path == "/api/character/portrait":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return

            role = get_user_role(user)
            target_pc_id = str(body.get("target", "")).strip()
            description_raw = str(body.get("description", ""))
            if len(description_raw) > PORTRAIT_DESCRIPTION_MAX:
                json_response(self, 400, {"ok": False, "error": f"描述不能超过 {PORTRAIT_DESCRIPTION_MAX} 字"})
                return
            description = normalize_item_description(description_raw)
            if not description:
                json_response(self, 400, {"ok": False, "error": "请输入外貌描述"})
                return

            with data_lock:
                db = load_characters_db()
                if target_pc_id:
                    if not can_view_all_characters(role):
                        json_response(self, 403, {"ok": False, "error": "无权为该角色生成头像"})
                        return
                    pc, _, _ = find_pc(db, target_pc_id)
                    if not pc:
                        json_response(self, 404, {"ok": False, "error": "人物卡不存在"})
                        return
                    pc_id = target_pc_id
                else:
                    if role != ROLE_PLAYER:
                        json_response(self, 400, {"ok": False, "error": "请先选择调查员人物卡"})
                        return
                    active_pc_id = get_session_active_pc_id(self)
                    pc = get_active_pc_for_pl(db, user, active_pc_id)
                    if not pc:
                        json_response(self, 404, {"ok": False, "error": "请先选择或新建人物卡"})
                        return
                    pc_id = pc.get("pc_id")

                pc["portrait"] = save_generated_portrait("pc", pc_id, description, "human")
                save_characters_db(db)
                portrait = pc["portrait"]

            json_response(self, 200, {"ok": True, "target": pc_id, "portrait": portrait})
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
            role = str(body.get("role", ROLE_PLAYER)).strip() or ROLE_PLAYER
            if role not in (ROLE_PLAYER, ROLE_KP):
                json_response(self, 400, {"ok": False, "error": "请选择 PL 或 KP"})
                return
            with data_lock:
                users = load_users()
                if username in users:
                    json_response(self, 409, {"ok": False, "error": "用户名已被注册"})
                    return
                users[username] = {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "role": role,
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
                    "role": role,
                    "room": room_payload,
                    **user_pc_payload(username, role, session_data.get("active_pc_id")),
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

        if path == "/api/backpack/items":
            user = get_current_user(self)
            if not user:
                json_response(self, 401, {"ok": False, "error": "请先登录"})
                return
            room_id = get_user_room_id(user)
            room, error = require_room_kp(user, room_id)
            if error:
                json_response(self, 404 if error == "房间不存在" else 403, {"ok": False, "error": error})
                return

            owner = normalize_username(str(body.get("owner", "")))
            try:
                item_id = int(body.get("id"))
            except (TypeError, ValueError):
                json_response(self, 400, {"ok": False, "error": "无效的物品 ID"})
                return
            if not owner:
                json_response(self, 400, {"ok": False, "error": "请选择背包"})
                return

            with data_lock:
                room = rooms[room_id]
                ensure_room_backpacks(room)
                items = room.get("backpacks", {}).get(owner, [])
                target = next((item for item in items if item.get("id") == item_id), None)
                if not target:
                    json_response(self, 404, {"ok": False, "error": "物品不存在"})
                    return
                room["backpacks"][owner] = [item for item in items if item.get("id") != item_id]
                delete_item_image_file(target)
                room["last_item_id"] = max(room.get("last_item_id", 0), item_id)
                save_rooms()
                payload = backpack_payload(room, user, True)

            json_response(self, 200, {"ok": True, **payload})
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
    os.makedirs(ITEMS_DIR, exist_ok=True)
    os.makedirs(PORTRAITS_GEN_DIR, exist_ok=True)
    init_characters_db_file()
    ensure_builtin_test_user()
    init_rooms()
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        local_ip = get_local_ip()
        print("服务已启动")
        print(f"  本机访问: http://127.0.0.1:{PORT}")
        print(f"  局域网访问: http://{local_ip}:{PORT}")
        print("  公网访问: http://<你的公网IP>:8080")
        print("按 Ctrl+C 停止")
        httpd.serve_forever()
