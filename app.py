import asyncio
import time
import httpx
import json
import hashlib
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
RELEASEVERSION = "OB53"  # OB53 ভার্সনে আপডেট করা হলো
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
SUPPORTED_REGIONS = {"BD"}  # শুধুমাত্র BD সার্ভার সচল রাখা হলো

# === Guest Accounts Pool ===
# আপনার লিস্ট থেকে ৫টি অ্যাকাউন্ট পুলে রাখা হলো। কোনো একটি ব্লক হলে স্বয়ংক্রিয়ভাবে অন্যটি কাজ করবে।
# আপনি চাইলে নিচে একইভাবে আরও অ্যাকাউন্ট যোগ করতে পারেন।
ACCOUNTS_POOL = [
    {"uid": "4437047528", "password": "sz_40NE1_BY_SPIDEERIO_GAMING_ZRU88"},
    {"uid": "4437031693", "password": "sz_0GQAH_BY_SPIDEERIO_GAMING_NUJLK"},
    {"uid": "4437040489", "password": "sz_11ZPW_BY_SPIDEERIO_GAMING_TY3NY"},
    {"uid": "4437038239", "password": "sz_PS6EH_BY_SPIDEERIO_GAMING_A6D62"},
    {"uid": "4437047003", "password": "sz_VK7KN_BY_SPIDEERIO_GAMING_LGGXC"}
]

# === Flask App Setup ===

app = Flask(__name__)
CORS(app)
cache = TTLCache(maxsize=100, ttl=300)
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

# === Token Generation ===

async def get_access_token(account: str):
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = account + "&response_type=token&client_type=2&client_secret=2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3&client_id=100067"
    headers = {'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip", 'Content-Type': "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as client:
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
    
    # পুলের অ্যাকাউন্টগুলো একে একে চেষ্টা করবে
    for acc in ACCOUNTS_POOL:
        uid = acc["uid"]
        raw_pass = acc["password"]
        try:
            # পাইথন স্বয়ংক্রিয়ভাবে পাসওয়ার্ড হ্যাশ (SHA-256 Uppercase) তৈরি করে নিচ্ছে
            pass_hash = hashlib.sha256(raw_pass.encode('utf-8')).hexdigest().upper()
            account_str = f"uid={uid}&password={pass_hash}"
            
            token_val, open_id = await get_access_token(account_str)
            
            body = json.dumps({"open_id": open_id, "open_id_type": "4", "login_token": token_val, "orign_platform_type": "4"})
            proto_bytes = await json_to_proto(body, FreeFire_pb2.LoginReq())
            payload = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, proto_bytes)
            url = "https://loginbp.ggblueshark.com/MajorLogin"
            headers = {'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip",
                       'Content-Type': "application/octet-stream", 'Expect': "100-continue", 'X-Unity-Version': "2018.4.11f1",
                       'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION}
            
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, data=payload, headers=headers)
                if resp.status_code != 200:
                    raise Exception(f"MajorLogin HTTP error {resp.status_code}")
                
                decoded = decode_protobuf(resp.content, FreeFire_pb2.LoginRes)
                msg = json.loads(json_format.MessageToJson(decoded))
                
                # যদি Garena এই আইডি বা IP লক করে কিউ-তে ফেলে দেয়
                if 'queueInfo' in msg:
                    raise Exception(f"Garena Login Queue active for UID {uid}. needWaitSecs: {msg['queueInfo'].get('needWaitSecs')}")
                    
                if 'token' not in msg or msg.get('token') == '0':
                    raise Exception("MajorLogin returned empty session token")
                    
                cached_tokens[region] = {
                    'token': f"Bearer {msg.get('token','0')}",
                    'region': msg.get('lockRegion','0'),
                    'server_url': msg.get('serverUrl','0'),
                    'expires_at': time.time() + 25200
                }
                print(f"Successfully authenticated with Garena using UID: {uid}")
                return  # সফলভাবে লগইন হলে ফাংশন থেকে বের হয়ে যাবে
                
        except Exception as e:
            print(f"Failed login attempt with UID {uid}. Error: {e}")
            last_error = e
            continue  # পরবর্তী অ্যাকাউন্টে চলে যাবে
            
    # যদি পুলে থাকা সব অ্যাকাউন্টই ব্যর্থ হয়
    raise Exception(f"All fallback accounts in pool failed to log in. Last Error: {last_error}")

async def initialize_tokens():
    tasks = [create_jwt(r) for r in SUPPORTED_REGIONS]
    await asyncio.gather(*tasks)

async def refresh_tokens_periodically():
    while True:
        await asyncio.sleep(25200)
        await initialize_tokens()

async def get_token_info(region: str) -> Tuple[str,str,str]:
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
    headers = {'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip",
               'Content-Type': "application/octet-stream", 'Expect': "100-continue",
               'Authorization': token, 'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1",
               'ReleaseVersion': RELEASEVERSION}
    async with httpx.AsyncClient() as client:
        resp = await client.post(server+endpoint, data_enc, headers=headers)
        return json.loads(json_format.MessageToJson(decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)))

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

@app.route('/player-info')
@cached_endpoint()
def get_account_info():
    uid = request.args.get('uid')
    if not uid:
        return jsonify({"error": "Please provide UID."}), 400

    try:
        return_data = asyncio.run(GetAccountInformation(uid, "7", "BD", "/GetPlayerPersonalShow"))
        formatted_json = json.dumps(return_data, indent=2, ensure_ascii=False)
        return formatted_json, 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        print(f"BD Server Error Log: {e}")
        return jsonify({"error": f"BD Server Error: {e}"}), 500

@app.route('/refresh', methods=['GET','POST'])
def refresh_tokens_endpoint():
    try:
        asyncio.run(initialize_tokens())
        return jsonify({'message':'Tokens refreshed for BD region.'}),200
    except Exception as e:
        return jsonify({'error': f'Refresh failed: {e}'}),500

# === Startup ===

async def startup():
    await initialize_tokens()
    asyncio.create_task(refresh_tokens_periodically())

if __name__ == '__main__':
    asyncio.run(startup())
    app.run(host='0.0.0.0', port=5000, debug=True)