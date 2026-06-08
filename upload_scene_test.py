#!/usr/bin/env python3
"""以 KP 身份登录并上传测试场景图。"""

import base64
import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://47.81.210.196:8080"
KP_USER = sys.argv[2] if len(sys.argv) > 2 else "哞之"
ROOM_NAME = sys.argv[3] if len(sys.argv) > 3 else "场景测试房"
IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "scene_test_map.png"


def api(opener, path, payload=None, method=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method or ("POST" if payload is not None else "GET"),
    )
    with opener.open(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    if not IMAGE_PATH.is_file():
        raise SystemExit(f"找不到场景图: {IMAGE_PATH}")

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login = api(opener, "/api/login", {"username": KP_USER})
    if not login.get("ok"):
        raise SystemExit(f"登录失败: {login.get('error')}")
    print(f"已登录: {KP_USER} ({login.get('role')})")

    room = login.get("room")
    if not room:
        created = api(opener, "/api/rooms", {"name": ROOM_NAME})
        if not created.get("ok"):
            raise SystemExit(f"建房失败: {created.get('error')}")
        room = created["room"]
        print(f"已创建房间: {room['name']} ({room['id']})")
    else:
        print(f"已在房间: {room['name']} ({room['id']})")

    raw = IMAGE_PATH.read_bytes()
    file_b64 = base64.b64encode(raw).decode("ascii")
    uploaded = api(
        opener,
        "/api/scene/image",
        {"filename": IMAGE_PATH.name, "file": file_b64},
    )
    if not uploaded.get("ok"):
        raise SystemExit(f"上传失败: {uploaded.get('error')}")
    print(f"场景图已上传: {uploaded['scene']['image']}")

    synced = api(opener, "/api/scene/sync-pcs", {})
    if synced.get("ok"):
        print(f"已同步 PC 棋子: +{synced.get('added', 0)} 个")

    npcs = [
        ("深潜者祭司", 0.72, 0.28),
        ("神父", 0.58, 0.42),
    ]
    for label, x, y in npcs:
        result = api(
            opener,
            "/api/scene/token",
            {"type": "npc", "label": label, "x": x, "y": y},
        )
        if result.get("ok"):
            print(f"已添加 NPC: {label}")

    scene = api(opener, "/api/scene")
    print(json.dumps(scene.get("scene"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
