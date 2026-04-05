import asyncio
from datetime import datetime
import random
import string
from urllib.parse import quote, quote_plus, urljoin, urlparse
import uuid
from typing import Iterable, List, Union, Optional
import re

import httpx
from loguru import logger
from curl_cffi.requests import AsyncSession, Response, RequestsError
from pydantic import BaseModel, ValidationError

from embykeeper import __version__
from embykeeper.utils import get_proxy_str, show_exception, truncate_str
from embykeeper.cache import cache
from embykeeper.schema import EmbyAccount
from embykeeper.config import config

logger = logger.bind(scheme="embywatcher")


class EmbyError(Exception):
    pass


class EmbyRequestError(EmbyError):
    pass


class EmbyConnectError(EmbyError):
    pass


class EmbyLoginError(EmbyRequestError):
    pass


class EmbyStatusError(EmbyRequestError):
    pass


class EmbyPlayError(EmbyError):
    pass


class EmbyEnv(BaseModel):
    client: str
    device: str
    device_id: str
    client_version: str
    useragent: str


class Emby:
    playing_count = 0

    def __init__(self, account: EmbyAccount):
        self.a = account

        self._env = None
        self._token = None
        self._user_id = None
        self._base_path = None
        self._stream_max_bytes_per_request = 8 * 1024 * 1024
        self._stream_max_request_seconds = 15

        self.run_id = str(uuid.uuid4()).upper()
        self.cf_clearance = None
        self.useragent = None
        self.items = {}

        self.log = logger.bind(server=self.a.name or self.server_label, username=self.a.username)

    @property
    def proxy(self):
        return config.proxy if self.a.use_proxy else None

    @property
    def hostname(self):
        return self.a.url.host

    @property
    def server_label(self):
        path = (self.a.url.path or "").rstrip("/")
        return f"{self.hostname}{path}" if path else self.hostname

    @property
    def use_special_stream_workaround(self):
        return bool(self.a.stream_workaround)

    @property
    def use_external_stream_workaround(self):
        return bool(self.a.external_stream_workaround)

    @property
    def use_stream_cdn_path(self):
        return bool(self.a.stream_cdn_path)

    @staticmethod
    def _describe_response(resp: Response, limit: int = 200) -> str:
        try:
            body = (resp.text or "").strip()
        except Exception:
            body = ""

        if body:
            body = re.sub(r"\s+", " ", body)
            if len(body) > limit:
                body = f"{body[:limit]}..."
            return f"响应内容: {body}"
        return "响应内容为空"

    @property
    def token(self):
        if not self._token:
            self._load_credentials()
        return self._token

    @property
    def env(self):
        if not self._env:
            self._load_env()
        if not self._env:
            self._env = self.get_fake_env()
        return self._env

    @property
    def user_id(self):
        if not self._user_id:
            self._load_credentials()
        return self._user_id

    def _load_credentials(self):
        data: dict = cache.get(f"emby.credential.{self.hostname}.{self.a.username}", {})
        self._token = data.get("token", None)
        self._user_id = data.get("userid", None)

    def _load_env(self):
        cache_key = f"emby.env.{self.hostname}.{self.a.username}"
        data: dict = cache.get(cache_key, {})
        if data:
            # 检查用户配置是否与缓存一致
            should_clear = False
            for key, user_value in {
                "client_version": self.a.client_version,
                "client": self.a.client,
                "device": self.a.device,
                "device_id": self.a.device_id,
                "useragent": self.a.useragent,
            }.items():
                if user_value and data.get(key) != user_value:
                    should_clear = True
                    break

            if should_clear:
                logger.info("账户设置已修改, 将重新生成环境 (Headers).")
                self._env = None
                cache.delete(cache_key)
            else:
                try:
                    self._env = EmbyEnv.model_validate(data)
                except ValidationError:
                    logger.warning("缓存加载失败, 将重新生成环境 (Headers).")
                    self._env = None

    @staticmethod
    def get_random_device():
        from faker import Faker

        device_type = random.choice(("iPhone", "iPad"))

        # All patterns with their weights
        patterns = [
            ("chinese_normal", 20),
            ("chinese_lastname_pinyin", 40),
            ("chinese_firstname_pinyin", 10),
            ("english_normal", 20),
            ("english_upper", 10),
            ("english_name_only", 10),
        ]

        pattern = random.choices([p[0] for p in patterns], weights=[p[1] for p in patterns])[0]

        if pattern.startswith("chinese"):
            fake = Faker("zh_CN")
            surname = fake.last_name()
            given_name = fake.first_name_male() if random.random() < 0.5 else fake.first_name_female()

            if pattern == "chinese_normal":
                return f"{surname}{given_name}的{device_type}"
            else:
                from xpinyin import Pinyin

                p = Pinyin()
                if pattern == "chinese_lastname_pinyin":
                    pinyin = p.get_pinyin(surname).capitalize()
                    return f"{pinyin}的{device_type}"
                else:  # chinese_firstname_pinyin
                    pinyin = "".join([word[0].upper() for word in p.get_pinyin(given_name).split("-")])
                    return f"{pinyin}的{device_type}"
        else:
            fake = Faker("en_US")
            name = fake.first_name()

            if pattern == "english_normal":
                return f"{name}'s {device_type}"
            elif pattern == "english_upper":
                return f"{name.upper()}{device_type.upper()}"
            else:  # english_name_only
                return name

    @staticmethod
    def get_device_uuid():
        rd = random.Random()
        rd.seed(uuid.getnode())
        return uuid.UUID(int=rd.getrandbits(128))

    def get_fake_env(self):
        cached_env: dict = cache.get(f"emby.env.{self.hostname}.{self.a.username}", {})

        # 按优先级获取各个值
        is_filebar = random.random() < 0.2
        version = (
            self.a.client_version
            or cached_env.get("client_version")
            or f"1.3.{random.randint(34, 34) if is_filebar else random.randint(16, 30)}"
        )
        client = self.a.client or cached_env.get("client") or ("Filebar" if is_filebar else "Fileball")
        device = self.a.device or cached_env.get("device") or self.get_random_device()
        device_id = self.a.device_id or cached_env.get("device_id") or str(uuid.uuid4()).upper()
        useragent = self.useragent or self.a.useragent or cached_env.get("ua") or f"{client}/{version}"

        data = {
            "client": client,
            "device": device,
            "device_id": device_id,
            "client_version": version,
            "useragent": useragent,
        }

        env = EmbyEnv(**data)
        cache.set(f"emby.env.{self.hostname}.{self.a.username}", data)
        return env

    def build_headers(self):
        headers = {}
        auth_headers = {
            "Client": self.env.client,
            "Device": self.env.device,
            "DeviceId": self.env.device_id,
            "Version": self.env.client_version,
        }
        auth_header = ",".join([f"{k}={quote(str(v))}" for k, v in auth_headers.items()])
        auth_user_id = self.user_id or self.run_id
        full_auth_header = f'MediaBrowser Token={self.token or ""},Emby UserId={auth_user_id},{auth_header}'
        headers["User-Agent"] = self.useragent or self.env.useragent
        headers["Accept-Language"] = "zh-CN,zh-Hans;q=0.9"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "*/*"
        headers["X-Emby-Authorization"] = full_auth_header
        if self.token:
            headers["X-Emby-Token"] = self.token
        return headers

    def _get_session(self) -> AsyncSession:
        cookies = {}
        if self.cf_clearance:
            cookies["cf_clearance"] = self.cf_clearance

        timeout = self.a.timeout or config.emby.timeout or 10

        return AsyncSession(
            verify=False,
            headers=self.build_headers(),
            cookies=cookies,
            proxy=get_proxy_str(self.proxy, curl=True),
            timeout=float(timeout),
            impersonate="chrome",
            allow_redirects=True,
            default_headers=False,
        )

    def _get_stream_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=20.0, read=None, write=20.0, pool=20.0)

    def _build_stream_headers(self, length: int) -> dict:
        return {
            "Range": f"bytes={length}-",
            "User-Agent": self.useragent or self.env.useragent,
            "Icy-MetaData": "1",
        }

    def _build_bounded_stream_headers(self, length: int, size: int) -> dict:
        end = length + size - 1
        return {
            "Range": f"bytes={length}-{end}",
            "User-Agent": self.useragent or self.env.useragent,
            "Icy-MetaData": "1",
        }

    def _build_external_stream_headers(self, length: int) -> dict:
        return {
            "User-Agent": self.useragent or self.env.useragent,
            "Accept": "*/*",
            "Range": f"bytes={length}-",
            "Connection": "close",
            "Icy-MetaData": "1",
        }

    def _build_cdn_stream_url(self, media_source_path: str) -> str:
        parsed = urlparse(media_source_path)
        if not parsed.scheme or not parsed.netloc:
            raise EmbyStatusError(f"访问失败: 无法解析媒体源 CDN 路径 (PATH = {media_source_path})")

        if self.hostname.startswith("emby."):
            cdn_host = self.hostname.replace("emby.", "emby-cdn1.", 1)
        else:
            cdn_host = f"emby-cdn1.{self.hostname}"

        return f"https://{cdn_host}/cdn?path={quote_plus(media_source_path)}&api_key={self.token}"

    def _build_cdn_stream_headers(self, length: int, size: int) -> dict:
        end = length + size - 1
        return {
            "Authorization": (
                f'MediaBrowser Client="Hills Windows", Device="DESKTOP-MKFV627", '
                f'DeviceId="3f3a1fd2c899d30f888ef5f79103e432", Version="1.0.0", Token="{self.token}"'
            ),
            "User-Agent": "Hills Windows/1.0.0 (windows; 26100.ge_release.240331-1435)",
            "Accept": "application/json, text/plain, */*",
            "Range": f"bytes={length}-{end}",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,en,*",
            "Icy-MetaData": "1",
        }

    @staticmethod
    def _is_absolute_url(value: Optional[str]) -> bool:
        if not value:
            return False
        parsed = urlparse(value)
        return bool(parsed.scheme and parsed.netloc)

    def _update_media_source_info(
        self,
        media_source_id: str,
        direct_stream_url: Optional[str],
        media_source_path: Optional[str],
        playback_info: dict,
    ):
        media_sources = playback_info.get("MediaSources") or []
        if not media_sources:
            return media_source_id, direct_stream_url, media_source_path

        source = media_sources[0]
        next_media_source_id = source.get("Id") or media_source_id
        next_direct_stream_url = source.get("DirectStreamUrl") or direct_stream_url
        next_media_source_path = source.get("Path")

        if self._is_absolute_url(next_media_source_path):
            media_source_path = next_media_source_path
        elif not media_source_path and next_media_source_path:
            media_source_path = next_media_source_path

        return next_media_source_id, next_direct_stream_url, media_source_path

    async def _probe_base_path(self, prefix: str) -> bool:
        url = f"{self.a.url.scheme}://{self.a.url.host}:{self.a.url.port}{prefix}/System/Info/Public"
        try:
            async with self._get_session() as session:
                resp: Response = await session.request("GET", url)
                await resp.aclose()
                return resp.ok
        except RequestsError:
            return False

    async def _get_base_path(self) -> str:
        if self._base_path is not None:
            return self._base_path

        configured_base_path = (self.a.url.path or "").rstrip("/")
        if configured_base_path:
            self._base_path = configured_base_path
            return self._base_path

        if not self.use_special_stream_workaround:
            self._base_path = ""
            return self._base_path

        cache_key = f"emby.basepath.{self.hostname}"
        cached_base_path = cache.get(cache_key, None)
        if cached_base_path not in (None, ""):
            self._base_path = cached_base_path
            return self._base_path

        for prefix in ("/emby", "/mediabrowser", ""):
            if await self._probe_base_path(prefix):
                self._base_path = prefix
                cache.set(cache_key, prefix)
                return self._base_path

        self._base_path = ""
        cache.set(cache_key, self._base_path)
        return self._base_path

    async def _request(self, method: str, path: str, _login=False, **kw) -> Response:

        url = await self._build_url(path)
        last_err = None
        for _ in range(3):
            try:
                async with self._get_session() as session:
                    resp: Response = await session.request(method, url, **kw)
                    if resp.status_code == 401 and self.a.username and not _login:
                        if not await self.login():
                            raise EmbyLoginError("无法登陆到服务器")
                        continue
                    elif resp.status_code in (502, 503, 504):
                        await asyncio.sleep(random.random() * 2 + 0.5)
                        continue
                    elif resp.status_code == 403 and (
                        "cf-wrapper" in resp.text or "Just a moment" in resp.text
                    ):
                        if self.cf_clearance:
                            raise EmbyStatusError("访问失败: Cloudflare 验证码解析后依然有验证")
                        await self.use_cfsolver()
                        continue
                    elif not resp.ok and not _login:
                        raise EmbyStatusError(
                            f"访问失败: 异常 HTTP 代码 {resp.status_code} (URL = {url}, "
                            f"{self._describe_response(resp)})"
                        )
                    else:
                        return resp
            except RequestsError as e:
                last_err = e
                await asyncio.sleep(random.random() + 0.5)

        if last_err:
            error_msg = re.sub(r"\s+See\s+.*?\s+first for more details\.\.?", "", str(last_err))
            raise EmbyConnectError(f"{last_err.__class__.__name__}: {error_msg}")
        else:
            raise EmbyConnectError(f'连接到 "{url}" 重试超限')

    async def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path

        base_path = await self._get_base_path()
        base_url = f"{self.a.url.scheme}://{self.a.url.host}:{self.a.url.port}{base_path}"
        return f"{base_url}/{path.lstrip('/')}"

    async def use_cfsolver(self):
        from embykeeper.cloudflare import get_cf_clearance

        if not self.a.cf_challenge:
            if self.proxy:
                self.log.warning(
                    f"该站点已启用 Cloudflare 保护, 请尝试浏览器以同样的代理访问: {self.a.url}"
                    "以解除 Cloudflare IP 限制, 然后再次运行.\n"
                    '或者, 高级用户可以使用 "cf_challenge = true" 配置项以允许尝试解析验证码.'
                )
            else:
                self.log.warning(
                    f'该站点已启用 Cloudflare 保护, 请使用 "cf_challenge = true" 配置项以允许尝试解析验证码.'
                )
        self.log.info(f"该站点已启用 Cloudflare 保护, 即将请求解析.")
        if self.proxy:
            if self.proxy.scheme != "socks5":
                self.log.warning(
                    f"该站点验证解析仅支持 SOCKS5 代理, 由于当前代理协议不支持, 将尝试不使用代理."
                )
                self.a.use_proxy = False
            else:
                self.log.info(
                    f"验证码解析将使用代理, 可能导致解析失败, 若失败请使用"
                    '"use_proxy = false" 以禁用该站点的代理.'
                )
        try:
            cf_clearance, useragent = await get_cf_clearance(self.a.url, self.proxy)
            if not cf_clearance:
                self.log.warning(f"Cloudflare 验证码解析失败.")
                return False
            else:
                self.cf_clearance = cf_clearance
                self.useragent = useragent
                return True
        except Exception as e:
            self.log.warning(f"Cloudflare 验证码解析时出现错误.")
            show_exception(e, regular=False)
            return False

    async def login(self) -> dict:
        """Login to Emby server and get authentication token."""

        if self.a.username is None or self.a.password is None:
            self.log.warning("没有提供用户名或密码, 无法登陆, 执行失败.")
            return None

        data = {
            "Username": self.a.username,
            "Pw": self.a.password,
        }

        resp = await self._request(
            "POST",
            "/Users/AuthenticateByName",
            json=data,
            _login=True,
        )

        if resp.status_code == 401:
            self.log.warning(f"用户名或密码错误, 执行失败.")
            return None

        if resp.status_code != 200:
            self.log.warning(f"登陆时出现错误 ({resp.status_code}), 执行失败.")
            return None

        user: dict = resp.json()
        self._token = user.get("AccessToken", None)
        self._user_id = user.get("User", {}).get("Id")
        if self.token and self.user_id:
            cache_data = {
                "token": self.token,
                "userid": self.user_id,
            }
            cache.set(f"emby.credential.{self.hostname}.{self.a.username}", cache_data)
            return self.token

    async def play(self, item: Union[dict, int], time: float = 10):
        if isinstance(item, dict):
            try:
                iid = item["Id"]
                iname = item["Name"]
            except KeyError:
                raise EmbyPlayError("无法解析视频信息")
        else:
            iid = item
            iname = "(请求播放的视频)"

        playback_info_data = {
            "DeviceProfile": {
                "CodecProfiles": [],
                "SubtitleProfiles": [
                    {"Method": "External", "Format": "vtt"},
                    {"Method": "External", "Format": "ass"},
                    {"Method": "External", "Format": "ssa"},
                    {"Method": "External", "Format": "srt"},
                    {"Method": "External", "Format": "sub"},
                    {"Method": "External", "Format": "subrip"},
                    {"Method": "External", "Format": "smi"},
                    {"Method": "External", "Format": "ttml"},
                    {"Method": "External", "Format": "webvtt"},
                    {"Method": "External", "Format": "dvdsub"},
                    {"Method": "External", "Format": "sup"},
                    {"Method": "Embed", "Format": "dvdsub"},
                    {"Method": "Embed", "Format": "vobsub"},
                    {"Method": "Embed", "Format": "vtt"},
                    {"Method": "Embed", "Format": "ass"},
                    {"Method": "Embed", "Format": "ssa"},
                    {"Method": "Embed", "Format": "srt"},
                    {"Method": "Embed", "Format": "sub"},
                    {"Method": "Embed", "Format": "pgssub"},
                    {"Method": "Embed", "Format": "pgs"},
                    {"Method": "Embed", "Format": "subrip"},
                    {"Method": "Embed", "Format": "smi"},
                    {"Method": "Embed", "Format": "ttml"},
                    {"Method": "Embed", "Format": "webvtt"},
                    {"Method": "Embed", "Format": "mov_text"},
                    {"Method": "Embed", "Format": "dvb_teletext"},
                    {"Method": "Embed", "Format": "dvb_subtitle"},
                    {"Method": "Embed", "Format": "dvbsub"},
                    {"Method": "Embed", "Format": "idx"},
                    {"Method": "Embed", "Format": "sup"},
                    {"Method": "Hls", "Format": "vtt"},
                    {"Method": "Hls", "Format": "vtt"},
                ],
                "MaxStreamingBitrate": 200000000,
                "DirectPlayProfiles": [
                    {"Type": "Video"},
                    {"Type": "Audio"},
                ],
                "TranscodingProfiles": [
                    {
                        "AudioCodec": "aac,mp3,wav,ac3,eac3,flac,opus",
                        "VideoCodec": "hevc,h264,h265,mpeg4",
                        "BreakOnNonKeyFrames": True,
                        "Type": "Video",
                        "Protocol": "hls",
                        "MaxAudioChannels": "6",
                        "Container": "ts",
                        "Context": "Streaming",
                        "MinSegments": "1",
                        "ManifestSubtitles": "vtt",
                    }
                ],
                "ContainerProfiles": [],
                "MusicStreamingTranscodingBitrate": 200000000,
                "ResponseProfiles": [],
                "MaxStaticBitrate": 200000000,
            }
        }

        resp = await self._request(
            method="GET",
            path=f"/Videos/{iid}/AdditionalParts",
            params=dict(
                Fields="PrimaryImageAspectRatio,UserData,CanDelete",
                IncludeItemTypes="Playlist,BoxSet",
                Recursive=True,
                SortBy="SortName",
            ),
        )

        resp = await self._request(
            method="POST",
            path=f"/Items/{iid}/PlaybackInfo",
            params=dict(
                AutoOpenLiveStream=False,
                IsPlayback=False,
                MaxStreamingBitrate=40000000,
                StartTimeTicks=0,
                UserID=self.user_id,
            ),
            json=playback_info_data,
        )
        playback_info = resp.json()

        play_session_id = playback_info.get("PlaySessionId", "")
        media_source_id = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(32))
        direct_stream_url = None
        media_source_path = None
        media_source_id, direct_stream_url, media_source_path = self._update_media_source_info(
            media_source_id,
            direct_stream_url,
            media_source_path,
            playback_info,
        )

        await asyncio.sleep(random.uniform(1, 3))

        # 模拟播放
        for i in range(4):
            if i:
                IsPlayback = True
                AutoOpenLiveStream = True
            else:
                IsPlayback = False
                AutoOpenLiveStream = False

            resp = await self._request(
                method="POST",
                path=f"/Items/{iid}/PlaybackInfo",
                params=dict(
                    AudioStreamIndex=1,
                    AutoOpenLiveStream=AutoOpenLiveStream,
                    IsPlayback=IsPlayback,
                    MaxStreamingBitrate=42000000,
                    MediaSourceId=str(media_source_id),
                    StartTimeTicks=0,
                    UserID=self.user_id,
                ),
                json=playback_info_data,
            )
            media_source_id, direct_stream_url, media_source_path = self._update_media_source_info(
                media_source_id,
                direct_stream_url,
                media_source_path,
                resp.json(),
            )

        def get_playing_data(tick, update=False, stop=False):
            data = {
                "SubtitleOffset": 0,
                "MaxStreamingBitrate": 420000000,
                "MediaSourceId": str(media_source_id),
                "SubtitleStreamIndex": -1,
                "VolumeLevel": 100,
                "PlaybackRate": 1,
                "PlaybackStartTimeTicks": int(datetime.now().timestamp() // 10 * 10 * 10000000),
                "PositionTicks": tick,
                "PlaySessionId": play_session_id,
            }
            if update:
                data["EventName"] = "TimeUpdate"
            if stop:
                queue = []
            else:
                queue = [{"Id": str(iid), "PlaylistItemId": "playlistItem0"}]
            data.update(
                {
                    "PlaylistLength": 1,
                    "NowPlayingQueue": queue,
                    "IsMuted": False,
                    "PlaylistIndex": 0,
                    "ItemId": str(iid),
                    "RepeatMode": "RepeatNone",
                    "AudioStreamIndex": -1,
                    "PlayMethod": "DirectStream",
                    "CanSeek": True,
                    "IsPaused": False,
                }
            )
            return data

        async def stream():
            url = direct_stream_url or f"/Videos/{iid}/stream"
            length = 0
            consecutive_stream_errors = 0
            while True:
                if self.use_stream_cdn_path and media_source_path:
                    max_bytes_per_request = 16 * 1024 * 1024
                    max_request_seconds = min(self._stream_max_request_seconds, 10)
                    stream_url = self._build_cdn_stream_url(media_source_path)
                    stream_headers = self._build_cdn_stream_headers(length, max_bytes_per_request)
                else:
                    stream_url = await self._build_url(url)
                    stream_headers = self._build_stream_headers(length)
                    max_bytes_per_request = self._stream_max_bytes_per_request
                    max_request_seconds = self._stream_max_request_seconds
                response = None
                response_cm = None
                client = None
                try:
                    client = httpx.AsyncClient(
                        verify=False,
                        timeout=self._get_stream_timeout(),
                        follow_redirects=False,
                        http2=False,
                        headers=stream_headers,
                    )
                    response_cm = client.stream("GET", stream_url)
                    response = await response_cm.__aenter__()

                    if response.status_code in (301, 302, 307, 308):
                        redirect_url = response.headers.get("Location")
                        parsed_original = urlparse(stream_url)
                        await response_cm.__aexit__(None, None, None)
                        response_cm = None
                        await client.aclose()
                        client = None
                        if not redirect_url:
                            raise EmbyStatusError(f"访问失败: 流媒体重定向缺少目标地址 (URL = {stream_url})")
                        parsed_redirect = urlparse(redirect_url)
                        redirect_headers = stream_headers
                        if parsed_redirect.netloc and parsed_redirect.netloc != parsed_original.netloc:
                            if self.use_external_stream_workaround:
                                max_bytes_per_request = min(max_bytes_per_request, 256 * 1024)
                                max_request_seconds = min(max_request_seconds, 2)
                            elif self.use_special_stream_workaround:
                                max_bytes_per_request = min(max_bytes_per_request, 2 * 1024 * 1024)
                                max_request_seconds = min(max_request_seconds, 5)
                            redirect_headers = self._build_external_stream_headers(length)

                        client = httpx.AsyncClient(
                            verify=False,
                            timeout=self._get_stream_timeout(),
                            follow_redirects=False,
                            http2=False,
                            headers=redirect_headers,
                        )
                        response_cm = client.stream("GET", redirect_url)
                        response = await response_cm.__aenter__()

                    if response.status_code >= 400:
                        raise EmbyStatusError(
                            f"访问失败: 异常 HTTP 代码 {response.status_code} (URL = {response.request.url}, "
                            f"{self._describe_response(response)})"
                        )

                    request_started_at = datetime.now()
                    read_bytes = 0
                    async for i in response.aiter_bytes(chunk_size=1024):
                        chunk_length = len(i)
                        length += chunk_length
                        read_bytes += chunk_length
                        del i
                        await asyncio.sleep(random.random())
                        elapsed = (datetime.now() - request_started_at).total_seconds()
                        if (
                            read_bytes >= max_bytes_per_request
                            or elapsed >= max_request_seconds
                        ):
                            break
                        if random.random() < 0.01:
                            continue
                    consecutive_stream_errors = 0
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    consecutive_stream_errors += 1
                    if consecutive_stream_errors > 3:
                        raise
                    self.log.debug(f"流媒体文件访问错误, 正在重试 ({consecutive_stream_errors}/3).")
                    await asyncio.sleep(min(consecutive_stream_errors, 3))
                    continue
                finally:
                    if response_cm is not None:
                        await response_cm.__aexit__(None, None, None)
                    if client is not None:
                        await client.aclose()

        stream_task = asyncio.create_task(stream())
        rt = random.uniform(5, 10)
        self.log.info(f'开始模拟加载视频 "{truncate_str(iname, 10)}" ({rt:.0f} 秒).')
        await asyncio.sleep(rt)
        self.log.info(f'开始发送视频 "{truncate_str(iname, 10)}" 发送进度.')
        Emby.playing_count += 1
        stream_error = None
        try:
            await asyncio.sleep(random.uniform(1, 3))
            try:
                resp = await self._request(
                    method="POST",
                    path="/Sessions/Playing",
                    json=get_playing_data(0),
                )
            except EmbyRequestError as e:
                raise EmbyPlayError(f"无法开始播放: {e}")
            t = time

            last_report_t = t
            consecutive_progress_errors = 0
            last_progress_error = None
            progress_reporting_enabled = True
            report_interval = 5  # Start with 5 seconds
            report_count = 0
            max_interval = 300  # 5 minutes in seconds
            while t > 0:
                if last_report_t and last_report_t - t > report_interval:
                    self.log.info(f'正在播放: "{truncate_str(iname, 10)}" (还剩 {t:.0f} 秒).')
                    last_report_t = t
                    report_count += 1
                    # After 3 reports at current interval, double the interval
                    if report_count >= 3:
                        report_count = 0
                        report_interval = min(report_interval * 2, max_interval)
                st = min(10, t)
                await asyncio.sleep(st)
                t -= st
                if not progress_reporting_enabled:
                    continue
                tick = int((time - t) * 10000000)
                payload = get_playing_data(tick, update=True)
                try:
                    resp = await asyncio.wait_for(
                        self._request(
                            method="POST",
                            path="/Sessions/Playing/Progress",
                            json=payload,
                        ),
                        10,
                    )
                except Exception as e:
                    last_progress_error = str(e)
                    consecutive_progress_errors += 1
                    if consecutive_progress_errors in (1, 3, 6, 12):
                        self.log.warning(
                            "播放状态设定错误"
                            f" ({consecutive_progress_errors}/12): {last_progress_error}"
                        )
                    else:
                        self.log.debug(f"播放状态设定错误: {e}")
                    if consecutive_progress_errors > 3:
                        if self.a.progress_fallback:
                            progress_reporting_enabled = False
                            self.log.warning(
                                "播放进度上报连续失败, 将继续模拟播放并在结束时仅发送停止事件: "
                                f"{last_progress_error}"
                            )
                        else:
                            raise EmbyPlayError(f"播放进度上报连续失败: {last_progress_error}")
                else:
                    consecutive_progress_errors = 0
                    last_progress_error = None
            await asyncio.sleep(random.uniform(1, 3))
        finally:
            Emby.playing_count -= 1
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                stream_error = e
                self.log.warning(f"模拟播放时, 访问流媒体文件失败.")
                show_exception(e)

        try:
            final_percentage = random.uniform(0.95, 1.0)
            final_tick = int((time * final_percentage) // 10 * 10 * 10000000)
            await self._request(
                method="POST",
                path="/Sessions/Playing/Stopped",
                json=get_playing_data(final_tick, stop=True),
            )
            if stream_error:
                raise EmbyPlayError(f"模拟播放时, 访问流媒体文件失败: {stream_error}")
            self.log.info(f"播放完成, 共 {time:.0f} 秒.")
            return True
        except Exception as e:
            if isinstance(e, EmbyPlayError):
                raise
            raise EmbyPlayError(f"由于连接错误或服务器错误无法停止播放: {e}")

    async def load_main_page(self):
        views = await self._request(
            method="GET",
            path=f"/Users/{self.user_id}/Views",
            params=dict(IncludeExternalContent=False),
        )

        col_ids = []
        for i in views.json().get("Items", []):
            cid: str = i.get("Id", None)
            type: str = i.get("CollectionType")
            if cid and type and type.lower() in ("movies", "tvshows"):
                col_ids.append(cid)
        await asyncio.sleep(random.uniform(0.1, 0.3))

        user = await self._request(method="GET", path=f"/Users/{self.user_id}")
        last_login_date = user.json().get("LastLoginDate", None)
        await asyncio.sleep(random.uniform(0.1, 0.3))

        await self._request(
            method="GET",
            path=f"/DisplayPreferences/usersettings",
            params=dict(client="emby", userId=self.user_id),
        )
        await asyncio.sleep(random.uniform(0.1, 0.3))

        for item in await self.get_resume_items(media_types=["Video"]):
            try:
                self.items[item["Id"]] = item
            except KeyError:
                pass
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await self.get_resume_items(media_types=["Audio"])
        await asyncio.sleep(random.uniform(0.1, 0.3))

        for cid in col_ids[:25]:
            items = await self.get_latest_items(parent_id=cid)
            for item in items:
                try:
                    iid = item["Id"]
                    self.items[iid] = item
                except KeyError:
                    pass

        if not self.items:
            if col_ids:
                self.log.info("无法获取最新视频, 尝试从文件夹中读取.")

                for col_id in col_ids[:3]:
                    await asyncio.sleep(4)
                    items = await self.get_folder_items(parent_id=col_id)
                    for item in items:
                        try:
                            iid = item["Id"]
                            self.items[iid] = item
                        except KeyError:
                            pass
                    if len(self.items) >= 3:
                        break

        return last_login_date

    async def resolve_playable_item(self, iid: str, item: dict) -> Optional[dict]:
        if item.get("MediaType") == "Video":
            return item

        try:
            detail = await self.get_item(iid)
        except EmbyError:
            return None

        if detail.get("MediaType") == "Video":
            self.items[iid] = detail
            return detail

        item_type = detail.get("Type") or item.get("Type")
        if item_type in ("Series", "Season", "BoxSet", "Folder"):
            children = await self.get_folder_items(parent_id=iid, limit=24)
            for child in children:
                child_id = child.get("Id")
                if child_id:
                    self.items[child_id] = child
            for child in children:
                if child.get("MediaType") == "Video":
                    return child

        return None

    async def get_latest_items(
        self,
        enable_image_types=None,
        fields=None,
        limit=16,
        group_items=True,
        parent_id=None,
        **kw,
    ) -> List[dict]:
        if not enable_image_types:
            enable_image_types = ["Primary", "Backdrop", "Thumb"]
        if not fields:
            fields = [
                "PrimaryImageAspectRatio",
                "BasicSyncInfo",
                "ProductionYear",
                "Status",
                "EndDate",
                "CanDelete",
            ]
        resp = await self._request(
            method="GET",
            path=f"/Users/{self.user_id}/Items/Latest",
            params={
                "EnableImageTypes": ",".join(enable_image_types),
                "Fields": ",".join(fields),
                "GroupItems": group_items,
                "Limit": limit,
                "ParentId": parent_id,
                **kw,
            },
        )
        return resp.json()

    async def get_resume_items(
        self,
        enable_image_types=None,
        fields=None,
        limit=12,
        media_types=None,
        **kw,
    ) -> List[dict]:
        if not enable_image_types:
            enable_image_types = ["Primary", "Backdrop", "Thumb"]
        if not fields:
            fields = ["PrimaryImageAspectRatio", "BasicSyncInfo", "ProductionYear", "CanDelete"]
        if not media_types:
            media_types = ["Video"]
        resp = await self._request(
            method="GET",
            path=f"/Users/{self.user_id}/Items/Resume",
            params={
                "EnableImageTypes": ",".join(enable_image_types),
                "Fields": ",".join(fields),
                "Limit": limit,
                "MediaTypes": ",".join(media_types),
                "Recursive": "true",
                **kw,
            },
        )
        data = resp.json()
        if isinstance(data, dict):
            return data.get("Items", [])
        return data

    async def get_folder_items(
        self,
        parent_id,
        enable_image_types=None,
        fields=None,
        limit=50,
        **kw,
    ) -> List[dict]:
        if not enable_image_types:
            enable_image_types = ["Primary", "Backdrop", "Thumb"]
        if not fields:
            fields = ["BasicSyncInfo", "CanDelete", "PrimaryImageAspectRatio", "ProductionYear"]
        resp = await self._request(
            method="GET",
            path=f"/Users/{self.user_id}/Items",
            params={
                "EnableImageTypes": ",".join(enable_image_types),
                "Fields": ",".join(fields),
                "ImageTypeLimit": 1,
                "IncludeItemTypes": "Movie,Episode",
                "Limit": limit,
                "ParentId": parent_id,
                "Recursive": "true",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "StartIndex": 0,
                **kw,
            },
        )
        return resp.json().get("Items", [])

    async def get_item(self, iid, **kw) -> dict:
        resp = await self._request(method="GET", path=f"/Users/{self.user_id}/Items/{iid}")
        return resp.json()

    async def get_user(self) -> dict:
        """Get current user information."""
        response = await self._request("GET", f"/Users/{self.user_id}")
        return response.json()

    async def mark_played(self, item_id: str) -> bool:
        """Mark an item as played."""
        response = await self._request("POST", f"/Users/{self.user_id}/PlayedItems/{item_id}")
        return response.status_code == 200

    async def watch(self):
        """Play one or more videos until account time requirement played."""

        try:
            if isinstance(self.a.time, Iterable):
                req_time = random.uniform(*self.a.time)
            else:
                req_time = self.a.time
        except TypeError:
            self.log.warning(f"无法解析 time 配置, 请检查配置: {self.a.time} (应该为数字或两个数字的数组).")
            return False
        msg = " (允许播放多个)" if self.a.allow_multiple else ""
        msg = f"开始播放视频{msg}, 共需播放 {req_time:.0f} 秒."
        self.log.info(msg)

        played_time = 0
        last_played_time = 0
        played_videos = 0
        retry = 0
        failed_items = []
        failed_reasons = {"invalid": 0, "no_length": 0, "wrong_type": 0, "short_length": 0}

        while True:
            shuffled_items = list(self.items.items())
            random.shuffle(shuffled_items)

            for iid, item in shuffled_items:
                try:
                    if iid in failed_items:
                        failed_reasons["invalid"] += 1
                        continue
                except KeyError:
                    continue
                item = await self.resolve_playable_item(iid, item)
                if not item:
                    failed_reasons["wrong_type"] += 1
                    continue
                iid = item["Id"]
                total_ticks = item.get("RunTimeTicks", None)
                if not total_ticks:
                    if self.a.allow_stream:
                        total_ticks = min(req_time, random.randint(480, 720)) * 10000000
                    else:
                        failed_reasons["no_length"] += 1
                        continue
                total_time = total_ticks / 10000000
                if req_time - played_time > total_time:
                    if not self.a.allow_multiple:
                        failed_reasons["short_length"] += 1
                        failed_items.append(iid)
                        continue
                    play_time = total_time
                else:
                    play_time = max(req_time - played_time, 10)
                name = truncate_str(item.get("Name", "(未命名视频)"), 10)
                self.log.info(f'开始播放 "{name}" ({play_time:.0f} 秒).')
                self.log.debug(f"视频 ID: {iid}.")
                while True:
                    try:
                        await self.play(item, time=play_time)
                        await asyncio.sleep(random.random())
                        item = await self.get_item(iid)
                        play_count = item.get("UserData", {}).get("PlayCount", 0)
                        if play_count < 1:
                            raise EmbyPlayError("播放后播放数低于 1")
                        self.log.info(f"[yellow]成功播放视频[/], 当前该视频播放 {play_count} 次.")
                        played_videos += 1
                        played_time += play_time
                        if played_time >= req_time - 1:
                            self.log.bind(log=True).info(f"保活成功, 共播放 {played_videos} 个视频.")
                            return True
                        else:
                            self.log.info(f"还需播放 {req_time - played_time:.0f} 秒.")
                            rt = random.uniform(5, 15)
                            self.log.info(f"等待 {rt:.0f} 秒后播放下一个.")
                            await asyncio.sleep(rt)
                            break
                    except EmbyError as e:
                        retry += 1
                        if retry > config.emby.retries:
                            self.log.warning(f"超过最大重试次数, 保活失败: {e}.")
                            return False
                        else:
                            rt = random.uniform(30, 60)
                            if isinstance(e, EmbyPlayError):
                                self.log.info(f"播放错误, 等待 {rt:.0f} 秒后重试: {e}.")
                            else:
                                self.log.info(f"连接失败, 等待 {rt:.0f} 秒后重试: {e}.")
                            await asyncio.sleep(rt)
                    except Exception as e:
                        self.log.warning(f"发生错误, 保活失败.")
                        show_exception(e, regular=False)
                        return False
            else:
                if len(failed_items) == len(self.items):
                    reasons = []
                    if failed_reasons["invalid"]:
                        reasons.append(f"{failed_reasons['invalid']} 个视频信息无效")
                    if failed_reasons["no_length"]:
                        reasons.append(f"{failed_reasons['no_length']} 个视频无法获取时长")
                    if failed_reasons["wrong_type"]:
                        reasons.append(f"{failed_reasons['wrong_type']} 个非视频项目")
                    if failed_reasons["short_length"]:
                        reasons.append(
                            f"{failed_reasons['short_length']} 个视频时长不足 (未开启 allow_multiple)"
                        )
                    self.log.warning(f"所有视频均不符合要求, 保活失败. 其中: {', '.join(reasons)}")
                elif played_time > last_played_time:
                    last_played_time = played_time
                    continue
                else:
                    self.log.warning(f"由于没有成功播放视频, 保活失败, 请重新检查配置.")
                    return False

    @staticmethod
    def parse_date(date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
