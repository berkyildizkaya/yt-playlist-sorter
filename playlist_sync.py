import os
import re
import sys
import argparse
import logging
import time
from collections import Counter
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configure logging to write to both a file and standard output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("playlist_sync.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

# Scope for YouTube Data API (Read/Write access to user's playlists)
SCOPES = ['https://www.googleapis.com/auth/youtube']

def handle_http_error(e, context_message):
    """Handles YouTube API errors, providing user-friendly instructions for common issues."""
    error_msg = str(e)
    logging.error(f"API Hatası ({context_message}): {e}")
    
    if "accessNotConfigured" in error_msg or "has not been used in project" in error_msg:
        logging.error("\n" + "="*80)
        logging.error("[!] HATA: YouTube Data API v3 Google Cloud projenizde etkinleştirilmemiş!")
        logging.error("Bu hatayı düzeltmek için:")
        logging.error("1. Aşağıdaki bağlantıya tıklayarak projenizde API'yi etkinleştirin (Enable yapın):")
        
        # Try to extract the console URL from the error message
        match = re.search(r'https://console\S+', error_msg)
        if match:
            url = match.group(0).rstrip('."\'')
            logging.error(f"   👉 {url}")
        else:
            logging.error("   👉 https://console.cloud.google.com/apis/library/youtube.googleapis.com")
            
        logging.error("2. API'yi etkinleştirdikten sonra 1-2 dakika bekleyin.")
        logging.error("3. Bu scripti tekrar çalıştırın.")
        logging.error("="*80 + "\n")
        
    elif "quotaExceeded" in error_msg:
        logging.error("\n" + "="*80)
        logging.error("[!] HATA: YouTube API günlük kullanım kotanız doldu!")
        logging.error("YouTube API günlük 10,000 birim kota sınırı uygular ve bu sınır aşılmış görünüyor.")
        logging.error("Yarın tekrar deneyebilir veya yeni bir Google Cloud projesi açarak kimlik bilgilerini güncelleyebilirsiniz.")
        logging.error("="*80 + "\n")
        
    sys.exit(1)

def authenticate_youtube():
    """Authenticates the user and returns the YouTube API service object."""
    creds = None
    # The file token.json stores the user's access and refresh tokens
    if os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        except Exception as e:
            logging.warning(f"token.json okunamadı, yeniden yetkilendirme yapılacak. Hata: {e}")
            creds = None

    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logging.warning(f"Token yenilenemedi, yeniden giriş yapılıyor: {e}")
                creds = None
        
        if not creds:
            if not os.path.exists('credentials.json'):
                logging.error("\nHata: 'credentials.json' dosyası bulunamadı!")
                logging.error("Lütfen README.md dosyasındaki adımları takip ederek Google Cloud'dan")
                logging.error("indirdiğiniz kimlik dosyasına 'credentials.json' adını verin ve bu klasöre koyun.\n")
                sys.exit(1)
            
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            # Run local server to authenticate. Using port=0 to find any available port.
            creds = flow.run_local_server(port=0)
            
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            
    return build('youtube', 'v3', credentials=creds)

def extract_playlist_id(url_or_id):
    """Extracts playlist ID from a YouTube playlist URL or returns it directly if it's already an ID."""
    url_or_id = url_or_id.strip()
    match = re.search(r'[&?]list=([^&]+)', url_or_id)
    if match:
        return match.group(1)
    return url_or_id

def extract_episode_number(title):
    """
    Extracts the episode number from a video title using multiple pattern matches.
    If no number matches, returns float('inf') so it gets sorted to the end.
    """
    title_lower = title.lower()
    
    # 1. Pattern: S01E02 or similar (Season & Episode)
    se_match = re.search(r's\d+e(\d+)', title_lower)
    if se_match:
        return int(se_match.group(1))
    
    # 2. Pattern: 1x02 (Season x Episode)
    x_match = re.search(r'\d+x(\d+)', title_lower)
    if x_match:
        return int(x_match.group(1))
    
    # 3. Pattern: "1. Bölüm" or "1.bölüm" or "1 Bölüm"
    b1_match = re.search(r'(\d+)\s*\.?\s*(?:bölüm|bolum|episode|ep|part|b)\b', title_lower)
    if b1_match:
        return int(b1_match.group(1))
    
    # 4. Pattern: "Bölüm 2" or "Bölüm: 2" or "Bölüm - 2" or "Ep.2"
    b2_match = re.search(r'(?:bölüm|bolum|episode|ep|part|b)\s*(?:[.:-]\s*)?(\d+)', title_lower)
    if b2_match:
        return int(b2_match.group(1))
    
    # 5. Fallback: Find the first standalone number that isn't a common year or resolution
    numbers = re.findall(r'\b\d+\b', title_lower)
    for num_str in numbers:
        num = int(num_str)
        # Skip resolutions (1080, 720, 480, 360, 2160) and common year ranges
        if num in [360, 480, 720, 1080, 2160] or (1990 <= num <= 2030):
            continue
        return num
        
    return float('inf')

def get_playlist_videos(youtube, playlist_id):
    """Fetches all video details from the given playlist using pagination."""
    videos = []
    try:
        request = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50
        )
        while request:
            response = request.execute()
            for item in response.get('items', []):
                snippet = item.get('snippet', {})
                video_id = snippet.get('resourceId', {}).get('videoId')
                title = snippet.get('title', '')
                videos.append({
                    'videoId': video_id,
                    'title': title,
                    'position': snippet.get('position', 0)
                })
            request = youtube.playlistItems().list_next(request, response)
    except HttpError as e:
        handle_http_error(e, "Kaynak oynatma listesi çekilirken")
        
    return videos

