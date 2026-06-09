import asyncio
import time
import httpx
import json
import hashlib
import random
import os
from collections import defaultdict
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from cachetools import TTLCache
from typing import Tuple
from proto import FreeFire_pb2, main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format, message
from google.protobuf.message import Message
from Crypto.Cipher import AES
import base64

# === Settings ===
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
RELEASEVERSION = "OB53"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
SUPPORTED_REGIONS = {"BD"}

# === Load Accounts from JSON ===
def load_accounts():
    """accounts.json থেকে সব activated BD accounts লোড করে"""
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accounts.json')
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            all_accounts = json.load(f)
        # শুধু BD region এর activated accounts ফিল্টার করি
        bd_accounts = [
            acc for acc in all_accounts
            if acc.get('region') == 'BD' and acc.get('status') == 'activated'
        ]
        print(f"✅ Loaded {len(bd_accounts)} BD activated accounts from accounts.json")
        return bd_accounts
    except FileNotFoundError:
        print("❌ accounts.json not found!")
        return []
    except Exception as e:
        print(f"❌ Error loading accounts: {e}")
        return []

ACCOUNTS_POOL = load_accounts()

# Track failed accounts to avoid retrying them immediately
failed_accounts = set()
last_failed_reset = time.time()

def get_random_accounts(count=10):
    """Pool থেকে র‍্যান্ডমলি accounts বাছাই করে, failed ones skip করে"""
    global failed_accounts, last_failed_reset
    
    # প্রতি ৩০ মিনিটে failed list রিসেট করি
    if time.time() - last_failed_reset > 1800:
        failed_accounts.clear()
        last_failed_reset = time.time()
    
    available = [acc for acc in ACCOUNTS_POOL if acc['uid'] not in failed_accounts]
    
    # যদি সব failed হয়ে যায়, তাহলে আবার সব try করি
    if len(available) < count:
        failed_accounts.clear()
        available = ACCOUNTS_POOL.copy()
    
    return random.sample(available, min(count, len(available)))

# === Flask App Setup ===
app = Flask(__name__)
CORS(app)
cache = TTLCache(maxsize=200, ttl=300)
cached_tokens = defaultdict(dict)

# === Helper Functions ===
def pad(text: bytes) -> bytes:
    padding_length = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([padding_length] * padding_length)

def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    aes = AES.new(key, AES.MODE_CBC, iv)
    return aes.encrypt(pad(plaintext))

def decode_protobuf(encoded_data: bytes, message_type: message.Message) -> message.Message:
    instance = message_type()
    instance.ParseFromString(encoded_data)
    return instance

async def json_to_proto(json_data: str, proto_message: Message) -> bytes:
    json_format.ParseDict(json.loads(json_data), proto_message)
    return proto_message.SerializeToString()

