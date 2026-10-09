import asyncio
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:  # spotipy opsional
    spotipy = None

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
SPOTIFY_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

IDLE_TIMEOUT = 180        # detik, keluar VC kalau tidak ada lagu
MAX_PLAYLIST = 100        # batas lagu per playlist/album
STREAM_TTL = 1800         # detik, link stream di-resolve ulang setelah ini

YTDL_SINGLE = {
    "format": "bestaudio[protocol=https]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": False,
    "source_address": "0.0.0.0",
}
YTDL_PLAYLIST = {**YTDL_SINGLE, "noplaylist": False, "extract_flat": "in_playlist"}

FFMPEG_RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def ffmpeg_opts_for(start_at: float = 0.0) -> dict:
    """Opsi FFmpeg: reconnect otomatis kalau koneksi stream putus, dan
    opsional lanjut dari detik tertentu (-ss)."""
    before = FFMPEG_RECONNECT
    if start_at and start_at > 0:
        before = f"-ss {int(start_at)} " + before
    return {"before_options": before, "options": "-vn"}


class CountingSource(discord.PCMVolumeTransformer):
    """PCMVolumeTransformer yang menghitung posisi putar (1 frame = 20 ms)."""

    def __init__(self, original, volume: float = 1.0, offset: float = 0.0):
        super().__init__(original, volume)
        self.frames = 0
        self.offset = offset

    def read(self):
        data = super().read()
        if data:
            self.frames += 1
        return data

    @property
    def position(self) -> float:
        return self.offset + self.frames * 0.02


SPOTIFY_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
)
YT_PLAYLIST_RE = re.compile(r"youtube\.com/playlist\?list=")
URL_RE = re.compile(r"^https?://", re.I)


# ----------------------------------------------------------------- helpers
def fmt_dur(seconds) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@dataclass
class Track:
    title: str
    query: str                      # URL atau "ytsearch1:..."
    requester: discord.abc.User
    duration: Optional[int] = None
    url: Optional[str] = None       # halaman video (untuk ditampilkan)
    thumbnail: Optional[str] = None
    stream_url: Optional[str] = None
    resolved_at: float = 0.0
    start_at: float = 0.0           # lanjut dari detik ini (kalau stream putus)
    retries: int = 0


async def ytdl_extract(query: str, opts: dict):
    loop = asyncio.get_running_loop()

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, run)


def fill_track(track: Track, info: dict):
    track.title = info.get("title") or track.title
    track.duration = info.get("duration") or track.duration
    track.url = info.get("webpage_url") or track.url
    track.thumbnail = info.get("thumbnail") or track.thumbnail
    track.stream_url = info.get("url")
    track.resolved_at = time.time()


async def resolve_stream(track: Track) -> str:
    if track.stream_url and time.time() - track.resolved_at < STREAM_TTL:
        return track.stream_url
    info = await ytdl_extract(track.query, YTDL_SINGLE)
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("Lagu tidak ditemukan.")
        info = entries[0]
    fill_track(track, info)
    return track.stream_url


class SpotifyNotConfigured(Exception):
    pass


_sp = None


def get_spotify():
    global _sp
    if _sp is None:
        if not (spotipy and SPOTIFY_ID and SPOTIFY_SECRET):
            raise SpotifyNotConfigured()
        _sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(SPOTIFY_ID, SPOTIFY_SECRET)
        )
    return _sp


def spotify_pairs(kind: str, sid: str):
    """Blocking: kembalikan list (artis, judul)."""
    sp = get_spotify()
    pairs = []
    if kind == "track":
        t = sp.track(sid)
        pairs.append((t["artists"][0]["name"], t["name"]))
    elif kind == "album":
        res = sp.album_tracks(sid)
        while res:
            for t in res["items"]:
                pairs.append((t["artists"][0]["name"], t["name"]))
            res = sp.next(res) if res["next"] and len(pairs) < MAX_PLAYLIST else None
    else:
        res = sp.playlist_items(sid, additional_types=("track",))
        while res:
            for it in res["items"]:
                t = it.get("track")
                if t and t.get("name"):
                    pairs.append((t["artists"][0]["name"], t["name"]))
            res = sp.next(res) if res["next"] and len(pairs) < MAX_PLAYLIST else None
    return pairs[:MAX_PLAYLIST]


