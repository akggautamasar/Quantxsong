import os
import re
import threading
import tempfile
import logging
import requests
from flask import Flask, jsonify, request, Response, send_from_directory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
AIRSONGS_API = 'https://airsongsapi.vercel.app'
YT_COOKIES = os.environ.get('YT_COOKIES', '')  # Netscape cookies.txt content

# Write cookies to a temp file if provided
COOKIES_FILE = None
if YT_COOKIES:
    _cf = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    _cf.write(YT_COOKIES)
    _cf.close()
    COOKIES_FILE = _cf.name
    log.info(f'YouTube cookies loaded from env → {COOKIES_FILE}')

def get_ydl_opts(extra=None):
    opts = {
        'quiet': False,
        'no_warnings': False,
        'nocheckcertificate': True,
        # ios client provides pre-signed URLs — no signature solving needed
        'extractor_args': {
            'youtube': {
                'player_client': ['ios'],
            }
        },
        'http_headers': {
            'User-Agent': 'com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)',
        },
    }
    if COOKIES_FILE:
        opts['cookiefile'] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts

# ─── In-memory caches ────────────────────────────────────────────
song_cache = {}
yt_cache = {}
cache_counter = [0]

def cache_song(song):
    cache_counter[0] += 1
    key = str(cache_counter[0])
    song_cache[key] = song
    if cache_counter[0] > 500:
        song_cache.pop(str(cache_counter[0] - 500), None)
    return key

def cache_yt(video):
    cache_counter[0] += 1
    key = 'yt' + str(cache_counter[0])
    yt_cache[key] = video
    return key

# ─── Telegram helpers ────────────────────────────────────────────
def tg(method, **kwargs):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}'
    r = requests.post(url, json=kwargs, timeout=30)
    return r.json()

def send(chat_id, text, **kwargs):
    return tg('sendMessage', chat_id=chat_id, text=text, **kwargs)

def send_md(chat_id, text, **kwargs):
    return tg('sendMessage', chat_id=chat_id, text=text, parse_mode='Markdown', **kwargs)

def send_photo(chat_id, photo, caption, keyboard=None):
    kwargs = dict(chat_id=chat_id, photo=photo, caption=caption, parse_mode='Markdown')
    if keyboard:
        kwargs['reply_markup'] = keyboard
    return tg('sendPhoto', **kwargs)

def send_audio(chat_id, audio_bytes, filename, title, performer, duration):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio'
    r = requests.post(url, data={
        'chat_id': chat_id,
        'title': title,
        'performer': performer,
        'duration': duration,
    }, files={
        'audio': (filename, audio_bytes, 'audio/mp4')
    }, timeout=120)
    return r.json()

def answer_cb(callback_id, text=''):
    return tg('answerCallbackQuery', callback_query_id=callback_id, text=text)

def chat_action(chat_id, action):
    return tg('sendChatAction', chat_id=chat_id, action=action)

def inline_kb(*rows):
    return {'inline_keyboard': list(rows)}

def yt_search_innertube(query, limit=5):
    """Search YouTube using InnerTube API — not blocked like yt-dlp scraping."""
    url = 'https://www.youtube.com/youtubei/v1/search?prettyPrint=false'
    payload = {
        'context': {
            'client': {
                'clientName': 'WEB',
                'clientVersion': '2.20240101.00.00',
            }
        },
        'query': query,
    }
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0',
        'X-YouTube-Client-Name': '1',
        'X-YouTube-Client-Version': '2.20240101.00.00',
    }
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    data = r.json()

    videos = []
    try:
        contents = data['contents']['twoColumnSearchResultsRenderer']['primaryContents']['sectionListRenderer']['contents']
        for section in contents:
            items = section.get('itemSectionRenderer', {}).get('contents', [])
            for item in items:
                v = item.get('videoRenderer')
                if not v:
                    continue
                vid_id = v.get('videoId')
                title = v.get('title', {}).get('runs', [{}])[0].get('text', '')
                channel = v.get('ownerText', {}).get('runs', [{}])[0].get('text', 'Unknown')
                duration_text = v.get('lengthText', {}).get('simpleText', '?')

                # Convert duration string to seconds
                dur_sec = 0
                try:
                    parts = duration_text.split(':')
                    if len(parts) == 2:
                        dur_sec = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        dur_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except:
                    pass

                videos.append({
                    'id': vid_id,
                    'title': title,
                    'channel': channel,
                    'duration': dur_sec,
                    'duration_str': duration_text,
                    'thumbnail': f'https://img.youtube.com/vi/{vid_id}/mqdefault.jpg'
                })
                if len(videos) >= limit:
                    return videos
    except Exception as e:
        log.error(f'InnerTube parse error: {e}')

    return videos
    if not sec:
        return '?'
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m2 = divmod(m, 60)
    return f'{h}:{m2:02}:{s:02}' if h else f'{m}:{s:02}'

