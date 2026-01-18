from flask import Flask, render_template, request
import re

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    message = None
    message_type = None
    embed_src = None
    if request.method == 'POST':
        telegram_link = request.form.get('telegram_link', '').strip()
        if not telegram_link:
            message = "Please provide a Telegram post link."
            message_type = 'danger'
        else:
            # Basic validation for t.me link format: https://t.me/[channel]/[post_id]
            pattern = r'^https://t\.me/([a-zA-Z0-9_]+)/(\d+)$'
            match = re.match(pattern, telegram_link)
            if match:
                channel = match.group(1)
                post_id = match.group(2)
                embed_src = f"https://t.me/{channel}/{post_id}?embed=1"
                message = "✅ Video ready to stream! (Works best for public channel video posts)"
                message_type = 'success'
            else:
                message = "Invalid Telegram link format. Use: https://t.me/channelname/123"
                message_type = 'danger'
    return render_template('index.html',
        message=message,
        message_type=message_type,
        embed_src=embed_src
    )

if __name__ == '__main__':
    app.run(debug=True)