async def get_access_token(account: str):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = account + "&response_type=token&client_type=2&client_secret=2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3&client_id=100067"
    headers = {
        'User-Agent': USERAGENT,
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Content-Type': "application/x-www-form-urlencoded"
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, data=payload, headers=headers)
        if resp.status_code != 200:
            raise Exception(f"Garena OAuth API returned status code {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            raise Exception("Garena OAuth did not return JSON response")

        if "access_token" not in data:
            raise Exception(f"OAuth error: {data.get('error', 'unknown_error')}")

        return data.get("access_token"), data.get("open_id", "0")

async def create_jwt(region: str):
    last_error = None

    # পুল থেকে র‍্যান্ডমলি ১৫টা account নিয়ে try করবে
    accounts_to_try = get_random_accounts(15)
    
    for acc in accounts_to_try:
        uid = acc["uid"]
        raw_pass = acc["password"]
        try:
            # SHA-256 hash
            pass_hash = hashlib.sha256(raw_pass.encode('utf-8')).hexdigest().upper()
            account_str = f"uid={uid}&password={pass_hash}"

            token_val, open_id = await get_access_token(account_str)

            body = json.dumps({
                "open_id": open_id,
                "open_id_type": "4",
                "login_token": token_val,
                "orign_platform_type": "4"
            })
            proto_bytes = await json_to_proto(body, FreeFire_pb2.LoginReq())
            payload = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)
            url = "https://loginbp.ggblueshark.com/MajorLogin"
            headers = {
                'User-Agent': USERAGENT,
                'Connection': "Keep-Alive",
                'Accept-Encoding': "gzip",
                'Content-Type': "application/octet-stream",
                'Expect': "100-continue",
                'X-Unity-Version': "2018.4.11f1",
                'X-GA': "v1 1",
                'ReleaseVersion': RELEASEVERSION
            }

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, data=payload, headers=headers)
                if resp.status_code != 200:
                    raise Exception(f"MajorLogin HTTP error {resp.status_code}")

                decoded = decode_protobuf(resp.content, FreeFire_pb2.LoginRes)
                msg = json.loads(json_format.MessageToJson(decoded))

                # Queue check
                if 'queueInfo' in msg:
                    raise Exception(f"Login Queue active for UID {uid}. needWaitSecs: {msg['queueInfo'].get('needWaitSecs')}")

                if 'token' not in msg or msg.get('token') == '0':
                    raise Exception("MajorLogin returned empty session token")

                cached_tokens[region] = {
                    'token': f"Bearer {msg.get('token','0')}",
                    'region': msg.get('lockRegion','0'),
                    'server_url': msg.get('serverUrl','0'),
                    'expires_at': time.time() + 25200  # 7 hours
                }
                print(f"✅ Successfully authenticated with UID: {uid} for region: {region}")
                return

        except Exception as e:
            print(f"❌ Failed login with UID {uid}: {e}")
            failed_accounts.add(uid)
            last_error = e
            continue

    raise Exception(f"All {len(accounts_to_try)} tried accounts failed. Last Error: {last_error}")

async def initialize_tokens():
    tasks = [create_jwt(r) for r in SUPPORTED_REGIONS]
    await asyncio.gather(*tasks)

async def refresh_tokens_periodically():
    while True:
        await asyncio.sleep(25200)
        await initialize_tokens()

async def get_token_info(region: str) -> Tuple[str, str, str]:
    info = cached_tokens.get(region)
    if info and time.time() < info['expires_at']:
        return info['token'], info['region'], info['server_url']
    await create_jwt(region)
    info = cached_tokens[region]
    return info['token'], info['region'], info['server_url']

async def GetAccountInformation(uid, unk, region, endpoint):
    payload = await json_to_proto(json.dumps({'a': uid, 'b': unk}), main_pb2.GetPlayerPersonalShow())
    data_enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, payload)
    token, lock, server = await get_token_info(region)
    headers = {
        'User-Agent': USERAGENT,
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Content-Type': "application/octet-stream",
        'Expect': "100-continue",
        'Authorization': token,
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': RELEASEVERSION
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(server + endpoint, data=data_enc, headers=headers)
        return json.loads(json_format.MessageToJson(
            decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
        ))

# === Caching Decorator ===
def cached_endpoint(ttl=300):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            key = (request.path, tuple(request.args.items()))
            if key in cache:
                return cache[key]
            res = fn(*a, **k)
            cache[key] = res
            return res
        return wrapper
    return decorator

# === Flask Routes ===
@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "server": "BD",
        "version": RELEASEVERSION,
        "accounts_loaded": len(ACCOUNTS_POOL),
        "usage": "/player-info?uid=YOUR_UID",
        "example": "/player-info?uid=338277714"
    })