def get_or_create_dest_playlist(youtube, playlist_name):
    """
    Checks if a playlist with the given name exists in the user's account.
    If yes, returns its ID and False. If no, creates it (Private) and returns its ID and True.
    """
    try:
        # Search in user's playlists
        request = youtube.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50
        )
        while request:
            response = request.execute()
            for item in response.get('items', []):
                if item['snippet']['title'].strip().lower() == playlist_name.strip().lower():
                    logging.info(f"Mevcut oynatma listesi bulundu: '{playlist_name}' (ID: {item['id']})")
                    return item['id'], False
            request = youtube.playlists().list_next(request, response)
            
        # Create a new playlist if not found
        logging.info(f"Yeni oynatma listesi oluşturuluyor: '{playlist_name}'...")
        response = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": playlist_name,
                    "description": "Otomatik olarak doğru bölüm sırasıyla eşitlenmiş oynatma listesi."
                },
                "status": {
                    "privacyStatus": "private"  # Private by default
                }
            }
        ).execute()
        logging.info(f"Oynatma listesi başarıyla oluşturuldu! (ID: {response['id']})")
        
        # Add a small delay for replication across YouTube servers
        logging.info("YouTube sunucularının senkronize olması için 3 saniye bekleniyor...")
        time.sleep(3)
        
        return response['id'], True
    except HttpError as e:
        handle_http_error(e, "Hedef oynatma listesi aranırken veya oluşturulurken")

def get_destination_playlist_items(youtube, playlist_id):
    """Fetches all items from the destination playlist, returning item IDs and video IDs."""
    items = []
    try:
        request = youtube.playlistItems().list(
            part="snippet",
            playlistId=playlist_id,
            maxResults=50
        )
        while request:
            response = request.execute()
            for item in response.get('items', []):
                snippet = item.get('snippet', {})
                items.append({
                    'playlistItemId': item['id'],
                    'videoId': snippet.get('resourceId', {}).get('videoId'),
                    'title': snippet.get('title', '')
                })
            request = youtube.playlistItems().list_next(request, response)
    except HttpError as e:
        error_msg = str(e)
        if "playlistNotFound" in error_msg:
            logging.warning("Hedef oynatma listesi henüz YouTube sunucularında tam olarak yansıtılmadı (yansıma gecikmesi). Boş liste olarak kabul ediliyor.")
            return []
        handle_http_error(e, "Hedef oynatma listesi içeriği okunurken")
    return items

