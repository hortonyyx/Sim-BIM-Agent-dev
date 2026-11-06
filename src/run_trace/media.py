"""Store verified local copies of the images attached to a recorded call."""

import base64
import binascii
import gzip
import hashlib
import io
import json
from pathlib import Path
import re

from PIL import Image


_HASH = re.compile(r"[0-9a-f]{64}")
_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}


class TraceMedia:
    def __init__(self, bundle: Path):
        self.bundle = bundle
        self.paths = {}

    def store(self, data: bytes) -> str | None:
        digest = hashlib.sha256(data).hexdigest()
        if digest not in self.paths:
            try:
                with Image.open(io.BytesIO(data)) as image:
                    extension = _FORMATS.get(image.format)
                    if not extension:
                        return None
                    image.verify()
            except (ValueError, SyntaxError, OSError, Image.DecompressionBombError):
                return None
            relative = f"media/{digest}.{extension}"
            path = self.bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.paths[digest] = relative
        return self.paths[digest]

    def stored_ref(self, ref, journal: Path):
        if not isinstance(ref, dict):
            return None
        digest = ref.get("sha256")
        mime = ref.get("media_type", ref.get("mime_type"))
        if not isinstance(mime, str) or not mime.startswith("image/") or not isinstance(digest, str) or not _HASH.fullmatch(digest):
            return None
        paths = (journal / "blobs" / digest,
                 journal / "dsh-home" / "attachments" / "v1" / "objects" / digest[:2] / digest)
        for path in paths:
            if path.is_file() and path.resolve().is_relative_to(journal.resolve()):
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() == digest:
                    return self.store(data)
        return None

    def event_images(self, event, journal: Path, wire: Path | None):
        items = []
        turn = event.get("turn")
        if event["kind"] == "model" and wire and isinstance(turn, int):
            request = wire / f"{turn:02d}-out.json.gz"
            if request.is_file():
                try:
                    with gzip.open(request, "rt", encoding="utf-8") as f:
                        body = json.load(f)
                    messages = body.get("messages", body.get("input", [])) if isinstance(body, dict) else []
                except (ValueError, OSError, EOFError):
                    return items
                for index, message in enumerate(messages, 1):
                    parts = message.get("content", message.get("output")) if isinstance(message, dict) else None
                    if not isinstance(parts, list):
                        continue
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        url = part.get("image_url", {})
                        url = url.get("url", "") if isinstance(url, dict) else url
                        if not isinstance(url, str) or not url.startswith("data:image/"):
                            continue
                        header, _, encoded = url.partition(",")
                        if not header.endswith(";base64"):
                            continue
                        try:
                            data = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error):
                            continue
                        if relative := self.store(data):
                            items.append({"src": relative, "label": f"消息 {index} · {message.get('role', '')} · 图 {len(items)+1}"})
                # No journal image should be misrepresented as a wire attachment.
                return items

        detail = event.get("detail", {})
        if event["kind"] == "model" and "request_event_id" in detail:
            # An original raster may be resized before it reaches the adapter.
            refs = [(ref.get("request_reference"), ref.get("sent")) for ref in detail.get("images", [])]
            if not refs:
                refs = [(ref.get("location"), ref.get("image")) for ref in detail.get("request_image_references", [])]
            for pointer, ref in refs:
                if relative := self.stored_ref(ref, journal):
                    items.append({"src": relative, "label": f"请求用图 · {pointer or '位置未记录'}"})
            return items

        def visit(node):
            if isinstance(node, list):
                for item in node:
                    visit(item)
            elif isinstance(node, dict):
                if relative := self.stored_ref(node, journal):
                    if not any(item["src"] == relative for item in items):
                        items.append({"src": relative, "label": f"记录附件 · 图 {len(items)+1}"})
                for item in node.values():
                    if isinstance(item, (list, dict)):
                        visit(item)
        visit(detail)
        return items
