from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4
import asyncio
import base64
import binascii
import contextlib
import json
import logging
import mimetypes
import os

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trichat")

MAX_HISTORY_MESSAGES = 100
MAX_FILE_SIZE = 5 * 1024 * 1024
ROOM_HISTORY_HOURS = int(os.getenv("ROOM_HISTORY_HOURS", "5"))
HISTORY_CACHE_SECONDS = int(os.getenv("HISTORY_CACHE_SECONDS", "30"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "600"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "chat-files")

app = FastAPI(title="Tri-Chat API", description="Temporary anonymous chat rooms")
app.mount("/static", StaticFiles(directory="static"), name="static")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> str:
    return now_utc().isoformat()


def expiry_time() -> str:
    return (now_utc() + timedelta(hours=ROOM_HISTORY_HOURS)).isoformat()


def clean_text(value: object, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return value.strip()[:limit]


def safe_file_name(file_name: str) -> str:
    cleaned = Path(file_name).name.strip()[:100]
    return cleaned or "upload.bin"


def storage_path_for(room: str, file_name: str) -> str:
    room_prefix = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in room)
    return f"{room_prefix}/{uuid4().hex}-{safe_file_name(file_name)}"


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_expired(message: dict) -> bool:
    expires_at = parse_iso_datetime(message.get("expiresAt", ""))
    return bool(expires_at and expires_at <= now_utc())


class SupabaseStore:
    def __init__(self, url: str, key: str, bucket: str):
        self.url = url
        self.key = key
        self.bucket = bucket

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }

    async def fetch_history(self, room: str) -> List[dict]:
        if not self.enabled:
            return []

        params = {
            "select": "*",
            "room": f"eq.{room}",
            "expires_at": f"gt.{utc_now()}",
            "order": "created_at.desc",
            "limit": str(MAX_HISTORY_MESSAGES),
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self.url}/rest/v1/messages",
                headers=self.headers,
                params=params,
            )
            response.raise_for_status()

        rows = list(reversed(response.json()))
        return [self.row_to_message(row) for row in rows]

    async def save_message(self, message: dict) -> None:
        if not self.enabled or message.get("type") == "system":
            return

        payload = {
            "room": message["room"],
            "username": message["username"],
            "message_type": message["type"],
            "text": message.get("text"),
            "file_url": message.get("fileUrl"),
            "file_path": message.get("filePath"),
            "file_name": message.get("fileName"),
            "file_type": message.get("fileType"),
            "file_size": message.get("fileSize"),
            "created_at": message["timestamp"],
            "expires_at": message["expiresAt"],
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.url}/rest/v1/messages",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()

    async def upload_file(self, room: str, file_name: str, file_type: str, file_bytes: bytes) -> tuple[str, str]:
        if not self.enabled:
            raise RuntimeError("Supabase is not configured")

        path = storage_path_for(room, file_name)
        content_type = file_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.url}/storage/v1/object/{self.bucket}/{path}",
                headers={
                    **self.headers,
                    "Content-Type": content_type,
                    "x-upsert": "false",
                },
                content=file_bytes,
            )
            response.raise_for_status()

        public_url = f"{self.url}/storage/v1/object/public/{self.bucket}/{path}"
        return public_url, path

    async def delete_storage_objects(self, paths: List[str]) -> None:
        if not self.enabled or not paths:
            return

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                "DELETE",
                f"{self.url}/storage/v1/object/{self.bucket}",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"prefixes": paths},
            )
            response.raise_for_status()

    async def cleanup_expired(self) -> int:
        if not self.enabled:
            return 0

        current_time = utc_now()
        async with httpx.AsyncClient(timeout=30) as client:
            select_response = await client.get(
                f"{self.url}/rest/v1/messages",
                headers=self.headers,
                params={
                    "select": "id,file_path",
                    "expires_at": f"lte.{current_time}",
                    "limit": "500",
                },
            )
            select_response.raise_for_status()
            expired_rows = select_response.json()

            if not expired_rows:
                return 0

            file_paths = [row["file_path"] for row in expired_rows if row.get("file_path")]
            if file_paths:
                await self.delete_storage_objects(file_paths)

            delete_response = await client.delete(
                f"{self.url}/rest/v1/messages",
                headers=self.headers,
                params={"expires_at": f"lte.{current_time}"},
            )
            delete_response.raise_for_status()

        return len(expired_rows)

    def row_to_message(self, row: dict) -> dict:
        message = {
            "type": row["message_type"],
            "username": row["username"],
            "timestamp": row["created_at"],
            "expiresAt": row["expires_at"],
            "room": row["room"],
        }

        if row["message_type"] == "text":
            message["text"] = row.get("text") or ""
        elif row["message_type"] == "file":
            message.update(
                {
                    "fileUrl": row.get("file_url") or "",
                    "fileName": row.get("file_name") or "download",
                    "fileType": row.get("file_type") or "application/octet-stream",
                    "fileSize": row.get("file_size") or 0,
                }
            )

        return message


