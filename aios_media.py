"""Images and PDFs as model input for aios-core: content blocks for attachments and tool results.

* Blocks use the Anthropic shape: {"type": "image" | "document", "source": {"type": "base64", ...}}.
  The kernel stores and resends them like any other block; OpenAICompatProvider translates images
  to `image_url` parts and replaces PDFs with a note (most local servers cannot read them).
* The file type comes from the first bytes, never from the extension.
* Media is expensive to resend on every step, so only the newest KEEP_MEDIA blocks stay in an
  agent's history; older ones are replaced by a short note at the start of the next run.
"""
import base64
from pathlib import Path

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
PDF_TYPE = "application/pdf"
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the API's limit for one image
MAX_PDF_BYTES = 20 * 1024 * 1024  # base64 adds a third; one request is capped at 32 MB
KEEP_MEDIA = 4  # images/PDFs kept in history; older ones become a note
DROPPED_NOTE = "[an image or PDF shown earlier was removed from history to save tokens; look at it again if needed]"


class MediaError(ValueError):
    pass


def sniff(head: bytes) -> str | None:
    """Media type from the first bytes of a file, or None if it is not a supported image/PDF."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return PDF_TYPE
    return None


def _limit(media_type: str) -> int:
    return MAX_PDF_BYTES if media_type == PDF_TYPE else MAX_IMAGE_BYTES


def block(data: bytes, name: str = "file") -> dict:
    """Content block for the bytes of an image or PDF. Raises MediaError for anything else."""
    media_type = sniff(data[:16])
    if media_type is None:
        raise MediaError(f"{name}: not a PNG, JPEG, GIF, WebP image or PDF")
    if len(data) > _limit(media_type):
        raise MediaError(f"{name}: {len(data) // 1024} KB is over the {_limit(media_type) // 2**20} MB "
                         f"limit for {media_type}")
    kind = "document" if media_type == PDF_TYPE else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.standard_b64encode(data).decode("ascii")}}


def from_base64(data: str, name: str = "file") -> dict:
    """Content block for base64 data that arrived from elsewhere (e.g. an MCP image result).
    The decoded bytes decide the type, not the sender's label."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise MediaError(f"{name}: invalid base64 data") from None
    return block(raw, name)


def load(path: str | Path) -> dict:
    """Content block for an image or PDF file. The size is checked before the file is read."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise MediaError(f"{path}: no such file")
    with p.open("rb") as f:
        media_type = sniff(f.read(16))
    if media_type is None:
        raise MediaError(f"{p.name}: not a PNG, JPEG, GIF, WebP image or PDF")
    size = p.stat().st_size
    if size > _limit(media_type):
        raise MediaError(f"{p.name}: {size // 1024} KB is over the {_limit(media_type) // 2**20} MB "
                         f"limit for {media_type}")
    return block(p.read_bytes(), p.name)


def is_media(b) -> bool:
    return isinstance(b, dict) and b.get("type") in ("image", "document")


def describe(b: dict) -> str:
    src = b.get("source") or {}
    size = len(src.get("data", "")) * 3 // 4
    return f"[{src.get('media_type', b.get('type'))}, {max(1, size // 1024)} KB]"


def summary(content) -> str:
    """Text of a result or message content, with media replaced by a short description
    (for audit entries and providers that cannot see images). Never includes base64 data."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if b.get("type") == "text"
                         else describe(b) if is_media(b)
                         else summary(b.get("content", "")) if b.get("type") == "tool_result"
                         else "" for b in content if isinstance(b, dict))
    return str(content)


def thin(history: list[dict], keep: int = KEEP_MEDIA) -> list[dict]:
    """Copy of `history` with all but the newest `keep` images/PDFs replaced by DROPPED_NOTE,
    including those inside tool results. Messages without media are returned as they are."""
    seen = 0

    def walk(blocks: list) -> list:
        nonlocal seen
        out = []
        for b in reversed(blocks):  # newest first
            if is_media(b):
                seen += 1
                if seen > keep:
                    b = {"type": "text", "text": DROPPED_NOTE}
            elif isinstance(b, dict) and isinstance(b.get("content"), list):
                b = {**b, "content": walk(b["content"])}
            out.append(b)
        return out[::-1]

    out = []
    for m in reversed(history):
        c = m.get("content")
        if isinstance(c, list):
            new = walk(c)
            if new != c:
                m = {**m, "content": new}
        out.append(m)
    return out[::-1]