def safe_name(text):
    return re.sub(r'[^a-zA-Z0-9 _\-]', '', text or '').strip() or 'audio'

# ─── Bot message handler ─────────────────────────────────────────
def handle_message(msg):
    chat_id = msg['chat']['id']
    text = msg.get('text', '')
    if not text:
        return

    if text == '/start':
        send_md(chat_id,
            '🎵 *Welcome to QuantX Songs Bot!* 🎵\n\n'
            'Search for any song:\n'
            '• 🎧 Stream & download music\n'
            '• 📝 Get lyrics\n'
            '• 🎬 YouTube search with /yt\n\n'
            '*Examples:*\n'
            '• Arjan Vailly\n'
            '• Shape of You\n'
            '• /yt Blinding Lights\n\n'
            'Built with ❤️ by QuantX'
        )
        return

    if text == '/help':
        send_md(chat_id,
            '🤖 *Commands:*\n\n'
            '/start - Welcome message\n'
            '/help - This message\n'
            '/yt SongName - Search YouTube\n\n'
            '🔍 Just type any song name to search!'
        )
        return

    if text.lower().startswith('/yt'):
        query = re.sub(r'^/yt\s*', '', text, flags=re.IGNORECASE).strip()
        if not query:
            send(chat_id, '❌ Please provide a song name.\nExample: /yt Shape of You')
            return
        handle_yt_search(chat_id, query)
        return

    if text.startswith('/'):
        return

    # AirSongs search
    handle_airsongs_search(chat_id, text)


def handle_airsongs_search(chat_id, query):
    try:
        chat_action(chat_id, 'typing')
        r = requests.get(f'{AIRSONGS_API}/result/', params={'query': query}, timeout=15)
        data = r.json()

        if not isinstance(data, list) or len(data) == 0:
            send(chat_id, '❌ No songs found. Try a different search term.')
            return

        songs = data[:5]
        send(chat_id, f'🔍 Found {len(songs)} results for "{query}":')

        for song in songs:
            key = cache_song(song)
            dur = fmt_dur(song.get('duration', 0))
            info = (
                f"🎵 *{song.get('song')}*\n"
                f"👤 Artist: {song.get('primary_artists')}\n"
                f"💽 Album: {song.get('album')}\n"
                f"⏱️ Duration: {dur}\n"
                f"🗓️ Year: {song.get('year')}\n"
                f"🌐 Language: {song.get('language')}"
            )
            kb = inline_kb(
                [{'text': '🎧 Stream', 'callback_data': f'stream_{key}'},
                 {'text': '📥 Download', 'callback_data': f'download_{key}'}],
                [{'text': '📝 Lyrics', 'callback_data': f'lyrics_{key}'},
                 {'text': 'ℹ️ Info', 'callback_data': f'info_{key}'}]
            )
            img = song.get('image')
            if img:
                send_photo(chat_id, img, info, kb)
            else:
                send_md(chat_id, info, reply_markup=kb)

    except Exception as e:
        log.error(f'AirSongs search error: {e}')
        send(chat_id, '❌ Sorry, there was an error. Please try again.')


