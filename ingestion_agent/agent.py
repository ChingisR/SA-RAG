"""
Ingestion Agent v3.0 — Multimodal SMB Document Watcher
─────────────────────────────────────────────────────────
Changes from v2.0:
  - Multi-format support: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV
  - SHA-256 content-hash cache (immune to renames, prevents duplicates)
  - JWT token auto-refresh (re-authenticates before expiry on long runs)
"""

import os
import io
import time
import json
import base64
import hashlib
import requests
import smbclient
import smbclient.path
from datetime import datetime, timezone
from dotenv import load_dotenv
import threading
import concurrent.futures

# Load environment variables
load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# Watchdog
# ──────────────────────────────────────────────────────────────────────────────
def watchdog_loop():
    """
    Background thread that monitors the application's heartbeat.
    
    If the `heartbeat.txt` file's modified time is older than 15 minutes, 
    it assumes the main thread or ThreadPoolExecutor has deadlocked 
    (e.g., due to hanging SMB connections) and force-kills the process `os._exit(1)`.
    Docker or the orchestrator will then restart the container automatically.
    """
    while True:
        time.sleep(60)
        try:
            if os.path.exists("heartbeat.txt"):
                mtime = os.path.getmtime("heartbeat.txt")
                if time.time() - mtime > 900:  # 15 minutes
                    print("🚨 WATCHDOG: Heartbeat is older than 15 minutes. Process is stuck. Restarting...")
                    os._exit(1)
        except Exception as e:
            print(f"⚠️ Watchdog error: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Local Configuration
# ──────────────────────────────────────────────────────────────────────────────
LOCAL_DATA_PATH = os.getenv("LOCAL_DATA_PATH", "/app/smb_copy")
API_URL         = os.getenv("API_URL", "http://fastapi-app:8000/api/v1")
API_USER        = os.getenv("API_USER", "admin@enterprise.com")
API_PASSWORD    = os.getenv("API_PASSWORD", "changeme")
SYNC_INTERVAL   = int(os.getenv("SYNC_INTERVAL", "60"))

# Supported file extensions
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv", ".jpg", ".jpeg", ".png", ".bmp"}


CACHE_FILE       = "processed_files.json"
DEAD_LETTER_FILE = "dead_letter.json"
MAX_UPLOAD_RETRIES = 3

# ──────────────────────────────────────────────────────────────────────────────
# Cache helpers (keyed by SHA-256 content hash)
# ──────────────────────────────────────────────────────────────────────────────
def load_cache() -> dict:
    """
    Load the local processed files cache (JSON).
    
    The cache stores mappings of `absolute_file_path -> sha256_hash`.
    This prevents re-uploading documents that haven't changed.
    
    Note: Contains migration logic to convert older caches that used `basename`
    instead of `absolute_file_path` as keys to support files with the same name
    in different directories.
    """
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            print(f"⚠️ Error reading cache file: {e}")
            return {}

        # Check if keys are already full paths
        sample_keys = list(raw_data.keys())[:10]
        is_already_migrated = all(k.startswith(LOCAL_DATA_PATH) or '/' in k or '\\' in k for k in sample_keys) if sample_keys else True
        if is_already_migrated:
            return raw_data

        print("🔄 Upgrading cache schema: Migrating basename keys to full_paths...")
        migrated_cache = {}
        
        # 1. Walk the directory and map basename -> list of full_paths
        basename_to_paths = {}
        for root, _, files in os.walk(LOCAL_DATA_PATH):
            for file in files:
                if os.path.splitext(file)[1].lower() in SUPPORTED_EXTENSIONS:
                    full_p = os.path.join(root, file)
                    basename_to_paths.setdefault(file, []).append(full_p)
        
        # 2. Map basename cache keys to full path if unique
        migrated_count = 0
        clash_count = 0
        for base_name, file_hash in raw_data.items():
            paths = basename_to_paths.get(base_name, [])
            if len(paths) == 1:
                # Unique basename - safe to migrate!
                migrated_cache[paths[0]] = file_hash
                migrated_count += 1
            elif len(paths) > 1:
                # Clash detected - let them re-verify hash or re-upload to be safe
                clash_count += 1
        
        print(f"✅ Cache migration complete: {migrated_count} keys migrated, {clash_count} clashes resolved safely.")
        # Save migrated cache back to file immediately
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(migrated_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ Could not save migrated cache: {e}")
        return migrated_cache
        
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def content_hash(data: bytes) -> str:
    """Return the SHA-256 hex digest of the given bytes."""
    return hashlib.sha256(data).hexdigest()


def load_dead_letter() -> dict:
    if os.path.exists(DEAD_LETTER_FILE):
        with open(DEAD_LETTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_dead_letter(dl: dict):
    with open(DEAD_LETTER_FILE, "w", encoding="utf-8") as f:
        json.dump(dl, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────────────────────
# JWT Authentication with auto-refresh
# ──────────────────────────────────────────────────────────────────────────────
_token: str | None = None
_token_exp: float = 0.0      # Unix timestamp when the token expires
_token_lock = threading.Lock()


def _decode_exp(token: str) -> float:
    """Decode the exp claim from a JWT without verifying the signature."""
    try:
        import base64 as _b64
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        padding = "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(_b64.urlsafe_b64decode(parts[1] + padding))
        return float(payload.get("exp", 0))
    except Exception:
        return 0.0


def authenticate() -> str | None:
    """Log in to the SA-RAG FastAPI backend and return a JWT token."""
    global _token, _token_exp
    print(f"🔑 Authenticating with {API_URL}/login as {API_USER}...")
    try:
        resp = requests.post(
            f"{API_URL}/login",
            json={"email": API_USER, "password": API_PASSWORD},
            timeout=10,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            print("❌ No token received.")
            return None
        _token = token
        _token_exp = _decode_exp(token)
        print("✅ Authenticated successfully.")
        return token
    except Exception as e:
        print(f"❌ Authentication failed: {e}")
        return None


def get_valid_token() -> str | None:
    """
    Return a valid JWT for FastAPI interactions.
    
    Uses a thread lock to ensure only one thread attempts to refresh the token 
    if it is missing or about to expire within 5 minutes (300 seconds).
    """
    global _token, _token_exp
    with _token_lock:
        now = datetime.now(timezone.utc).timestamp()
        if _token is None or now >= _token_exp - 300:
            return authenticate()
        return _token


# ──────────────────────────────────────────────────────────────────────────────
# Upload
# ──────────────────────────────────────────────────────────────────────────────
def upload_document(file_name: str, file_path: str, file_hash: str = "") -> bool:
    """Upload a document to the FastAPI backend. Re-authenticates if needed."""
    token = get_valid_token()
    if not token:
        print(f"❌ Cannot upload {file_name}: no valid token.")
        return False

    ext = os.path.splitext(file_name)[1].lower()
    mime_types = {
        ".pdf":  "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls":  "application/vnd.ms-excel",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt":  "text/plain",
        ".md":   "text/markdown",
        ".csv":  "text/csv",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
        ".bmp":  "image/bmp",
    }
    mime = mime_types.get(ext, "application/octet-stream")

    print(f"⬆️  Uploading [{ext}] {file_name} ...")
    try:
        headers = {"Authorization": f"Bearer {token}"}
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{API_URL}/upload-pdf?sync=false&file_hash={file_hash}",
                headers=headers,
                files={"file": (file_name, f, mime)},
                timeout=300,
            )
        resp.raise_for_status()
        print(f"✅ Uploaded {file_name}: {resp.json()}")
        return True
    except Exception as e:
        print(f"❌ Upload failed for {file_name}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Details: {e.response.text}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Local Directory Walker
# ──────────────────────────────────────────────────────────────────────────────
def delete_document(file_name: str) -> bool:
    """Delete a document from the FastAPI backend."""
    token = get_valid_token()
    if not token:
        print(f"❌ Cannot delete {file_name}: no valid token.")
        return False

    print(f"🗑️  Deleting removed file {file_name} from backend ...")
    try:
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.delete(
            f"{API_URL}/documents/{file_name}",
            headers=headers,
            timeout=30
        )
        resp.raise_for_status()
        print(f"✅ Deleted {file_name} from backend.")
        return True
    except Exception as e:
        print(f"❌ Deletion failed for {file_name}: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Local Directory Walker
# ──────────────────────────────────────────────────────────────────────────────
def walk_local(base_path: str):
    """Recursively yield (full_path, filename) for all supported files."""
    try:
        for root, _, files in os.walk(base_path):
            for file in files:
                if os.path.splitext(file)[1].lower() in SUPPORTED_EXTENSIONS:
                    yield os.path.join(root, file), file
    except Exception as e:
        print(f"⚠️  Cannot scan {base_path}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Main Sync Loop
# ──────────────────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_dl_lock = threading.Lock()
_stats_lock = threading.Lock()
_new_files = 0
_skipped_count = 0

def touch_heartbeat():
    try:
        with open("heartbeat.txt", "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"⚠️ Could not write heartbeat file: {e}")

def _process_single_file(full_path, file_name, cache, dead_letter):
    """
    Worker function executed by the ThreadPoolExecutor for a single file.
    
    1. Reads the file in 8MB chunks to calculate a SHA-256 hash without consuming too much RAM.
    2. Checks the cache to see if the file hash is already processed.
    3. Checks the dead letter queue to see if it failed too many times previously.
    4. Uploads the document to FastAPI if it's new/modified.
    5. Updates thread-safe shared state (cache, dead_letter, stats).
    """
    global _new_files, _skipped_count
    sha = hashlib.sha256()
    try:
        with open(full_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024 * 8)  # 8 MB chunks
                if not chunk:
                    break
                sha.update(chunk)
        
        sha_hex = sha.hexdigest()

        with _cache_lock:
            cached_val = cache.get(full_path)
        if cached_val == sha_hex:
            with _stats_lock:
                _skipped_count += 1
                if _skipped_count % 100 == 0:
                    touch_heartbeat()
            return

        with _dl_lock:
            dl_failures = dead_letter.get(sha_hex, {}).get("failures", 0)
        if dl_failures >= MAX_UPLOAD_RETRIES:
            print(f"⚰️  Skipping dead-lettered file: {file_name} (SHA: {sha_hex[:12]}...)")
            return

        print(f"\\n📄 New/modified [{os.path.splitext(file_name)[1]}]: {full_path}")

        if upload_document(file_name, full_path, sha_hex):
            with _cache_lock:
                cache[full_path] = sha_hex
                save_cache(cache)
            with _dl_lock:
                if sha_hex in dead_letter:
                    del dead_letter[sha_hex]
                    save_dead_letter(dead_letter)
            with _stats_lock:
                _new_files += 1
            touch_heartbeat()
        else:
            with _dl_lock:
                entry = dead_letter.setdefault(sha_hex, {"file_name": file_name, "failures": 0, "path": full_path})
                entry["failures"] += 1
                failures = entry["failures"]
                save_dead_letter(dead_letter)
            print(f"   ⚠️  Failure #{failures}/{MAX_UPLOAD_RETRIES} — {'dead-lettering' if failures >= MAX_UPLOAD_RETRIES else 'will retry next cycle'}")

    except Exception as e:
        print(f"❌ Error processing {full_path}: {e}")

def run_sync():
    global _new_files, _skipped_count
    print(f"\n🔄 Sync started at {time.strftime('%Y-%m-%d %H:%M:%S')}")


    # 1. Pre-auth
    if not get_valid_token():
        return

    # 2. Walk local directory recursively
    cache       = load_cache()
    dead_letter = load_dead_letter()
    _new_files  = 0
    loop_count  = 0

    if not os.path.exists(LOCAL_DATA_PATH):
        print(f"⚠️ Local data path {LOCAL_DATA_PATH} does not exist.")
        return

    current_files = set()
    files_to_process = []

    for full_path, file_name in walk_local(LOCAL_DATA_PATH):
        current_files.add(full_path)
        files_to_process.append((full_path, file_name))
        loop_count += 1
        if loop_count % 10 == 0:
            touch_heartbeat()

    max_workers = int(os.getenv("MAX_CONCURRENT_UPLOADS", "8"))
    print(f"🚀 Launching {len(files_to_process)} files into parallel processing (max {max_workers} workers)...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for full_path, file_name in files_to_process:
            futures.append(executor.submit(_process_single_file, full_path, file_name, cache, dead_letter))
            
        for future in concurrent.futures.as_completed(futures):
            pass

    # 3. Detect and remove deleted files
    deleted_files = set(cache.keys()) - current_files
    files_to_remove_from_cache = []
    for full_path in deleted_files:
        file_name = os.path.basename(full_path)
        if delete_document(file_name):
            files_to_remove_from_cache.append(full_path)
    
    for full_path in files_to_remove_from_cache:
        del cache[full_path]

    if files_to_remove_from_cache or _new_files > 0:
        save_cache(cache)
        save_dead_letter(dead_letter)

    print(f"\n🏁 Sync complete — {_new_files} new/modified, {len(files_to_remove_from_cache)} deleted. Dead letter queue: {len(dead_letter)} item(s).")
    touch_heartbeat()


# ──────────────────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Ingestion Agent v3.0 starting...")
    threading.Thread(target=watchdog_loop, daemon=True).start()
    while True:
        run_sync()
        print(f"💤 Sleeping {SYNC_INTERVAL}s...\n")
        # Sleep in 10-second intervals and touch heartbeat to keep docker healthchecks and watchdog alive
        slept = 0
        while slept < SYNC_INTERVAL:
            sleep_time = min(10, SYNC_INTERVAL - slept)
            time.sleep(sleep_time)
            touch_heartbeat()
            slept += sleep_time
