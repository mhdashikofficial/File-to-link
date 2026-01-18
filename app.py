from flask import Flask, render_template, request, send_from_directory
import re
import os
import uuid
import subprocess
import logging
import shutil
import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, SessionPasswordNeeded

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUTPUT_FOLDER = '/tmp/streams'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

def parse_telegram_link(link):
    pattern = r'^https://t\.me/([a-zA-Z0-9_]+)/(\d+)$'
    match = re.match(pattern, link)
    if match:
        channel = match.group(1)
        post_id = int(match.group(2))
        from_chat_id = f"@{channel}" if not channel.startswith('-100') else channel
        return from_chat_id, post_id
    return None, None

async def download_large_file(api_id, api_hash, session_string, from_chat_id, message_id, temp_path):
    """Download file progressively with Pyrogram"""
    app = Client("temp_session", api_id=api_id, api_hash=api_hash, session_string=session_string, in_memory=True)
    try:
        await app.start()
        msg: Message = await app.get_messages(from_chat_id, message_id)
        if not (msg.video or msg.document):
            raise ValueError("No video/document in message")
        
        file_size = msg.video.file_size if msg.video else msg.document.file_size
        logger.info(f"Downloading {file_size} bytes from {from_chat_id}/{message_id}")
        
        # Download to file with progress
        def progress(current, total):
            percent = (current / total) * 100 if total else 0
            logger.info(f"Download progress: {current}/{total} ({percent:.1f}%)")
        
        await app.download_media(msg, file_name=temp_path, progress=progress)
        logger.info("Download complete")
        return True
    except FloodWait as e:
        logger.error(f"Flood wait: {e.value}s")
        raise
    except Exception as e:
        logger.error(f"Download error: {e}")
        raise
    finally:
        await app.stop()

def convert_to_hls(input_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    hls_playlist = os.path.join(output_dir, 'playlist.m3u8')
    
    hls_cmd = [
        'ffmpeg', '-i', input_path,
        '-profile:v', 'baseline', '-level', '3.0', '-start_number', '0',
        '-hls_time', '10', '-hls_list_size', '0', '-f', 'hls', hls_playlist,
        '-y'  # Overwrite
    ]
    
    try:
        result = subprocess.run(hls_cmd, check=True, capture_output=True, timeout=900)  # 15 min for conversion
        logger.info("HLS conversion complete")
        return True
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timeout")
        return False
    except Exception as e:
        logger.error(f"FFmpeg error: {e}")
        return False

def cleanup_temp(video_id, keep_hls=True):
    temp_file = os.path.join(OUTPUT_FOLDER, f"{video_id}.mkv")
    if os.path.exists(temp_file):
        os.remove(temp_file)
    if not keep_hls:
        hls_dir = os.path.join(OUTPUT_FOLDER, video_id, 'hls')
        if os.path.exists(hls_dir):
            shutil.rmtree(hls_dir)

@app.route('/stream/<video_id>/<format_type>/<path:filename>')
def stream_file(video_id, format_type, filename):
    directory = os.path.join(app.config['OUTPUT_FOLDER'], video_id, format_type)
    if not os.path.exists(directory):
        return "Not ready yet - refresh", 404
    return send_from_directory(directory, filename)

@app.route('/', methods=['GET', 'POST'])
def index():
    message = None
    message_type = None
    hls_url = None
    api_error = None
    progress = None
    progress_percent = 0
    if request.method == 'POST':
        api_id = int(request.form.get('api_id', 0))
        api_hash = request.form.get('api_hash', '').strip()
        session_string = request.form.get('session_string', '').strip()
        chat_id = request.form.get('chat_id', '').strip()
        telegram_link = request.form.get('telegram_link', '').strip()
        
        if not all([api_id, api_hash, session_string, telegram_link]):
            message = "All Pyrogram creds and link required."
            message_type = 'danger'
        else:
            from_chat_id, message_id = parse_telegram_link(telegram_link)
            if not from_chat_id:
                message = "Invalid link format."
                message_type = 'danger'
            else:
                target_chat_id = chat_id or from_chat_id
                video_id = str(uuid.uuid4())
                temp_path = os.path.join(OUTPUT_FOLDER, f"{video_id}.mkv")
                hls_dir = os.path.join(OUTPUT_FOLDER, video_id, 'hls')
                
                try:
                    # Download async
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    success = loop.run_until_complete(download_large_file(api_id, api_hash, session_string, from_chat_id, message_id, temp_path))
                    loop.close()
                    
                    if not success:
                        raise ValueError("Download failed")
                    
                    # Convert to HLS
                    if convert_to_hls(temp_path, hls_dir):
                        hls_url = f"/stream/{video_id}/hls/playlist.m3u8"
                        message = f"✅ {video_id} streamed! (Cleanup in 1hr)"
                        message_type = 'success'
                        cleanup_temp(video_id)  # Remove raw file, keep HLS
                    else:
                        raise ValueError("HLS failed")
                        
                except Exception as e:
                    message = "Error during download/conversion."
                    message_type = 'danger'
                    api_error = str(e)
                    cleanup_temp(video_id, keep_hls=False)
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
    
    # For progress, you'd need a task ID/session storage (e.g., Redis); here, simple log-based
    return render_template('index.html',
        message=message, message_type=message_type, hls_url=hls_url,
        api_error=api_error, progress=progress, progress_percent=progress_percent
    )

if __name__ == '__main__':
    app.run(debug=True)