async def resolve_tracks(query: str, requester) -> list[Track]:
    query = query.strip()

    # --- Spotify
    m = SPOTIFY_RE.search(query)
    if m:
        loop = asyncio.get_running_loop()
        pairs = await loop.run_in_executor(None, spotify_pairs, m.group(1), m.group(2))
        if not pairs:
            raise RuntimeError("Tidak ada lagu yang bisa diambil dari link Spotify itu.")
        return [
            Track(
                title=f"{artist} - {title}",
                query=f"ytsearch1:{artist} {title} official audio",
                requester=requester,
            )
            for artist, title in pairs
        ]

    # --- Playlist YouTube
    if YT_PLAYLIST_RE.search(query):
        info = await ytdl_extract(query, YTDL_PLAYLIST)
        tracks = []
        for e in (info.get("entries") or [])[:MAX_PLAYLIST]:
            if e and e.get("url"):
                tracks.append(
                    Track(
                        title=e.get("title") or "Unknown",
                        query=e["url"],
                        requester=requester,
                        duration=e.get("duration"),
                    )
                )
        if not tracks:
            raise RuntimeError("Playlist kosong atau tidak bisa diakses.")
        return tracks

    # --- Link biasa (YouTube, SoundCloud, dll) atau pencarian judul
    q = query if URL_RE.match(query) else f"ytsearch1:{query}"
    info = await ytdl_extract(q, YTDL_SINGLE)
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("Lagu tidak ditemukan.")
        info = entries[0]
    track = Track(title=info.get("title") or "Unknown", query=info.get("webpage_url") or q,
                  requester=requester)
    fill_track(track, info)
    return [track]


# ----------------------------------------------------------------- player
class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.loop_mode = "off"          # off | track | queue
        self.volume = 0.5
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.skipped = False
        self.current_source: Optional[CountingSource] = None
        self.lock = asyncio.Lock()
        self.idle_task: Optional[asyncio.Task] = None

    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client

    def is_active(self) -> bool:
        vc = self.vc
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    async def start_if_idle(self):
        if not self.is_active():
            await self.advance()

    def _after(self, error):
        if error:
            print(f"[player error] {error}")
        asyncio.run_coroutine_threadsafe(self.advance(), self.bot.loop)

    async def advance(self):
        async with self.lock:
            vc = self.vc
            if not vc or not vc.is_connected():
                self.current = None
                return
            if vc.is_playing() or vc.is_paused():
                return

            prev, repeat = self.current, not self.skipped
            self.skipped = False
            src = self.current_source
            self.current_source = None
            if prev and repeat:
                pos = src.position if src else 0
                if prev.duration and pos < prev.duration - 10 and prev.retries < 3:
                    # Berhenti sebelum selesai (stream putus): lanjutkan dari posisi terakhir
                    prev.retries += 1
                    prev.start_at = pos
                    prev.stream_url = None      # paksa ambil link baru
                    print(f"[retry] {prev.title}: berhenti di {pos:.0f}s dari {prev.duration}s, "
                          f"lanjut (percobaan {prev.retries})")
                    self.queue.appendleft(prev)
                else:
                    prev.start_at, prev.retries = 0.0, 0
                    if self.loop_mode == "track":
                        self.queue.appendleft(prev)
                    elif self.loop_mode == "queue":
                        self.queue.append(prev)
            self.current = None

            while self.queue:
                track = self.queue.popleft()
                try:
                    stream = await resolve_stream(track)
                except Exception as e:
                    if self.text_channel:
                        await self.text_channel.send(
                            f"⚠️ Gagal memutar **{track.title}**, dilewati. (`{str(e)[:150]}`)"
                        )
                    continue
                source = CountingSource(
                    discord.FFmpegPCMAudio(stream, stderr=sys.stderr, **ffmpeg_opts_for(track.start_at)),
                    volume=self.volume, offset=track.start_at,
                )
                self.current = track
                self.current_source = source
                print(f"[debug] memutar: {track.title} | {stream[:110]}...")
                vc.play(source, after=self._after)
                self._cancel_idle()
                if self.text_channel and track.start_at == 0 and not (prev is track and self.loop_mode == "track"):
                    await self.text_channel.send(embed=now_playing_embed(track))
                return

            self._schedule_idle()

    def _cancel_idle(self):
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None

    def _schedule_idle(self):
        self._cancel_idle()
        self.idle_task = asyncio.create_task(self._idle_disconnect())

    async def _idle_disconnect(self):
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self.vc and not self.is_active():
            if self.text_channel:
                await self.text_channel.send("👋 Tidak ada aktivitas, saya keluar dari voice channel.")
            await self.vc.disconnect()

    def stop(self):
        self.queue.clear()
        self.loop_mode = "off"
        self.skipped = True
        if self.vc:
            self.vc.stop()


