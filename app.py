import os
import json
import time
import hmac
import hashlib
import base64
import httpx
from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps

app = Flask(__name__)
CORS(app) # CORS সমস্যা সমাধান করার জন্য এনাবেল করলাম


# Load Local Accounts DB (ক্যাশ ডাটাবেজ)
ACCOUNTS_POOL = []
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), 'accounts.json')
try:
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            ACCOUNTS_POOL = json.load(f)
        print(f"✅ Loaded {len(ACCOUNTS_POOL)} accounts from accounts.json")
except Exception as e:
    print(f"❌ Failed to load accounts.json: {e}")

# Simple caching system
cache = {}
def cached_endpoint(timeout_seconds=300):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            uid = request.args.get('uid')
            if not uid:
                return f(*args, **kwargs)
            
            now = time.time()
            if uid in cache:
                cache_time, cached_response, status_code = cache[uid]
                if now - cache_time < timeout_seconds:
                    print(f"⚡ Serving from RAM Cache for UID {uid}")
                    return cached_response, status_code, {'Content-Type': 'application/json; charset=utf-8'}
            
            resp = f(*args, **kwargs)
            if isinstance(resp, tuple) and len(resp) >= 2:
                response_data, status_code = resp[0], resp[1]
            else:
                response_data, status_code = resp, 200
                
            if status_code == 200:
                cache[uid] = (now, response_data, status_code)
                
            return resp
        return decorated_function
    return decorator

# Gameskinbo dynamic token generation
def generate_gameskinbo_token(uid: str) -> str:
    secret = b"GAMESKINBOFFIDCHECKERSECURITYPROTOCOL"
    timestamp = int(time.time() * 1000)
    time_window = timestamp // 30000
    
    h1 = hmac.new(secret, str(time_window).encode('utf-8'), hashlib.sha256)
    hmac_key = h1.hexdigest()[:32].encode('utf-8')
    
    message = f"{uid}|{timestamp}".encode('utf-8')
    h2 = hmac.new(hmac_key, message, hashlib.sha256)
    hmac_sig = h2.hexdigest()
    
    token_str = f"{uid}|{timestamp}|{hmac_sig}"
    token = base64.b64encode(token_str.encode('utf-8')).decode('utf-8')
    return token

@app.route('/')
def home():
    try:
        with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'r', encoding='utf-8') as f:
            return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
    except Exception as e:
        return f"Error loading frontend: {e}", 500

@app.route('/player-info')
@cached_endpoint()
def get_account_info():
    uid = request.args.get('uid')
    if not uid:
        return jsonify({"error": "Please provide UID. Example: /player-info?uid=338277714"}), 400

    # ১. প্রথমে accounts.json-এ লোকাল সার্চ (ইনস্ট্যান্ট রেসপন্স)
    local_account = next((acc for acc in ACCOUNTS_POOL if acc.get('uid') == str(uid)), None)
    if local_account:
        print(f"🎯 Local DB Match Found for UID {uid}")
        return jsonify({
            "uid": uid,
            "nickname": local_account.get("name", "Unknown"),
            "region": local_account.get("region", "BD"),
            "level": 1,
            "likes": "N/A",
            "guild_name": "N/A",
            "release_version": "OB53"
        }), 200, {'Content-Type': 'application/json; charset=utf-8'}

    # ২. গেমস্কিনবো ডায়নামিক এপিআই লাইভ সার্চ (১০০% সচল ও ইউনিভার্সাল)
    print(f"🔍 Local DB Miss. Querying Gameskinbo API for UID {uid}")
    try:
        token = generate_gameskinbo_token(str(uid))
        url = f"https://gameskinbo.com/api/ff_id_checker?uid={uid}&token={token}"
        headers = {
            "x-api-client": "gameskinbo-web",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://gameskinbo.com/free_fire_id_checker",
            "Origin": "https://gameskinbo.com"
        }
        with httpx.Client(timeout=10.0, verify=False) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                name = data.get("name")
                release_version = "OB53"
                
                if not name and "raw_data" in data:
                    try:
                        raw = json.loads(data["raw_data"])
                        name = raw.get("AccountInfo", {}).get("AccountName")
                        release_version = raw.get("ReleaseVersion", "OB53")
                    except:
                        pass
                
                if data.get("release_version"):
                    release_version = data.get("release_version")
                
                if name:
                    print(f"🎉 Success! Gameskinbo resolved nickname: {name}")
                    return jsonify({
                        "uid": uid,
                        "nickname": name,
                        "region": data.get("region", "BD"),
                        "level": data.get("level", 1),
                        "likes": data.get("likes", "N/A"),
                        "guild_name": data.get("guild_name", "N/A"),
                        "release_version": release_version
                    }), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as skinbo_err:
        print(f"⚠️ Gameskinbo API Error: {skinbo_err}")

    # ৩. যদি গেমস্কিনবো ফেইল করে, গ্যারিনার অফিশিয়াল শপ এপিআই ট্রাই করা হবে
    print(f"🔍 Querying Garena shop fallback validation gateway for UID {uid}")
    gateways = [
        {
            "url": "https://sg.garena.moe/api/auth/player",
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://sg.garena.moe/app",
                "Origin": "https://sg.garena.moe"
            },
            "payload": {
                "app_id": 100067,
                "login_channel": 1,
                "player_id": str(uid)
            }
        }
    ]

    for gw in gateways:
        try:
            with httpx.Client(timeout=10.0, verify=False) as client:
                resp = client.post(gw["url"], json=gw["payload"], headers=gw["headers"])
                if resp.status_code == 200:
                    data = resp.json()
                    if "nickname" in data:
                        print(f"🎉 Success! Garena validation resolved nickname: {data.get('nickname')}")
                        return jsonify({
                            "uid": uid,
                            "nickname": data.get("nickname"),
                            "region": "BD",
                            "level": 1,
                            "likes": "N/A",
                            "guild_name": "N/A",
                            "release_version": "OB53"
                        }), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception as e:
            print(f"⚠️ Gateway validation request failed: {e}")
            continue

    # ৪. সব ব্যর্থ হলে এরর
    return jsonify({
        "error": "Player nickname not found. Either the UID is invalid, or the Garena verification servers are currently busy. Please try again later."
    }), 404, {'Content-Type': 'application/json; charset=utf-8'}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)