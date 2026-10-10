from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("uz_navo.providers")

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}


class ProviderError(RuntimeError):
    """Base media provider error."""


class ProviderAuthError(ProviderError):
    """Provider asked for authentication / bot verification."""


class ProviderUnavailableError(ProviderError):
    """Media/provider is unavailable."""


class ProviderTemporaryError(ProviderError):
    """Network or transient provider failure."""


def _error_from(exc: Exception, provider: str) -> ProviderError:
    text = str(exc)
    low = text.lower()
    auth_markers = (
        "sign in to confirm", "you're not a bot", "you’re not a bot", "cookies",
        "login required", "authentication required", "confirm your age", "po token",
    )
    unavailable_markers = (
        "video unavailable", "private video", "this video is private", "not available",
        "removed by the uploader", "members-only", "copyright",
    )
    temporary_markers = (
        "timed out", "timeout", "connection reset", "connection aborted", "temporary failure",
        "429", "too many requests", "503", "502", "network is unreachable",
    )
    prefix = f"{provider}: "
    if any(x in low for x in auth_markers):
        return ProviderAuthError(prefix + text)
    if any(x in low for x in unavailable_markers):
        return ProviderUnavailableError(prefix + text)
    if any(x in low for x in temporary_markers):
        return ProviderTemporaryError(prefix + text)
    return ProviderError(prefix + text)


def public_error_message(exc: Exception, action: str = "media") -> str:
    if isinstance(exc, ProviderAuthError):
        return "⚠️ Bu media hozirgi provider orqali olinmadi. Boshqa provider ham sinab ko‘rildi. Keyinroq qayta urinib ko‘ring."
    if isinstance(exc, ProviderUnavailableError):
        return "❌ Bu media mavjud emas, yopiq yoki yuklab olish uchun ochiq emas."
    if isinstance(exc, ProviderTemporaryError):
        return "⚠️ Media serverida vaqtinchalik muammo. Birozdan keyin qayta urinib ko‘ring."
    labels = {
        "audio": "audioni",
        "video": "videoni",
        "effect": "effektni",
        "recognize": "musiqani",
        "search": "qidiruvni",
        "info": "media ma’lumotini",
    }
    return f"❌ {labels.get(action, 'mediani')} hozir tayyorlab bo‘lmadi. Keyinroq qayta urinib ko‘ring."


