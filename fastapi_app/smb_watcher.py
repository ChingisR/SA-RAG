import smbclient
import os
import time
from pathlib import Path
from dotenv import load_dotenv

# Load local .env if present
load_dotenv()

SMB_SERVER = os.getenv("SMB_SERVER", "10.243.100.6")
SMB_SHARE = os.getenv("SMB_SHARE", r"\\10.243.100.6\OcifrovkaDoc")
SMB_USER = os.getenv("SMB_USER", "svc-rag-reader")
SMB_PASS = os.getenv("SMB_PASSWORD")

if not SMB_PASS:
    print("⚠️ WARNING: SMB_PASSWORD environment variable is not set. SMB Watcher might fail to authenticate.")

LOCAL_DIR = "/app/smb_copy"  # Mapped to /mnt/sda1/chingis/AgenticRAG/smb_copy on the host


# Ensure local dir exists
os.makedirs(LOCAL_DIR, exist_ok=True)

def get_remote_files_recursive(current_path=""):
    remote_files = []
    full_path = f"{SMB_SHARE}\\{current_path}" if current_path else SMB_SHARE
    try:
        entries = smbclient.scandir(full_path)
        for entry in entries:
            # Skip hidden files
            if entry.name.startswith('.'):
                continue
            
            relative_path = f"{current_path}\\{entry.name}" if current_path else entry.name
            try:
                if entry.is_dir():
                    # It's a directory, traverse recursively
                    remote_files.extend(get_remote_files_recursive(relative_path))
                elif entry.is_file():
                    # It's a file, check if it's a PDF
                    if entry.name.lower().endswith('.pdf'):
                        remote_files.append(relative_path)
            except Exception as e:
                print(f"Error processing entry {entry.name}: {e}")
    except Exception as e:
        print(f"Error scanning {full_path}: {e}")
    return remote_files

def run_sync():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Connecting to SMB Share {SMB_SHARE}...")
    try:
        smbclient.register_session(SMB_SERVER, username=SMB_USER, password=SMB_PASS)
        
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Scanning for remote files...")
        start_time = time.time()
        remote_files = get_remote_files_recursive()
        scan_duration = time.time() - start_time
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Scan completed in {scan_duration:.2f} seconds. Found {len(remote_files)} remote PDFs.")
        
        new_files_count = 0
        downloaded_count = 0
        
        for relative_path in remote_files:
            clean_rel_path = relative_path.replace("\\", "/")
            local_full_path = os.path.join(LOCAL_DIR, clean_rel_path)
            local_dir = os.path.dirname(local_full_path)
            
            if not os.path.exists(local_full_path):
                new_files_count += 1
                print(f"📥 New file detected: {relative_path}")
                os.makedirs(local_dir, exist_ok=True)
                
                # Download file to local smb_copy so ingestion-agent can pick it up
                remote_full_path = f"{SMB_SHARE}\\{relative_path}"
                try:
                    with smbclient.open_file(remote_full_path, mode='rb') as rf:
                        with open(local_full_path, 'wb') as lf:
                            lf.write(rf.read())
                    print(f"💾 Downloaded to {local_full_path}")
                    downloaded_count += 1
                except Exception as e:
                    print(f"❌ Failed to download {relative_path}: {e}")
                    
        if new_files_count > 0:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sync finished. Downloaded {downloaded_count} out of {new_files_count} new files.")
        else:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sync finished. No new files detected.")
            
    except Exception as e:
        print(f"❌ SMB Connection Error: {e}")
    finally:
        smbclient.reset_connection_cache()

if __name__ == "__main__":
    print("🚀 SMB Watchdog Sync Started!")
    while True:
        run_sync()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sleeping for 15 minutes...")
        time.sleep(900)
