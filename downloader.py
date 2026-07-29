# backend/downloader.py
import yt_dlp
import os
import shutil
import random
import subprocess
from urllib.error import URLError
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(BASE_DIR, 'downloads')
COOKIE_FILE = os.path.join(BASE_DIR, 'youtube_cookies.txt')

if not os.path.exists(TMP_DIR):
    os.makedirs(TMP_DIR)

def is_valid_cookie_file(path):
    """
    Verifica defensivamente se o arquivo de cookies existe e segue o padrão
    Netscape antes de injetá-lo no yt-dlp, evitando quebras por arquivos corrompidos.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            first_line = f.readline()
            return first_line.startswith('# Netscape') or 'cookie' in first_line.lower()
    except Exception:
        return False

def format_url(url_or_id):
    if not url_or_id: return ""
    if "http://" in url_or_id or "https://" in url_or_id: return url_or_id
    return f"https://www.youtube.com/watch?v={url_or_id}"

# CONFIGURAÇÕES DE DOWNLOAD
def get_ydl_opts(output_path):
    ffmpeg_exe = shutil.which('ffmpeg') or r'C:\ffmpeg\bin\ffmpeg.exe'
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{output_path}/%(title)s.%(ext)s',
        'ffmpeg_location': ffmpeg_exe,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio', 
            'preferredcodec': 'mp3', 
            'preferredquality': '192' 
        }],
        'postprocessor_args': [
            '-ar', '44100'
        ],
        'quiet': True,
        'noplaylist': True, 
        'nocheckcertificate': True,
        'match_filter': yt_dlp.utils.match_filter_func("duration < 900"), 
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'android', 'web_embedded'], 
                'player_skip': ['tv', 'web']
            }
        }
    }
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
    return opts

def fetch_single_query(q):
    if not q.strip(): 
        return None
    search_opts = {
        'quiet': True, 
        'default_search': 'ytsearch1', 
        'extract_flat': True, 
        'nocheckcertificate': True
    }
    if is_valid_cookie_file(COOKIE_FILE):
        search_opts['cookiefile'] = COOKIE_FILE
        
    try:
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{q} official audio", download=False)
            if 'entries' in info and len(info['entries']) > 0:
                entry = info['entries'][0]
                
                duration = entry.get('duration')
                if duration and duration > 900:
                    return None
                    
                title_lower = entry.get('title', '').lower()
                forbidden_terms = [
                    'cover', 'covers', 'karaoke', 'karaokê', 'instrumental', 
                    'acustico', 'acústico', 'set ', 'mix', 'dvd', 'completo', 
                    'album', 'coletanea', 'coletânea', 'playlist', 'grandes sucessos', 
                    'melhores sucessos', 'sucessos de', 'as melhores'
                ]
                if any(term in title_lower for term in forbidden_terms):
                    return None

                return {
                    'title': entry.get('title'), 
                    'url': f"https://www.youtube.com/watch?v={entry.get('id')}", 
                    'thumbnail': entry.get('thumbnails')[0]['url'] if entry.get('thumbnails') else None, 
                    'videoId': entry.get('id'),
                    'duration': duration
                }
    except Exception as e:
        print(f"[Voxify Debug Error] Falha ao extrair query avulsa: {e}")
    return None

def fetch_results(queries):
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single_query, q) for q in queries if q.strip()]
        raw_results = [f.result() for f in futures]
    return [r for r in raw_results if r is not None]

def search_single(query):
    results = []
    search_opts = {
        'quiet': True, 
        'extract_flat': True, 
        'nocheckcertificate': True
    }
    if is_valid_cookie_file(COOKIE_FILE):
        search_opts['cookiefile'] = COOKIE_FILE
        
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            if 'entries' in info:
                for entry in info['entries']:
                    duration = entry.get('duration')
                    if duration and duration > 900:
                        continue
                    
                    title_lower = entry.get('title', '').lower()
                    if any(term in title_lower for term in ['cover', 'covers', 'karaoke', 'karaokê', 'instrumental', 'acustico', 'acústico', 'set ', 'mix', 'dvd', 'completo', 'album', 'coletanea', 'coletânea', 'playlist']):
                        continue

                    results.append({
                        'title': entry.get('title', 'Sem título'), 
                        'url': f"https://www.youtube.com/watch?v={entry.get('id')}", 
                        'thumbnail': entry.get('thumbnails')[0]['url'] if entry.get('thumbnails') else None, 
                        'videoId': entry.get('id'),
                        'duration': duration
                    })
        except Exception as e:
            print(f"[Voxify Debug Error] Falha na busca avulsa de canal: {e}")
    return results

def download_with_fallback(v_id, work_dir):
    try:
        print(f"[Downloader] Tentando baixar a música {v_id}...")
        with yt_dlp.YoutubeDL(get_ydl_opts(work_dir)) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={v_id}"])
            return True
    except Exception as e:
        print(f"[Fallback] Falha ao baixar {v_id}. Buscando versão alternativa... Erro: {e}")
        try:
            opts_search = {'quiet': True, 'extract_flat': True}
            if is_valid_cookie_file(COOKIE_FILE):
                opts_search['cookiefile'] = COOKIE_FILE
                
            with yt_dlp.YoutubeDL(opts_search) as ydl_s:
                info = ydl_s.extract_info(f"https://www.youtube.com/watch?v={v_id}", download=False)
                title = info.get('title', '')
                alt_info = ydl_s.extract_info(f"ytsearch1:{title} official audio", download=False)
                alt_id = alt_info['entries'][0]['id']
                
            print(f"[Fallback] Encontrado alternativo: {alt_id}. Baixando...")
            with yt_dlp.YoutubeDL(get_ydl_opts(work_dir)) as ydl2:
                ydl2.download([f"https://www.youtube.com/watch?v={alt_id}"])
                return True
        except Exception:
            return False

# VERIFICADOR DE TEMPO INDIVIDUAL
def verify_track_duration(track):
    opts = {
        'quiet': True,
        'extract_flat': False,
        'nocheckcertificate': True
    }
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
        
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track['url'], download=False)
            return info.get('duration')
    except Exception as e:
        print(f"[Voxify Debug Error] Falha ao extrair tempo de {track['title']}: {e}")
        return None

# GERADOR DE CD COM FILTRO REAL DE TEMPO (< 15M)
def generate_cd_playlist(artist, limit=20):
    candidates = []
    search_query = f"ytsearch{limit * 2}:{artist} clipe oficial"
    opts = {
        'quiet': True, 
        'extract_flat': True, 
        'nocheckcertificate': True, 
        'compat_opts': ['no-youtube-js']
    }
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
        
    print(f"[IA CD Generator] Buscando {limit} músicas de: {artist}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                for entry in info['entries']:
                    if not entry.get('id'): continue
                    
                    title_lower = entry.get('title', '').lower()
                    forbidden_terms = [
                        'cover', 'covers', 'karaoke', 'karaokê', 'instrumental', 
                        'acustico', 'acústico', 'set ', 'mix', 'dvd', 'completo', 
                        'album', 'coletanea', 'coletânea', 'playlist', ' full ', 'complete'
                    ]
                    if any(term in title_lower for term in forbidden_terms):
                        continue
                        
                    candidates.append({
                        'title': entry.get('title', 'Sem título'), 
                        'url': f"https://www.youtube.com/watch?v={entry.get('id')}", 
                        'thumbnail': entry.get('thumbnails')[0]['url'] if entry.get('thumbnails') else None, 
                        'videoId': entry.get('id')
                    })
    except Exception as e: 
        print(f"[IA CD Generator Error] Erro ao processar CD: {e}")
        return []

    print(f"[IA CD Generator] Validando tempo de {len(candidates)} faixas candidatas em paralelo...")
    final_tracks = []
    
    def process_candidate(c):
        dur = verify_track_duration(c)
        if dur:
            c['duration'] = dur
            if dur > 900:
                print(f"[Voxify Filter] 🚨 BARRADO POR TEMPO NO CD: {c['title']} ({dur}s)")
                return None
        return c

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_candidate, c) for c in candidates]
        for f in futures:
            res = f.result()
            if res:
                final_tracks.append(res)
                
    random.shuffle(final_tracks)
    return final_tracks[:limit]

# PLAYLIST IA MULTITHREADED COM FILTRO REAL DE TEMPO (< 15M)
def generate_custom_playlist(query, limit=30):
    clean_query = query.replace(' mix', '').strip()
    raw_terms = [t.strip() for t in clean_query.split(',') if t.strip()]
    
    if len(raw_terms) <= 1:
        raw_terms = [t.strip() for t in clean_query.split(' ') if len(t.strip()) > 2]
        
    grouped_terms = [" ".join(raw_terms[i:i+2]) for i in range(0, len(raw_terms), 2)] or [clean_query]
    items_per_group = max(3, min(15, limit // len(grouped_terms) + 1))
    
    opts = {
        'quiet': True, 
        'extract_flat': True, 
        'nocheckcertificate': True, 
        'compat_opts': ['no-youtube-js'], 
        'socket_timeout': 5, 
        'retries': 1
    }
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
        
    results = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_single_group, group, items_per_group, opts) for group in grouped_terms]
        for f in futures:
            results.extend(f.result())
            
    seen = set()
    candidates = []
    
    for r in results:
        title_lower = r['title'].lower()
        forbidden_terms = [
            'cover', 'covers', 'karaoke', 'karaokê', 'instrumental', 
            'acustico', 'acústico', 'set ', 'mix', 'dvd', 'completo', 
            'album', 'coletanea', 'coletânea', 'playlist', ' full ', 'complete'
        ]
        if any(term in title_lower for term in forbidden_terms):
            continue
            
        if r['videoId'] not in seen:
            seen.add(r['videoId'])
            candidates.append(r)

    print(f"[IA Custom Generator] Validando tempo de {len(candidates)} faixas em paralelo...")
    final_tracks = []
    
    def process_candidate(c):
        dur = verify_track_duration(c)
        if dur:
            c['duration'] = dur
            if dur > 900:
                print(f"[Voxify Filter] 🚨 BARRADO POR TEMPO NO CUSTOM: {c['title']} ({dur}s)")
                return None
        return c

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_candidate, c) for c in candidates]
        for f in futures:
            res = f.result()
            if res:
                final_tracks.append(res)

    random.shuffle(final_tracks)
    return final_tracks[:limit]

def fetch_single_group(group, items_per_group, opts):
    search_query = f"ytsearch{items_per_group}:{group} musica oficial clipe -mix -set -completo"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                return [
                    {
                        'title': entry.get('title', 'Sem título'), 
                        'url': f"https://www.youtube.com/watch?v={entry.get('id')}", 
                        'thumbnail': entry.get('thumbnails')[0]['url'] if entry.get('thumbnails') else None, 
                        'videoId': entry.get('id')
                    }
                    for entry in info['entries'] if entry.get('id')
                ]
    except Exception as e:
        print(f"[Voxify Engine Warning] Falha na busca paralela de {group}: {e}")
    return []