from flask import Flask, render_template, request, send_from_directory
import re
import requests
import json
import os
import uuid
import subprocess
import logging
import shutil  # Added for cleanup

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration - FIXED: Use /tmp for writable FS in serverless
OUTPUT_FOLDER = '/tmp/streams'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

def allowed_chat(chat_id):
    # Basic validation; expand as needed
    return chat_id.startswith('@') or chat_id.startswith('-')

def parse_telegram_link(link):
    pattern = r'^https://t\.me/([a-zA-Z0-9_]+)/(\d+)$'
    match = re.match(pattern, link)
    if match:
        channel = match.group(1)
        post_id = int(match.group(2))
        from_chat_id = f"@{channel}" if not channel.startswith('-100') else channel  # Handle username or ID
        return from_chat_id, post_id
    return None, None

def get_file_id_from_forward(bot_token, from_chat_id, message_id, chat_id):
    url = f"https://api.telegram.org/bot{bot_token}/forwardMessage"
    payload = {
        "chat_id": chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id
    }
    r = requests.post(url, data=payload, timeout=30)
    res = r.json()
    logger.info(f"ForwardMessage response: {json.dumps(res, indent=2)}")  # Log full response
    if res.get("ok"):
        forwarded_msg = res['result']
        if 'video' in forwarded_msg:
            logger.info("Found video file_id")
            return forwarded_msg['video']['file_id']
        elif 'document' in forwarded_msg:
            logger.info("Found document file_id (e.g., MKV)")
            return forwarded_msg['document']['file_id']
        else:
            logger.warning(f"No video/document in forwarded message: {forwarded_msg.keys()}")
            return None
    else:
        logger.error(f"ForwardMessage failed: {res}")
        return None

def get_direct_url(bot_token, file_id):
    # Get file path
    url = f"https://api.telegram.org/bot{bot_token}/getFile"
    payload = {"file_id": file_id}
    r = requests.post(url, data=payload, timeout=30)
    res = r.json()
    logger.info(f"GetFile response: {json.dumps(res, indent=2)}")  # Log full response for debugging
    if res.get("ok"):
        file_path = res['result']['file_path']
        direct_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        logger.info(f"Direct URL generated: {direct_url}")
        return direct_url
    else:
        logger.error(f"GetFile failed: {res}")
        return None

def convert_to_hls(input_url, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    hls_playlist = os.path.join(output_dir, 'playlist.m3u8')
    
    hls_cmd = [
        'ffmpeg', '-i', input_url,
        '-profile:v', 'baseline', '-level', '3.0',
        '-start_number', '0',
        '-hls_time', '10',
        '-hls_list_size', '0',
        '-f', 'hls', hls_playlist
    ]
    
    try:
        result = subprocess.run(hls_cmd, check=True, capture_output=True, timeout=300)  # Added timeout for large files
        logger.info(f"HLS conversion completed for {input_url}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"HLS conversion failed: {e}")
        return False
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timeout - file too large?")
        return False

def cleanup_old_streams():
    """Optional: Clean up old streams in /tmp to free space"""
    if os.path.exists(OUTPUT_FOLDER):
        for item in os.listdir(OUTPUT_FOLDER):
            item_path = os.path.join(OUTPUT_FOLDER, item)
            if os.path.isdir(item_path):
                shutil.rmtree(item_path, ignore_errors=True)

@app.route('/stream/<video_id>/<format_type>/<path:filename>')
def stream_file(video_id, format_type, filename):
    directory = os.path.join(app.config['OUTPUT_FOLDER'], video_id, format_type)
    if not os.path.exists(directory):
        return "File not found", 404
    return send_from_directory(directory, filename)

@app.route('/', methods=['GET', 'POST'])
def index():
    message = None
    message_type = None
    hls_url = None
    api_error = None  # New: To show detailed API errors
    if request.method == 'POST':
        bot_token = request.form.get('bot_token', '').strip()
        chat_id = request.form.get('chat_id', '').strip()
        telegram_link = request.form.get('telegram_link', '').strip()
        
        if not bot_token or not telegram_link:
            message = "Bot token and Telegram link are required."
            message_type = 'danger'
        elif not allowed_chat(chat_id or ''):
            message = "Invalid chat ID format."
            message_type = 'danger'
        else:
            from_chat_id, message_id = parse_telegram_link(telegram_link)
            if not from_chat_id:
                message = "Invalid Telegram link format. Use: https://t.me/channelname/123"
                message_type = 'danger'
            else:
                # Use provided chat_id or parse from link
                target_chat_id = chat_id or from_chat_id
                try:
                    # Optional cleanup on each request to manage /tmp space
                    cleanup_old_streams()
                    
                    file_id = get_file_id_from_forward(bot_token, from_chat_id, message_id, target_chat_id)
                    if not file_id:
                        message = "Could not retrieve file. Ensure bot is admin in channel and post contains video/document."
                        message_type = 'danger'
                        api_error = "Check logs for ForwardMessage response. Post may lack media."
                    else:
                        direct_url = get_direct_url(bot_token, file_id)
                        if not direct_url:
                            message = "Could not get direct file URL."
                            message_type = 'danger'
                            api_error = "Check logs for GetFile response. Invalid file_id or permissions?"
                        else:
                            video_id = str(uuid.uuid4())
                            hls_dir = os.path.join(app.config['OUTPUT_FOLDER'], video_id, 'hls')
                            if convert_to_hls(direct_url, hls_dir):
                                hls_url = f"/stream/{video_id}/hls/playlist.m3u8"
                                message = "✅ Video converted and ready to stream! (MKV and others now playable via HLS)"
                                message_type = 'success'
                            else:
                                message = "HLS conversion failed. Ensure FFmpeg is installed and accessible."
                                message_type = 'danger'
                except Exception as e:
                    message = f"Error: {str(e)}"
                    message_type = 'danger'
                    api_error = str(e)
    return render_template('index.html',
        message=message,
        message_type=message_type,
        hls_url=hls_url,
        api_error=api_error  # Pass to template for display
    )

if __name__ == '__main__':
    app.run(debug=True)