def handle_yt_search(chat_id, query):
    try:
        chat_action(chat_id, 'typing')
        send(chat_id, f'🎬 Searching YouTube for "{query}"...')

        videos = yt_search_innertube(query, limit=5)

        if not videos:
            send(chat_id, '❌ No YouTube results found.')
            return

        for video in videos:
            key = cache_yt(video)
            info = (
                f"🎬 *{video['title']}*\n"
                f"👤 Channel: {video['channel']}\n"
                f"⏱️ Duration: {video['duration_str']}"
            )
            kb = inline_kb([{'text': '🎧 Download Audio', 'callback_data': f'ytdl_{key}'}])
            send_photo(chat_id, video['thumbnail'], info, kb)

    except Exception as e:
        log.error(f'YT search error: {e}')
        send(chat_id, '❌ YouTube search failed. Please try again.')


# ─── Bot callback handler ─────────────────────────────────────────
def handle_callback(cb):
    chat_id = cb['message']['chat']['id']
    data = cb['data']
    cb_id = cb['id']

    if data.startswith('ytdl_'):
        handle_yt_download(chat_id, cb_id, data.replace('ytdl_', ''))
        return

    idx = data.index('_')
    action = data[:idx]
    key = data[idx+1:]
    song = song_cache.get(key)

    if not song:
        answer_cb(cb_id, '❌ Session expired. Search again.')
        return

    if action == 'stream':
        answer_cb(cb_id, '🎧 Downloading...')
        chat_action(chat_id, 'upload_audio')
        media_url = song.get('media_url')
        if not media_url:
            send(chat_id, '❌ Stream not available for this song.')
            return
        try:
            r = requests.get(media_url, timeout=60)
            fname = f"{safe_name(song.get('song'))} - {song.get('primary_artists', '')}.m4a"
            result = send_audio(
                chat_id, r.content, fname,
                song.get('song', ''), song.get('primary_artists', ''),
                int(song.get('duration') or 0)
            )
            if not result.get('ok'):
                send(chat_id, '❌ Failed to send audio. Try download link instead.')
        except Exception as e:
            log.error(f'Stream error: {e}')
            send(chat_id, '❌ Stream failed. Please try again.')

    elif action == 'download':
        answer_cb(cb_id, '📥 Link sent!')
        media_url = song.get('media_url')
        if media_url:
            send_md(chat_id, f"📥 *Download Link:*\n{media_url}\n\nClick to download MP3.")
        else:
            send(chat_id, '❌ Download not available for this song.')

    elif action == 'lyrics':
        answer_cb(cb_id, '📝 Loading lyrics...')
        chat_action(chat_id, 'typing')
        try:
            r = requests.get(f'{AIRSONGS_API}/lyrics/', params={'query': song.get('id')}, timeout=15)
            data2 = r.json()
            if data2.get('success') and data2.get('data', {}).get('lyrics'):
                lyrics = data2['data']['lyrics']
                if len(lyrics) > 3800:
                    lyrics = lyrics[:3800] + '\n...'
                send_md(chat_id, f"📝 *Lyrics for {song.get('song')}*\n\n{lyrics}")
            else:
                send(chat_id, '❌ Lyrics not available for this song.')
        except Exception as e:
            log.error(f'Lyrics error: {e}')
            send(chat_id, '❌ Error fetching lyrics.')

    elif action == 'info':
        answer_cb(cb_id, 'ℹ️ Info displayed!')
        dur = fmt_dur(song.get('duration', 0))
        pc = song.get('play_count')
        send_md(chat_id,
            f"ℹ️ *Song Information*\n\n"
            f"🎵 *Title:* {song.get('song')}\n"
            f"👤 *Artist:* {song.get('primary_artists')}\n"
            f"💽 *Album:* {song.get('album')}\n"
            f"⏱️ *Duration:* {dur}\n"
            f"🗓️ *Year:* {song.get('year')}\n"
            f"🌐 *Language:* {song.get('language')}\n"
            f"▶️ *Play Count:* {int(pc):,}" if pc else f"▶️ *Play Count:* N/A\n"
            f"🏷️ *Label:* {song.get('label') or 'N/A'}"
        )
    else:
        answer_cb(cb_id, '❌ Unknown action.')