store = SupabaseStore(SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET)


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[Dict]] = {}
        self.fallback_history: Dict[str, List[Dict]] = {}
        self.history_cache: Dict[str, Dict] = {}

    async def connect(self, websocket: WebSocket, room: str, username: str):
        await websocket.accept()

        if room not in self.active_connections:
            self.active_connections[room] = []
            self.fallback_history[room] = []

        self.active_connections[room].append(
            {
                "websocket": websocket,
                "username": username,
                "joined_at": utc_now(),
            }
        )

        for message in await self.get_history(room):
            await websocket.send_text(json.dumps(message))

        await self.broadcast_to_room(
            room,
            {
                "type": "system",
                "message": f"{username} joined the room",
                "timestamp": utc_now(),
                "room": room,
            },
            persist=False,
        )
        await self.broadcast_user_list(room)

    async def get_history(self, room: str) -> List[dict]:
        self.prune_fallback_history(room)
        cached = self.history_cache.get(room)
        if cached and cached["expires_at"] > now_utc():
            return cached["messages"]

        if store.enabled:
            try:
                messages = await store.fetch_history(room)
                self.cache_history(room, messages)
                return messages
            except httpx.HTTPError as exc:
                logger.warning("Could not fetch Supabase history: %s", exc)

        messages = self.fallback_history.get(room, [])
        self.cache_history(room, messages)
        return messages

    def cache_history(self, room: str, messages: List[dict]) -> None:
        self.history_cache[room] = {
            "messages": [message for message in messages if not is_expired(message)],
            "expires_at": now_utc() + timedelta(seconds=HISTORY_CACHE_SECONDS),
        }

    def append_to_cache(self, room: str, message: dict) -> None:
        cached = self.history_cache.get(room)
        if not cached:
            return

        cached["messages"].append(message)
        cached["messages"] = cached["messages"][-MAX_HISTORY_MESSAGES:]

    def prune_fallback_history(self, room: str) -> None:
        if room in self.fallback_history:
            self.fallback_history[room] = [
                message for message in self.fallback_history[room] if not is_expired(message)
            ][-MAX_HISTORY_MESSAGES:]

    def disconnect(self, websocket: WebSocket, room: str) -> Optional[str]:
        if room not in self.active_connections:
            return None

        for conn in list(self.active_connections[room]):
            if conn["websocket"] == websocket:
                self.active_connections[room].remove(conn)
                username = conn["username"]
                if not self.active_connections[room]:
                    self.active_connections.pop(room, None)
                return username

        return None

    async def broadcast_to_room(self, room: str, message: dict, persist: bool = True):
        if persist:
            if store.enabled:
                try:
                    await store.save_message(message)
                except httpx.HTTPError as exc:
                    logger.warning("Could not save message to Supabase: %s", exc)
                    self.add_fallback_message(room, message)
            else:
                self.add_fallback_message(room, message)

            self.append_to_cache(room, message)

        if room not in self.active_connections:
            return

        disconnected = []
        for connection_info in list(self.active_connections[room]):
            try:
                await connection_info["websocket"].send_text(json.dumps(message))
            except RuntimeError:
                disconnected.append(connection_info)

        for conn in disconnected:
            if conn in self.active_connections.get(room, []):
                self.active_connections[room].remove(conn)

    def add_fallback_message(self, room: str, message: dict) -> None:
        self.fallback_history.setdefault(room, []).append(message)
        self.prune_fallback_history(room)

    async def broadcast_user_list(self, room: str):
        if room not in self.active_connections:
            return

        users_message = {
            "type": "user_list",
            "users": self.get_room_users(room),
            "room": room,
        }

        disconnected = []
        for connection_info in list(self.active_connections[room]):
            try:
                await connection_info["websocket"].send_text(json.dumps(users_message))
            except RuntimeError:
                disconnected.append(connection_info)

        for conn in disconnected:
            if conn in self.active_connections.get(room, []):
                self.active_connections[room].remove(conn)

    def get_room_users(self, room: str) -> List[str]:
        if room not in self.active_connections:
            return []
        return [conn["username"] for conn in self.active_connections[room]]

    def clear_cache(self) -> None:
        self.history_cache.clear()