@app.route('/player-info')
@cached_endpoint()
def get_account_info():
    uid = request.args.get('uid')
    if not uid:
        return jsonify({"error": "Please provide UID. Example: /player-info?uid=338277714"}), 400

    # ১. প্রথমে accounts.json-এ লোকাল সার্চ
    local_account = next((acc for acc in ACCOUNTS_POOL if acc.get('uid') == str(uid)), None)
    if local_account:
        print(f"🎯 Local DB Match Found for UID {uid}")
        return jsonify({
            "uid": uid,
            "nickname": local_account.get("name", "Unknown"),
            "region": "BD",
            "source": "Local DB Cache"
        }), 200, {'Content-Type': 'application/json; charset=utf-8'}

    # ২. যদি লোকাল ফাইলে না থাকে, তবে অনলাইন পাবলিক API-তে চেষ্টা করা হবে
    print(f"🔍 Local DB Miss. Searching online for UID {uid}")
    
    # আমরা Prince-LKTeam এবং অন্যান্য রানিং পাবলিক APIs এর একটি লিস্ট ট্রাই করব
    fallback_apis = [
        f"https://freefireinfo-zy9l.onrender.com/api/v1/player-profile?uid={uid}&server=BD",
        f"https://freefire-api-six.vercel.app/player-info?uid={uid}&region=BD" # Just in case it gets open
    ]
    
    for api_url in fallback_apis:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(api_url)
                if resp.status_code == 200:
                    data = resp.json()
                    
                    # API 1 payload checking (Prince-LK)
                    if 'basicInfo' in data and 'nickname' in data['basicInfo']:
                        return jsonify({
                            "uid": uid,
                            "nickname": data['basicInfo']['nickname'],
                            "region": "BD",
                            "source": "Public API Fallback"
                        }), 200, {'Content-Type': 'application/json; charset=utf-8'}
                        
                    # API 2 payload checking (Generic)
                    elif 'nickname' in data:
                        return jsonify({
                            "uid": uid,
                            "nickname": data['nickname'],
                            "region": "BD",
                            "source": "Public API Fallback"
                        }), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception as api_err:
            print(f"⚠️ Fallback API Failed: {api_err}")
            continue

    # ৩. যদি কিছুই কাজ না করে, গ্যারিনা এপিআই-তে চেষ্টা করব (যদি কোনো অ্যাকাউন্ট লাকিলি লগইন হতে পারে)
    try:
        return_data = asyncio.run(GetAccountInformation(uid, "7", "BD", "/GetPlayerPersonalShow"))
        if 'basicInfo' in return_data and 'nickname' in return_data['basicInfo']:
            return jsonify({
                "uid": uid,
                "nickname": return_data['basicInfo']['nickname'],
                "region": "BD",
                "source": "Garena Live Server"
            }), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        print(f"❌ Live Garena Check Failed: {e}")

    # ৪. সব ব্যর্থ হলে ক্লিয়ার এরর রিটার্ন
    return jsonify({
        "error": "Player nickname not found in Local DB, and Garena API is currently offline. Please try another UID present in your success list."
    }), 404, {'Content-Type': 'application/json; charset=utf-8'}

@app.route('/refresh', methods=['GET', 'POST'])
def refresh_tokens_endpoint():
    try:
        cached_tokens.pop("BD", None)
        failed_accounts.clear()
        asyncio.run(initialize_tokens())
        return jsonify({
            'message': 'Tokens refreshed for BD region.',
            'accounts_available': len(ACCOUNTS_POOL)
        }), 200
    except Exception as e:
        return jsonify({'error': f'Refresh failed: {e}'}), 500

@app.route('/status')
def status():
    bd_info = cached_tokens.get("BD", {})
    return jsonify({
        "server": "BD",
        "version": RELEASEVERSION,
        "total_accounts": len(ACCOUNTS_POOL),
        "failed_accounts": len(failed_accounts),
        "token_active": bool(bd_info and time.time() < bd_info.get('expires_at', 0)),
        "token_expires_in": max(0, int(bd_info.get('expires_at', 0) - time.time())) if bd_info else 0
    })

# === Startup ===
async def startup():
    await initialize_tokens()
    asyncio.create_task(refresh_tokens_periodically())

if __name__ == '__main__':
    asyncio.run(startup())
    app.run(host='0.0.0.0', port=5000, debug=True)