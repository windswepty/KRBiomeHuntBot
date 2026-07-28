from flask import Flask
from threading import Thread
import logging

app = Flask(__name__)

# Suppress flask default logs to keep console clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app.route('/')
def home():
    return "KRBiomeHuntBot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
