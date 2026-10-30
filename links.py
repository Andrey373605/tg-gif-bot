"""Download the picture or video behind a link (Pinterest, TikTok, X, Reddit, direct file links...).

First yt-dlp is tried: it knows hundreds of sites. If it can't find a video
(e.g. a Pinterest pin with just a picture, or a plain image link), the page is
fetched directly and the file or its og:image / og:video preview is taken.
"""
import ipaddress
import re
import socket
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yt_dlp

MAX_BYTES = 50 * 1024 * 1024
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
}
META_RE = re.compile(r"<meta\s[^>]*>", re.I)
ATTR_RE = re.compile(r'(\w[\w:-]*)\s*=\s*"([^"]*)"|(\w[\w:-]*)\s*=\s*\'([^\']*)\'')


class LinkError(Exception):
    """Message for the user about why the link didn't work."""


def check_public(url):
    """Refuse non-http links and links to the server's own network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LinkError("Нужна обычная ссылка, начинающаяся с http:// или https://")
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        raise LinkError("Не получилось открыть этот сайт.")
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if not ip.is_global:
            raise LinkError("Эта ссылка ведёт во внутреннюю сеть, такие не открываю.")


class QuietLogger:
    """yt-dlp prints "Unsupported URL" for every non-video page; that's expected here."""

    def debug(self, msg):
        pass

    info = warning = error = debug


def via_ytdlp(url, out_dir):
    opts = {
        "outtmpl": str(out_dir / "link.%(ext)s"),
        # Video-only stream is enough for a GIF, so no ffmpeg merge is needed
        "format": "bv*[height<=720]/b[height<=720]/bv*/b",
        "max_filesize": MAX_BYTES,
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [None])[0] or {}
    files = []
    for d in info.get("requested_downloads") or []:
        path = Path(d.get("filepath") or "")
        if not path.is_file():
            continue
        if d.get("vcodec") == "none" and d.get("acodec") not in (None, "none"):
            path.unlink()  # audio only (e.g. a podcast on the page), nothing to make a GIF from
            continue
        files.append(path)
    return files[0] if files else None


def meta_tags(html):
    tags = {}
    for tag in META_RE.findall(html):
        attrs = {}
        for m in ATTR_RE.finditer(tag):
            key = (m.group(1) or m.group(3)).lower()
            attrs[key] = unescape(m.group(2) if m.group(1) else m.group(4))
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name and "content" in attrs:
            tags.setdefault(name, attrs["content"])
    return tags


def fetch(client, url, out_dir, depth=0):
    check_public(url)
    with client.stream("GET", url) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype in EXT_BY_TYPE:
            dst = out_dir / ("link" + EXT_BY_TYPE[ctype])
            size = 0
            with dst.open("wb") as f:
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise LinkError("Файл по ссылке больше 50 МБ.")
                    f.write(chunk)
            return dst
        if ctype != "text/html" or depth > 0:
            raise LinkError("По ссылке нет ни картинки, ни видео.")
        r.read()
        html = r.text[:2_000_000]
        page_url = str(r.url)

    tags = meta_tags(html)
    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream", "og:image", "twitter:image"):
        media = tags.get(key)
        if media:
            try:
                return fetch(client, urljoin(page_url, media), out_dir, depth + 1)
            except (LinkError, httpx.HTTPError):
                continue
    raise LinkError("Не нашёл на этой странице ни картинки, ни видео.")


def download(url, out_dir):
    """Save the media behind `url` into `out_dir` and return its path."""
    check_public(url)
    try:
        path = via_ytdlp(url, out_dir)
        if path:
            return path
    except yt_dlp.utils.DownloadError:
        pass  # not a video yt-dlp understands, try the page itself
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
        try:
            return fetch(client, url, out_dir)
        except httpx.HTTPError:
            raise LinkError("Не получилось скачать по этой ссылке.")