def handle_yt_download(chat_id, cb_id, key):
    video = yt_cache.get(key)
    if not video:
        answer_cb(cb_id, '❌ Session expired. Search again.')
        return

    answer_cb(cb_id, '⏳ Preparing audio...')
    chat_action(chat_id, 'upload_audio')
    send_md(chat_id, f"⏳ Downloading *{video['title']}*...\nThis may take a moment.")

    try:
        vid_url = f"https://www.youtube.com/watch?v={video['id']}"

        # Use cobalt.tools API — handles YouTube blocking
        cobalt_resp = requests.post(
            'https://api.cobalt.tools/',
            json={
                'url': vid_url,
                'downloadMode': 'audio',
                'audioFormat': 'mp3',
                'audioBitrate': '128',
            },
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            },
            timeout=30
        )

        cobalt_data = cobalt_resp.json()
        log.info(f'Cobalt response: {cobalt_data}')

        # cobalt returns {status, url} or {status, tunnel}
        audio_url = cobalt_data.get('url') or cobalt_data.get('tunnel')

        if not audio_url or cobalt_data.get('status') == 'error':
            error_msg = cobalt_data.get('error', {}).get('code', 'Unknown error')
            log.error(f'Cobalt error: {cobalt_data}')
            send(chat_id, f'❌ Could not get audio. Try a different video.')
            return

        # Download the audio
        audio_resp = requests.get(audio_url, timeout=60, stream=True)
        content = audio_resp.content

        size_mb = len(content) / 1024 / 1024
        log.info(f'Audio size: {size_mb:.1f}MB')

        if len(content) > 45 * 1024 * 1024:
            send(chat_id, f'❌ File too large ({size_mb:.1f}MB). Telegram limit is 50MB.')
            return

        fname = f"{safe_name(video['title'])}.mp3"

        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio'
        r = requests.post(url, data={
            'chat_id': chat_id,
            'title': video['title'],
            'performer': video['channel'],
            'duration': int(video.get('duration') or 0),
        }, files={
            'audio': (fname, content, 'audio/mpeg')
        }, timeout=120)

        result = r.json()
        if not result.get('ok'):
            log.error(f"Telegram sendAudio failed: {result}")
            send(chat_id, f"❌ Failed to send audio: {result.get('description', 'Unknown error')}")

    except Exception as e:
        log.error(f'YT download error: {e}', exc_info=True)
        send(chat_id, f'❌ Download failed. Please try a different video.')


# ─── Bot polling thread ──────────────────────────────────────────
def poll_bot():
    log.info('Bot polling started...')
    offset = None
    while True:
        try:
            params = {'timeout': 30, 'allowed_updates': ['message', 'callback_query']}
            if offset:
                params['offset'] = offset
            r = requests.get(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates',
                params=params, timeout=40
            )
            updates = r.json().get('result', [])
            for update in updates:
                offset = update['update_id'] + 1
                try:
                    if 'message' in update:
                        handle_message(update['message'])
                    elif 'callback_query' in update:
                        handle_callback(update['callback_query'])
                except Exception as e:
                    log.error(f'Update error: {e}')
        except Exception as e:
            log.error(f'Polling error: {e}')
            import time; time.sleep(5)


# ─── Flask routes ────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/search')
def yt_search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing ?q='}), 400
    try:
        videos = yt_search_innertube(query, limit=5)
        return jsonify({'results': videos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download')
def yt_download():
    video_id = request.args.get('id', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing ?id='}), 400
    try:
        cobalt_resp = requests.post(
            'https://api.cobalt.tools/',
            json={
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'downloadMode': 'audio',
                'audioFormat': 'mp3',
                'audioBitrate': '128',
            },
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
            timeout=30
        )
        cobalt_data = cobalt_resp.json()
        audio_url = cobalt_data.get('url') or cobalt_data.get('tunnel')
        if not audio_url:
            return jsonify({'error': 'Could not get audio URL'}), 500

        audio_resp = requests.get(audio_url, timeout=60)

        return Response(audio_resp.content, mimetype='audio/mpeg', headers={
            'Content-Disposition': f'attachment; filename="{video_id}.mp3"',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'QuantX All-in-One'})


# ─── Start polling at module level (works with gunicorn) ─────────
if TELEGRAM_TOKEN:
    t = threading.Thread(target=poll_bot, daemon=True)
    t.start()
    log.info('Bot polling thread started')
else:
    log.warning('TELEGRAM_BOT_TOKEN not set — bot polling disabled')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