def sync_playlists(youtube, source_videos, dest_playlist_id, dest_items=None):
    """
    Syncs the destination playlist with the sorted source videos list.
    Removes excess, adds missing, and fixes ordering with minimal quota usage.
    """
    # 1. Sort the source videos based on the extracted episode number
    sorted_source = sorted(source_videos, key=lambda v: extract_episode_number(v['title']))
    target_video_ids = [v['videoId'] for v in sorted_source]
    
    # 2. Get current destination items if not provided (e.g. if it wasn't newly created)
    if dest_items is None:
        dest_items = get_destination_playlist_items(youtube, dest_playlist_id)
    
    logging.info("--- Senkronizasyon Analizi Başlatılıyor ---")
    logging.info(f"Kaynak listede toplam video: {len(source_videos)}")
    logging.info(f"Hedef listede mevcut video: {len(dest_items)}")
    
    # Check if sets and order already match perfectly
    current_video_ids = [item['videoId'] for item in dest_items]
    if current_video_ids == target_video_ids:
        logging.info("Mükemmel! Oynatma listesi zaten güncel ve doğru sırada. Hiçbir işlem yapılmadı.")
        return

    # Counts of video IDs
    target_counts = Counter(target_video_ids)
    current_counts = Counter(item['videoId'] for item in dest_items)
    
    # 3. Step 1: Remove excess video occurrences
    for video_id in list(current_counts.keys()):
        excess = current_counts[video_id] - target_counts[video_id]
        if excess > 0:
            indices_to_delete = [idx for idx, item in enumerate(dest_items) if item['videoId'] == video_id][-excess:]
            for idx in sorted(indices_to_delete, reverse=True):
                item = dest_items[idx]
                logging.info(f"[-] Kaldırılıyor (Gereksiz/Fazla): '{item['title']}' ({video_id})")
                try:
                    youtube.playlistItems().delete(id=item['playlistItemId']).execute()
                    dest_items.pop(idx)
                except HttpError as e:
                    handle_http_error(e, f"Gereksiz video ({item['title']}) silinirken")
            current_counts[video_id] = target_counts[video_id]

    # 4. Step 2: Add missing videos (Appended to the end)
    for video_id, target_cnt in target_counts.items():
        missing = target_cnt - current_counts.get(video_id, 0)
        if missing > 0:
            title = next((v['title'] for v in source_videos if v['videoId'] == video_id), "Bilinmeyen Başlık")
            for _ in range(missing):
                logging.info(f"[+] Ekleniyor (Eksik): '{title}' ({video_id})")
                try:
                    response = youtube.playlistItems().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "playlistId": dest_playlist_id,
                                "resourceId": {
                                    "kind": "youtube#video",
                                    "videoId": video_id
                                }
                            }
                        }
                    ).execute()
                    dest_items.append({
                        'playlistItemId': response['id'],
                        'videoId': video_id,
                        'title': title
                    })
                except HttpError as e:
                    handle_http_error(e, f"Eksik video ({title}) eklenirken")
                    
    # 5. Step 3: Align the sequence order
    for i in range(len(target_video_ids)):
        target_id = target_video_ids[i]
        current_id = dest_items[i]['videoId']
        
        if current_id != target_id:
            found_idx = -1
            for j in range(i + 1, len(dest_items)):
                if dest_items[j]['videoId'] == target_id:
                    found_idx = j
                    break
            
            if found_idx != -1:
                item_to_move = dest_items.pop(found_idx)
                logging.info(f"[/] Konum Güncelleniyor: '{item_to_move['title']}' -> Sıra: {i+1}")
                try:
                    youtube.playlistItems().update(
                        part="snippet",
                        body={
                            "id": item_to_move["playlistItemId"],
                            "snippet": {
                                "playlistId": dest_playlist_id,
                                "resourceId": {
                                    "kind": "youtube#video",
                                    "videoId": item_to_move["videoId"]
                                },
                                "position": i
                            }
                        }
                    ).execute()
                    dest_items.insert(i, item_to_move)
                except HttpError as e:
                    handle_http_error(e, f"Video konumu ({item_to_move['title']}) güncellenirken")

    logging.info("--- Senkronizasyon Başarıyla Tamamlandı! ---")

def main():
    parser = argparse.ArgumentParser(description="YouTube oynatma listesini bölüm sırasına göre senkronize eder.")
    parser.add_argument("-s", "--source", help="Kaynak oynatma listesi ID'si veya URL'si")
    parser.add_argument("-d", "--dest", help="Oluşturulacak/Güncellenecek hedef oynatma listesinin adı")
    args = parser.parse_args()
    
    source_input = args.source
    dest_name = args.dest
    
    print("=" * 60)
    print("      YouTube Oynatma Listesi Sıralayıcı & Senkronize Edici")
    print("=" * 60)
    
    if not source_input:
        source_input = input("Kaynak oynatma listesi URL'si veya ID'sini girin: ")
    if not dest_name:
        dest_name = input("Oluşturulacak hedef oynatma listesinin adını girin: ")
        
    source_id = extract_playlist_id(source_input)
    if not source_id:
        logging.error("Hata: Geçersiz kaynak oynatma listesi ID'si veya URL'si.")
        sys.exit(1)
        
    logging.info("[1] YouTube API Yetkilendirmesi Yapılıyor...")
    youtube = authenticate_youtube()
    
    logging.info("[2] Kaynak Oynatma Listesindeki Videolar Çekiliyor...")
    source_videos = get_playlist_videos(youtube, source_id)
    if not source_videos:
        logging.error("Kaynak oynatma listesinde hiç video bulunamadı veya listeye erişilemiyor.")
        sys.exit(1)
        
    logging.info(f"Bulunan video sayısı: {len(source_videos)}")
    logging.info("İlk 3 video:")
    for v in source_videos[:3]:
        logging.info(f" - {v['title']} (Bölüm: {extract_episode_number(v['title'])})")
        
    logging.info("[3] Hedef Oynatma Listesi Kontrol Ediliyor...")
    dest_playlist_id, was_created = get_or_create_dest_playlist(youtube, dest_name)
    
    dest_items = [] if was_created else None
    
    logging.info("[4] Senkronizasyon Başlatılıyor...")
    sync_playlists(youtube, source_videos, dest_playlist_id, dest_items)

if __name__ == "__main__":
    main()