manager = ConnectionManager()
cleanup_task: Optional[asyncio.Task] = None


async def cleanup_loop() -> None:
    while True:
        try:
            deleted_count = await store.cleanup_expired()
            if deleted_count:
                manager.clear_cache()
                logger.info("Deleted %s expired messages/files", deleted_count)
        except httpx.HTTPError as exc:
            logger.warning("Expired cleanup failed: %s", exc)

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    global cleanup_task
    if store.enabled:
        cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info("Temporary cleanup is running every %s seconds", CLEANUP_INTERVAL_SECONDS)
    else:
        logger.warning("Supabase is not configured. Using in-memory fallback only.")


@app.on_event("shutdown")
async def shutdown_event():
    if cleanup_task:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    try:
        with open("templates/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Error: templates/index.html not found</h1>",
            status_code=404,
        )


@app.websocket("/ws/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str, username: str):
    username = clean_text(username, 20)
    room = clean_text(room, 30, "global") or "global"

    if not username:
        await websocket.close(code=1008, reason="Username is required")
        return

    await manager.connect(websocket, room, username)

    try:
        while True:
            data = await websocket.receive_text()

            try:
                message_data = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid message"}))
                continue

            message_type = message_data.get("type")
            if message_type == "text":
                await handle_text_message(room, username, message_data)
            elif message_type == "file":
                await handle_file_message(websocket, room, username, message_data)
    except WebSocketDisconnect:
        disconnected_username = manager.disconnect(websocket, room)
        if disconnected_username:
            await manager.broadcast_to_room(
                room,
                {
                    "type": "system",
                    "message": f"{disconnected_username} left the room",
                    "timestamp": utc_now(),
                    "room": room,
                },
                persist=False,
            )
            await manager.broadcast_user_list(room)


async def handle_text_message(room: str, username: str, message_data: dict):
    text_content = clean_text(message_data.get("text"), 500)
    if not text_content:
        return

    await manager.broadcast_to_room(
        room,
        {
            "type": "text",
            "username": username,
            "text": text_content,
            "timestamp": utc_now(),
            "expiresAt": expiry_time(),
            "room": room,
        },
    )


async def handle_file_message(websocket: WebSocket, room: str, username: str, message_data: dict):
    file_name = safe_file_name(clean_text(message_data.get("fileName"), 100, "upload.bin"))
    file_type = clean_text(message_data.get("fileType"), 120, "application/octet-stream")
    file_data = message_data.get("fileData", "")

    if not store.enabled:
        await websocket.send_text(json.dumps({"type": "error", "message": "File uploads need Supabase configured"}))
        return

    try:
        file_bytes = base64.b64decode(file_data, validate=True)
    except (binascii.Error, TypeError):
        await websocket.send_text(json.dumps({"type": "error", "message": "Invalid file data"}))
        return

    if len(file_bytes) > MAX_FILE_SIZE:
        await websocket.send_text(json.dumps({"type": "error", "message": "File size exceeds 5MB limit"}))
        return

    try:
        file_url, file_path = await store.upload_file(room, file_name, file_type, file_bytes)
    except httpx.HTTPError as exc:
        logger.warning("File upload failed: %s", exc)
        await websocket.send_text(json.dumps({"type": "error", "message": "File upload failed"}))
        return

    await manager.broadcast_to_room(
        room,
        {
            "type": "file",
            "username": username,
            "fileName": file_name,
            "fileType": file_type,
            "fileSize": len(file_bytes),
            "fileUrl": file_url,
            "filePath": file_path,
            "timestamp": utc_now(),
            "expiresAt": expiry_time(),
            "room": room,
        },
    )


@app.get("/api/rooms")
async def get_active_rooms():
    rooms = []
    for room_name, connections in manager.active_connections.items():
        if connections:
            rooms.append(
                {
                    "name": room_name,
                    "user_count": len(connections),
                    "users": [conn["username"] for conn in connections],
                }
            )
    return {"rooms": rooms}


@app.get("/api/rooms/{room}/users")
async def get_room_users(room: str):
    users = manager.get_room_users(room)
    return {
        "room": room,
        "users": users,
        "user_count": len(users),
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