def now_playing_embed(track: Track) -> discord.Embed:
    e = discord.Embed(
        title="🎶 Sedang diputar",
        description=f"[{track.title}]({track.url})" if track.url else track.title,
        color=discord.Color.blurple(),
    )
    e.add_field(name="Durasi", value=fmt_dur(track.duration))
    e.add_field(name="Request oleh", value=track.requester.mention)
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    return e


# ----------------------------------------------------------------- cog
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    # ---- util
    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self.bot, guild)
        return self.players[guild.id]

    async def destroy_player(self, guild: discord.Guild):
        player = self.players.pop(guild.id, None)
        if player:
            player._cancel_idle()
            player.queue.clear()
        if guild.voice_client:
            await guild.voice_client.disconnect()

    async def ensure_voice(self, ctx: commands.Context) -> bool:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Kamu harus masuk voice channel dulu.")
            return False
        channel = ctx.author.voice.channel
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.connect and perms.speak):
            await ctx.send("❌ Bot tidak punya izin View Channel / Connect / Speak di channel itu.")
            return False
        if channel.user_limit and len(channel.members) >= channel.user_limit:
            await ctx.send("❌ Voice channel sudah penuh.")
            return False
        if ctx.voice_client is None:
            await channel.connect(self_deaf=True)
        elif ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)
        return True

    async def control_player(self, ctx: commands.Context) -> Optional[GuildPlayer]:
        player = self.players.get(ctx.guild.id)
        if not player or not ctx.voice_client:
            await ctx.send("❌ Bot tidak sedang memutar apa pun.")
            return None
        if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ Kamu harus berada di voice channel yang sama dengan bot.")
            return None
        return player

    # ---- commands
    @commands.hybrid_command(name="play", aliases=["p"],
                             description="Putar lagu dari judul, link YouTube, atau link Spotify")
    @app_commands.describe(query="Judul lagu, link YouTube, atau link Spotify")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str):
        if not await self.ensure_voice(ctx):
            return
        await ctx.defer()
        status = await ctx.send(f"🔎 Mencari: **{query[:80]}** ...")

        async def reply(text: str):
            try:
                await status.edit(content=text)
            except Exception:
                await ctx.send(text)

        try:
            tracks = await asyncio.wait_for(resolve_tracks(query, ctx.author), timeout=90)
        except SpotifyNotConfigured:
            return await reply(
                "❌ Link Spotify butuh `SPOTIFY_CLIENT_ID` dan `SPOTIFY_CLIENT_SECRET` di file `.env`."
            )
        except asyncio.TimeoutError:
            return await reply("❌ Pencarian terlalu lama (timeout). Coba lagi.")
        except Exception as e:
            print(f"[play error] {e!r}")
            return await reply(f"❌ Gagal mengambil lagu: `{str(e)[:200]}`")

        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel
        was_idle = not player.is_active()
        player.queue.extend(tracks)

        if len(tracks) > 1:
            await reply(f"✅ Menambahkan **{len(tracks)}** lagu ke antrean.")
        elif not was_idle:
            t = tracks[0]
            pos = len(player.queue)
            await reply(f"✅ Masuk antrean #{pos}: **{t.title}** `[{fmt_dur(t.duration)}]`")
        else:
            await reply("🔎 Lagu ditemukan!")
        await player.start_if_idle()

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Lewati lagu saat ini")
    @commands.guild_only()
    async def skip(self, ctx):
        player = await self.control_player(ctx)
        if player and player.is_active():
            player.skipped = True
            ctx.voice_client.stop()
            await ctx.send("⏭️ Dilewati.")

    @commands.hybrid_command(name="pause", description="Jeda lagu")
    @commands.guild_only()
    async def pause(self, ctx):
        player = await self.control_player(ctx)
        if player and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Dijeda.")

    @commands.hybrid_command(name="resume", aliases=["unpause"], description="Lanjutkan lagu")
    @commands.guild_only()
    async def resume(self, ctx):
        player = await self.control_player(ctx)
        if player and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Dilanjutkan.")

    @commands.hybrid_command(name="stop", description="Hentikan lagu dan kosongkan antrean")
    @commands.guild_only()
    async def stop(self, ctx):
        player = await self.control_player(ctx)
        if player:
            player.stop()
            await ctx.send("⏹️ Dihentikan dan antrean dikosongkan.")

    @commands.hybrid_command(name="leave", aliases=["dc", "disconnect"], description="Keluarkan bot dari voice channel")
    @commands.guild_only()
    async def leave(self, ctx):
        player = await self.control_player(ctx)
        if player:
            await self.destroy_player(ctx.guild)
            await ctx.send("👋 Sampai jumpa!")

    @commands.hybrid_command(name="queue", aliases=["q"], description="Lihat antrean")
    @commands.guild_only()
    async def queue(self, ctx, page: int = 1):
        player = self.players.get(ctx.guild.id)
        if not player or (not player.current and not player.queue):
            return await ctx.send("📭 Antrean kosong.")
        items = list(player.queue)
        per_page = 10
        pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        lines = [
            f"`{start + i + 1}.` {t.title} `[{fmt_dur(t.duration)}]`"
            for i, t in enumerate(items[start:start + per_page])
        ]
        e = discord.Embed(title="📜 Antrean", color=discord.Color.blurple())
        if player.current:
            e.add_field(name="Sedang diputar", value=player.current.title, inline=False)
        e.add_field(name="Berikutnya", value="\n".join(lines) or "—", inline=False)
        e.set_footer(text=f"Halaman {page}/{pages} • {len(items)} lagu • loop: {player.loop_mode}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="Lagu yang sedang diputar")
    @commands.guild_only()
    async def nowplaying(self, ctx):
        player = self.players.get(ctx.guild.id)
        if not player or not player.current:
            return await ctx.send("❌ Tidak ada lagu yang sedang diputar.")
        await ctx.send(embed=now_playing_embed(player.current))

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Atur volume (0-100)")
    @app_commands.describe(level="Volume 0-100")
    @commands.guild_only()
    async def volume(self, ctx, level: commands.Range[int, 0, 100]):
        player = await self.control_player(ctx)
        if player:
            player.volume = level / 100
            if ctx.voice_client.source:
                ctx.voice_client.source.volume = player.volume
            await ctx.send(f"🔊 Volume: **{level}%**")

    @commands.hybrid_command(name="loop", description="Ganti mode loop: off → track → queue")
    @commands.guild_only()
    async def loop(self, ctx):
        player = await self.control_player(ctx)
        if player:
            order = ["off", "track", "queue"]
            player.loop_mode = order[(order.index(player.loop_mode) + 1) % 3]
            label = {"off": "mati", "track": "🔂 satu lagu", "queue": "🔁 seluruh antrean"}
            await ctx.send(f"Loop: **{label[player.loop_mode]}**")

    @commands.hybrid_command(name="shuffle", description="Acak antrean")
    @commands.guild_only()
    async def shuffle(self, ctx):
        player = await self.control_player(ctx)
        if player:
            items = list(player.queue)
            random.shuffle(items)
            player.queue = deque(items)
            await ctx.send("🔀 Antrean diacak.")

    @commands.hybrid_command(name="remove", description="Hapus lagu dari antrean berdasarkan nomor")
    @commands.guild_only()
    async def remove(self, ctx, index: int):
        player = await self.control_player(ctx)
        if not player:
            return
        if not 1 <= index <= len(player.queue):
            return await ctx.send("❌ Nomor tidak valid.")
        items = list(player.queue)
        removed = items.pop(index - 1)
        player.queue = deque(items)
        await ctx.send(f"🗑️ Dihapus: **{removed.title}**")

    @commands.hybrid_command(name="clear", description="Kosongkan antrean (lagu saat ini tetap main)")
    @commands.guild_only()
    async def clear(self, ctx):
        player = await self.control_player(ctx)
        if player:
            player.queue.clear()
            await ctx.send("🧹 Antrean dikosongkan.")

    # ---- events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        if member.id == self.bot.user.id and after.channel is None:
            player = self.players.pop(guild.id, None)
            if player:
                player._cancel_idle()
            return
        vc = guild.voice_client
        if vc and vc.channel and all(m.bot for m in vc.channel.members):
            await asyncio.sleep(60)
            vc = guild.voice_client
            if vc and vc.channel and all(m.bot for m in vc.channel.members):
                await self.destroy_player(guild)


# ----------------------------------------------------------------- bot
class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # aktifkan juga di Developer Portal
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned_or(PREFIX), intents=intents)

    async def setup_hook(self):
        await self.add_cog(Music(self))
        await self.tree.sync()           # daftarkan slash command

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        while hasattr(error, "original") and error.original is not None:
            error = error.original
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"❌ Kurang argumen: `{error.param.name}`. Contoh: `{PREFIX}play judul lagu`")
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            await ctx.send(f"❌ Terjadi error: `{type(error).__name__}: {str(error)[:150]}`")
        except Exception:
            pass

    async def on_ready(self):
        print(f"Login sebagai {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening, name=f"{PREFIX}play | /play"))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN belum diisi di file .env")
    MusicBot().run(TOKEN)
