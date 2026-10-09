# Bot Musik Discord (ala Jockie Music)

Fitur: putar dari **judul lagu**, **link YouTube** (video & playlist), **link Spotify** (track/album/playlist), antrean, loop, shuffle, volume, auto-leave saat sepi.

## Setup
1. **Buat bot**: https://discord.com/developers/applications → New Application → Bot → copy token.
   Aktifkan **Message Content Intent** di tab Bot.
2. **Invite bot**: OAuth2 → URL Generator → scope `bot` + `applications.commands`,
   permission: View Channels, Send Messages, Embed Links, Connect, Speak.
3. **Install FFmpeg** (wajib): Windows `winget install Gyan.FFmpeg`, Linux `sudo apt install ffmpeg`, Mac `brew install ffmpeg`.
   **Install Deno** (runtime JS untuk yt-dlp/YouTube): Windows `winget install DenoLand.Deno`. Buka terminal baru setelah install.
4. Install dependensi:
   ```
   python -m venv venv
   venv\Scripts\activate          # Windows
   source venv/bin/activate        # Linux/Mac
   pip install -r requirements.txt
   ```
5. Salin `.env.example` jadi `.env`, isi token (dan kunci Spotify jika mau).
6. Jalankan: `python bot.py`

## Perintah (prefix `!` atau slash `/`)
| Perintah | Fungsi |
|---|---|
| `!play <judul/link>` (`!p`) | Putar / tambah ke antrean |
| `!skip` `!pause` `!resume` `!stop` | Kontrol dasar |
| `!queue [halaman]` (`!q`) | Lihat antrean |
| `!nowplaying` (`!np`) | Lagu saat ini |
| `!volume 0-100` | Atur volume |
| `!loop` | Ganti mode: off → track → queue |
| `!shuffle` `!remove <no>` `!clear` | Kelola antrean |
| `!leave` (`!dc`) | Keluar dari voice channel |

## Catatan
- Spotify tidak bisa di-stream langsung; bot membaca judul/artis dari Spotify lalu mencari versi audionya di YouTube.
- Jika tiba-tiba gagal memutar, update yt-dlp: `pip install -U yt-dlp` (YouTube sering berubah).
- Jika di-host di VPS/cloud, YouTube kadang memblokir IP datacenter; solusinya pakai cookies yt-dlp atau host di PC/IP rumahan.
- Memutar audio YouTube lewat bot melanggar ToS YouTube, jadi sebaiknya dipakai untuk server pribadi/teman, bukan bot publik komersial.