def host_kind(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host in YOUTUBE_HOSTS:
        return "youtube"
    if host in INSTAGRAM_HOSTS:
        return "instagram"
    return "other"


def normalize_youtube_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
    except Exception:
        pass
    return url


def media_candidates(workdir: Path) -> list[Path]:
    ignored = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp"}
    return [p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() not in ignored]


class BaseProvider:
    name = "base"
    supports_youtube = False
    supports_instagram = False

    def supports(self, url: str) -> bool:
        kind = host_kind(url)
        return (kind == "youtube" and self.supports_youtube) or (kind == "instagram" and self.supports_instagram)

    def get_info(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def download_audio(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        raise NotImplementedError

    def download_video(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        raise NotImplementedError


class PytubeFixProvider(BaseProvider):
    name = "pytubefix"
    supports_youtube = True

    def __init__(self, video_height: int = 480, max_duration: int = 1200, proxy: str | None = None):
        self.video_height = video_height
        self.max_duration = max_duration
        self.proxy = proxy or None

    def _youtube(self, url: str):
        from pytubefix import YouTube

        kwargs: dict[str, Any] = {
            "use_oauth": False,
            "allow_oauth_cache": False,
        }
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return YouTube(normalize_youtube_url(url), **kwargs)

    @staticmethod
    def _info(yt) -> dict[str, Any]:
        return {
            "id": getattr(yt, "video_id", None),
            "title": getattr(yt, "title", None) or "Media",
            "uploader": getattr(yt, "author", None) or "",
            "channel": getattr(yt, "author", None) or "",
            "duration": getattr(yt, "length", None),
            "thumbnail": getattr(yt, "thumbnail_url", None),
            "webpage_url": getattr(yt, "watch_url", None),
        }

    def _check_duration(self, info: dict[str, Any]) -> None:
        duration = info.get("duration")
        if duration and int(duration) > self.max_duration:
            raise ProviderUnavailableError(
                f"Media juda uzun ({int(duration) // 60} daqiqa). Limit: {self.max_duration // 60} daqiqa."
            )

    def get_info(self, url: str) -> dict[str, Any]:
        try:
            yt = self._youtube(url)
            info = self._info(yt)
            self._check_duration(info)
            return info
        except ProviderError:
            raise
        except Exception as exc:
            raise _error_from(exc, self.name) from exc

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            from pytubefix import Search

            search = Search(query)
            results: list[dict[str, Any]] = []
            for yt in list(search.results)[:limit]:
                try:
                    video_id = getattr(yt, "video_id", None)
                    if not video_id:
                        continue
                    results.append({
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "id": video_id,
                        "title": getattr(yt, "title", None) or "Noma'lum",
                        "uploader": getattr(yt, "author", None) or "",
                        "duration": getattr(yt, "length", None),
                        "thumbnail": getattr(yt, "thumbnail_url", None)
                        or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    })
                except Exception:
                    continue
            return results
        except Exception as exc:
            raise _error_from(exc, self.name) from exc

    def download_audio(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        try:
            yt = self._youtube(url)
            info = self._info(yt)
            self._check_duration(info)
            stream = (
                yt.streams.filter(only_audio=True, file_extension="mp4").order_by("abr").desc().first()
                or yt.streams.filter(only_audio=True).order_by("abr").desc().first()
            )
            if stream is None:
                raise ProviderUnavailableError("Audio stream topilmadi.")
            ext = getattr(stream, "subtype", None) or "m4a"
            if ext == "mp4":
                ext = "m4a"
            path = stream.download(
                output_path=str(workdir),
                filename=f"audio.{ext}",
                timeout=30,
                max_retries=2,
            )
            if not path:
                raise ProviderError("Audio fayl yaratilmagan.")
            return Path(path), info
        except ProviderError:
            raise
        except Exception as exc:
            raise _error_from(exc, self.name) from exc

    def download_video(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        try:
            yt = self._youtube(url)
            info = self._info(yt)
            self._check_duration(info)
            streams = list(yt.streams.filter(progressive=True, file_extension="mp4"))

            def height(stream) -> int:
                res = str(getattr(stream, "resolution", "") or "")
                m = re.search(r"(\d+)", res)
                return int(m.group(1)) if m else 0

            suitable = [s for s in streams if 0 < height(s) <= self.video_height]
            if suitable:
                stream = max(suitable, key=height)
            elif streams:
                stream = min(streams, key=lambda s: height(s) or 99999)
            else:
                raise ProviderUnavailableError("Progressive MP4 video stream topilmadi.")

            path = stream.download(
                output_path=str(workdir),
                filename="video.mp4",
                timeout=30,
                max_retries=2,
            )
            if not path:
                raise ProviderError("Video fayl yaratilmagan.")
            return Path(path), info
        except ProviderError:
            raise
        except Exception as exc:
            raise _error_from(exc, self.name) from exc


class YtDlpProvider(BaseProvider):
    name = "yt-dlp"
    supports_youtube = True
    supports_instagram = True

    def __init__(self, video_height: int = 480, max_duration: int = 1200, proxy: str | None = None):
        self.video_height = video_height
        self.max_duration = max_duration
        self.proxy = proxy or None

    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        return opts

    def _check_duration(self, info: dict[str, Any]) -> None:
        duration = info.get("duration")
        if duration and int(duration) > self.max_duration:
            raise ProviderUnavailableError(
                f"Media juda uzun ({int(duration) // 60} daqiqa). Limit: {self.max_duration // 60} daqiqa."
            )

    def get_info(self, url: str) -> dict[str, Any]:
        try:
            from yt_dlp import YoutubeDL
            opts = self._base_opts() | {"skip_download": True}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalize_youtube_url(url), download=False)
            if not isinstance(info, dict):
                raise ProviderError("Media ma'lumoti olinmadi.")
            self._check_duration(info)
            return info
        except ProviderError:
            raise
        except Exception as exc:
            raise _error_from(exc, self.name) from exc

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            from yt_dlp import YoutubeDL
            opts = self._base_opts() | {"skip_download": True, "extract_flat": True}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            entries = (info or {}).get("entries") or []
            results: list[dict[str, Any]] = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                video_id = item.get("id")
                if not video_id:
                    continue
                results.append({
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "id": video_id,
                    "title": item.get("title") or "Noma'lum",
                    "uploader": item.get("uploader") or item.get("channel") or "",
                    "duration": item.get("duration"),
                    "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                })
            return results
        except Exception as exc:
            raise _error_from(exc, self.name) from exc

    def download_video(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        try:
            from yt_dlp import YoutubeDL
            outtmpl = str(workdir / "video.%(ext)s")
            preferred_format = (
                f"b[height<={self.video_height}][ext=mp4]/"
                f"b[height<={self.video_height}]/"
                f"bv*[height<={self.video_height}][ext=mp4]+ba[ext=m4a]/"
                f"bv*[height<={self.video_height}]+ba/b/bv*+ba"
            )
            opts = self._base_opts() | {
                "outtmpl": outtmpl,
                "restrictfilenames": True,
                "overwrites": True,
                "concurrent_fragment_downloads": 4,
                "merge_output_format": "mp4",
                "format": preferred_format,
                "no_warnings": False,
            }
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalize_youtube_url(url), download=True)
            if not isinstance(info, dict):
                raise ProviderError("Video ma'lumoti olinmadi.")
            self._check_duration(info)
            files = media_candidates(workdir)
            if not files:
                raise ProviderError("Yuklangan video topilmadi.")
            return max(files, key=lambda p: p.stat().st_size), info
        except ProviderError:
            raise
        except Exception as exc:
            raise _error_from(exc, self.name) from exc

    def download_audio(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        try:
            from yt_dlp import YoutubeDL
            opts = self._base_opts() | {
                "outtmpl": str(workdir / "audio.%(ext)s"),
                "restrictfilenames": True,
                "overwrites": True,
                "concurrent_fragment_downloads": 4,
                "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
                "no_warnings": False,
            }
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalize_youtube_url(url), download=True)
            if not isinstance(info, dict):
                raise ProviderError("Audio ma'lumoti olinmadi.")
            self._check_duration(info)
            files = media_candidates(workdir)
            if not files:
                raise ProviderError("Yuklangan audio topilmadi.")
            return max(files, key=lambda p: p.stat().st_size), info
        except ProviderError:
            raise
        except Exception as exc:
            raise _error_from(exc, self.name) from exc


@dataclass
class CircuitState:
    failures: int = 0
    open_until: float = 0.0


class ProviderManager:
    def __init__(self, providers: list[BaseProvider], failure_threshold: int = 3, cooldown_seconds: int = 120):
        self.providers = providers
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(10, cooldown_seconds)
        self.states: dict[str, CircuitState] = {p.name: CircuitState() for p in providers}

    def _is_open(self, provider: BaseProvider) -> bool:
        state = self.states[provider.name]
        return state.open_until > time.monotonic()

    def _success(self, provider: BaseProvider) -> None:
        self.states[provider.name] = CircuitState()

    def _failure(self, provider: BaseProvider) -> None:
        state = self.states[provider.name]
        state.failures += 1
        if state.failures >= self.failure_threshold:
            state.open_until = time.monotonic() + self.cooldown_seconds
            logger.warning("Circuit opened for %s for %ss", provider.name, self.cooldown_seconds)

    def _call(self, capability: str, *args):
        url = args[0] if args and isinstance(args[0], str) and args[0].startswith("http") else None
        candidates = [p for p in self.providers if (not url or p.supports(url)) and hasattr(p, capability)]
        errors: list[Exception] = []

        # Prefer healthy providers, but if all circuits are open, allow one probe.
        healthy = [p for p in candidates if not self._is_open(p)]
        if healthy:
            candidates = healthy
        elif candidates:
            candidates = [min(candidates, key=lambda p: self.states[p.name].open_until)]

        for provider in candidates:
            method = getattr(provider, capability)
            try:
                result = method(*args)
                self._success(provider)
                logger.info("Provider success: %s.%s", provider.name, capability)
                return result
            except Exception as exc:
                err = exc if isinstance(exc, ProviderError) else _error_from(exc, provider.name)
                errors.append(err)
                self._failure(provider)
                logger.warning("Provider failed: %s.%s: %s", provider.name, capability, type(err).__name__)

        if errors:
            # Prefer a meaningful typed error from the last provider.
            raise errors[-1]
        raise ProviderUnavailableError(f"{capability} uchun mos provider topilmadi.")

    def get_info(self, url: str) -> dict[str, Any]:
        return self._call("get_info", url)

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        # Search is YouTube-only and does not take a URL.
        errors: list[Exception] = []
        candidates = [p for p in self.providers if p.supports_youtube and not self._is_open(p)]
        if not candidates:
            candidates = [p for p in self.providers if p.supports_youtube][:1]
        for provider in candidates:
            try:
                result = provider.search(query, limit)
                if result:
                    self._success(provider)
                    return result
                raise ProviderUnavailableError(f"{provider.name}: qidiruv natijasi bo‘sh.")
            except Exception as exc:
                err = exc if isinstance(exc, ProviderError) else _error_from(exc, provider.name)
                errors.append(err)
                self._failure(provider)
                logger.warning("Search provider failed: %s: %s", provider.name, type(err).__name__)
        if errors:
            raise errors[-1]
        return []

    def download_audio(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        return self._call("download_audio", url, workdir)

    def download_video(self, url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
        return self._call("download_video", url, workdir)


def build_provider_manager(video_height: int, max_duration: int) -> ProviderManager:
    order = [x.strip().lower() for x in os.getenv("YOUTUBE_PROVIDERS", "pytubefix,yt-dlp").split(",") if x.strip()]
    proxy = os.getenv("OUTBOUND_PROXY", "").strip() or None
    providers: list[BaseProvider] = []
    for name in order:
        if name in {"pytubefix", "pytube", "ptf"} and not any(isinstance(p, PytubeFixProvider) for p in providers):
            providers.append(PytubeFixProvider(video_height, max_duration, proxy))
        elif name in {"yt-dlp", "ytdlp", "yt_dlp"} and not any(isinstance(p, YtDlpProvider) for p in providers):
            providers.append(YtDlpProvider(video_height, max_duration, proxy))

    # Instagram currently uses yt-dlp as a separate provider path, even if omitted for YouTube.
    if not any(isinstance(p, YtDlpProvider) for p in providers):
        providers.append(YtDlpProvider(video_height, max_duration, proxy))

    return ProviderManager(
        providers,
        failure_threshold=int(os.getenv("PROVIDER_FAILURE_THRESHOLD", "3")),
        cooldown_seconds=int(os.getenv("PROVIDER_COOLDOWN_SECONDS", "120")),
    )
