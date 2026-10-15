"""
ERP Desktop Launcher
Launches the Flask app in a PyWebView window (desktop app).
"""
import webview
import threading
import time
import sys
import os

# Add project directory to path
sys.path.insert(0, os.path.dirname(__file__))

from app import app, init_db

def start_flask():
    """Run Flask in background thread."""
    init_db()
    app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False, threaded=True)

def main():
    # Start Flask server in background
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Wait for server to be ready
    time.sleep(1.5)

    # Create desktop window
    window = webview.create_window(
        title='نظام ERP — إدارة الموارد المؤسسية',
        url='http://127.0.0.1:5050',
        width=1400,
        height=900,
        min_size=(1100, 700),
        resizable=True,
        fullscreen=False,
        background_color='#050a18',
        text_select=False,
    )

    # Start WebView (blocks until window is closed)
    webview.start(debug=False)

if __name__ == '__main__':
    main()
