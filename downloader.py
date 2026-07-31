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

if not os.path.exists(TMP_DIR):
    os.makedirs(TMP_DIR)

# ===================================================
# LOCALIZAÇÃO DO ARQUIVO DE COOKIES
# Prioridade 1: Secret File do Render (/etc/secrets/youtube_cookies.txt)
#   -> configurado no Dashboard Render > Environment > Secret Files
#   -> não passa pelo Git, não corrompe encoding, não expõe cookies no repo
#   -> IMPORTANTE: esse arquivo é montado READ-ONLY pelo Render! O yt-dlp
#      tenta regravar o cookiejar ao final de cada sessão (pra persistir
#      cookies rotacionados), e isso falha com OSError(30, 'Read-only
#      file system') se apontarmos direto pro Secret File. Por isso,
#      copiamos para um arquivo gravável em /tmp na inicialização.
# Prioridade 2: arquivo local (útil pra testar na sua máquina Windows)
# ===================================================
_RENDER_SECRET_COOKIE_PATH = "/etc/secrets/youtube_cookies.txt"
_LOCAL_COOKIE_PATH = os.path.join(BASE_DIR, 'youtube_cookies.txt')
_WRITABLE_COOKIE_PATH = "/tmp/youtube_cookies_writable.txt"

def _prepare_writable_cookie_file():
    """
    Copia o cookie do Secret File (read-only) para /tmp (gravável),
    permitindo que o yt-dlp regrave o cookiejar sem estourar OSError.
    Chamado uma vez na inicialização do módulo.
    """
    import shutil as _shutil
    if os.path.exists(_RENDER_SECRET_COOKIE_PATH):
        try:
            _shutil.copyfile(_RENDER_SECRET_COOKIE_PATH, _WRITABLE_COOKIE_PATH)
            print(f"[BOOT] Cookie copiado do Secret File (read-only) para local gravável: {_WRITABLE_COOKIE_PATH}")
            return _WRITABLE_COOKIE_PATH
        except Exception as e:
            print(f"[BOOT WARNING] Falha ao copiar cookie pra local gravável: {e}. Usando Secret File direto (pode dar erro de gravação).")
            return _RENDER_SECRET_COOKIE_PATH
    return _LOCAL_COOKIE_PATH

# ===================================================
# SUPORTE A PROXY (opcional)
# Se o IP do Render for bloqueado persistentemente pelo YouTube (403
# mesmo com cookies válidos), configure a env var YTDLP_PROXY no Render
# com a URL do proxy, ex: http://usuario:senha@host:porta
# Deixe em branco / não configure para manter o comportamento padrão (sem proxy).
# ===================================================
PROXY_URL = os.environ.get("YTDLP_PROXY", "").strip() or None

if PROXY_URL:
    print(f"[BOOT] Proxy configurado e será usado em todas as requisições do yt-dlp.")
else:
    print(f"[BOOT] Nenhum proxy configurado (YTDLP_PROXY vazio). Requisições saem do IP direto do Render.")

# ===================================================
# LIMITE MÁXIMO DE RESULTADOS POR BUSCA
# Buscas grandes (20+) forçam paginação no YouTube, que é o gatilho
# mais comum de bloqueio 403. Para uso pessoal, 5-10 resultados por
# busca já é suficiente e reduz bastante o risco de bloqueio.
# ===================================================
MAX_RESULTS_PER_SEARCH = 10

# ===================================================
# IMPERSONATION DE NAVEGADOR (curl_cffi)
# Faz as requisições HTTP terem a mesma "assinatura digital" (TLS
# fingerprint) de um navegador Chrome real, em vez da assinatura padrão
# de bibliotecas Python — isso é bem mais convincente pro YouTube do
# que só trocar o User-Agent, e reduz a chance de bloqueio por bot.
# Requer: pip install curl_cffi (adicionar no requirements.txt)
# ===================================================
try:
    import curl_cffi
    # TEMPORARIAMENTE DESATIVADO para diagnóstico — suspeita de que o
    # impersonate está causando as exceções sem mensagem que apareceram
    # no log ("Erro ao processar CD:" vazio). Reativar depois de confirmar.
    IMPERSONATE_TARGET = None
    print("[BOOT] curl_cffi encontrado, mas impersonate DESATIVADO temporariamente para diagnóstico.")
except ImportError:
    IMPERSONATE_TARGET = None
    print("[BOOT INFO] curl_cffi não instalado. Para ativar impersonation de navegador, "
          "adicione 'curl_cffi>=0.7.1' ao requirements.txt.")

def get_cookie_file_path():
    """
    Retorna o caminho do cookies.txt correto dependendo do ambiente:
    Render (produção) usa a cópia gravável em /tmp (derivada do Secret File),
    ambiente local usa o arquivo na pasta backend.
    """
    if os.path.exists(_WRITABLE_COOKIE_PATH):
        return _WRITABLE_COOKIE_PATH
    if os.path.exists(_RENDER_SECRET_COOKIE_PATH):
        return _RENDER_SECRET_COOKIE_PATH
    return _LOCAL_COOKIE_PATH

# Prepara a cópia gravável do cookie assim que o módulo é carregado
_prepare_writable_cookie_file()

COOKIE_FILE = get_cookie_file_path()

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
            return first_line.startswith('# Netscape') or first_line.startswith('# HTTP Cookie File') or 'cookie' in first_line.lower()
    except Exception:
        return False

# LOG DE BOOT — confirma qual fonte de cookies está sendo usada
if is_valid_cookie_file(COOKIE_FILE):
    print(f"[BOOT] Cookies do YouTube carregados com sucesso de: {COOKIE_FILE}")
else:
    print(f"[BOOT ALERTA] Nenhum cookies.txt válido encontrado em: {COOKIE_FILE}. "
          f"Buscas continuarão funcionando, mas o YouTube pode bloquear como bot em alguns casos.")

def format_url(url_or_id):
    if not url_or_id: return ""
    if "http://" in url_or_id or "https://" in url_or_id: return url_or_id
    return f"https://www.youtube.com/watch?v={url_or_id}"

def apply_common_opts(opts):
    """
    Aplica cookie, proxy e impersonation de navegador nas opções do yt-dlp,
    centralizando a lógica que antes estava repetida em cada função.
    """
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
    if PROXY_URL:
        opts['proxy'] = PROXY_URL
    if IMPERSONATE_TARGET:
        opts['impersonate'] = IMPERSONATE_TARGET
    return opts

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
    return apply_common_opts(opts)

def fetch_single_query(q):
    if not q.strip(): 
        return None
    search_opts = {
        'quiet': True, 
        'default_search': 'ytsearch1', 
        'extract_flat': True, 
        'nocheckcertificate': True,
        'sleep_interval_requests': 1,
        'retries': 3,
    }
    apply_common_opts(search_opts)
        
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
        print(f"[Voxify Debug Error] Falha ao extrair query avulsa: [{type(e).__name__}] {repr(e)}")
    return None

def fetch_results(queries):
    # Reduzido de 10 para 3 threads simultâneas: buscas em paralelo demais
    # com o mesmo cookie/IP são interpretadas pelo YouTube como comportamento
    # de bot e geram bloqueios 403/timeout.
    with ThreadPoolExecutor(max_workers=3) as executor:
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
    apply_common_opts(search_opts)
        
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{MAX_RESULTS_PER_SEARCH}:{query}", download=False)
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
            print(f"[Voxify Debug Error] Falha na busca avulsa de canal: [{type(e).__name__}] {repr(e)}")
    return results

def download_with_fallback(v_id, work_dir):
    try:
        print(f"[Downloader] Tentando baixar a música {v_id}...")
        with yt_dlp.YoutubeDL(get_ydl_opts(work_dir)) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={v_id}"])
            return True
    except Exception as e:
        print(f"[Fallback] Falha ao baixar {v_id}. Buscando versão alternativa... Erro: [{type(e).__name__}] {repr(e)}")
        try:
            opts_search = {'quiet': True, 'extract_flat': True}
            apply_common_opts(opts_search)
                
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
    apply_common_opts(opts)
        
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track['url'], download=False)
            return info.get('duration')
    except Exception as e:
        print(f"[Voxify Debug Error] Falha ao extrair tempo de {track['title']}: [{type(e).__name__}] {repr(e)}")
        return None

# GERADOR DE CD COM FILTRO REAL DE TEMPO (< 15M)
def generate_cd_playlist(artist, limit=20):
    candidates = []
    # Antes: limit * 2 (podia pedir 40+ resultados, forçando paginação
    # multi-página no YouTube, que é o que dispara bloqueio 403).
    # Agora: capado em MAX_RESULTS_PER_SEARCH (10), uso pessoal não precisa de mais.
    search_count = min(limit * 2, MAX_RESULTS_PER_SEARCH)
    search_query = f"ytsearch{search_count}:{artist} clipe oficial"
    opts = {
        'quiet': True, 
        'extract_flat': True, 
        'nocheckcertificate': True, 
        'compat_opts': ['no-youtube-js']
    }
    apply_common_opts(opts)
        
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
        print(f"[IA CD Generator Error] Erro ao processar CD: [{type(e).__name__}] {repr(e)}")
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

    with ThreadPoolExecutor(max_workers=4) as executor:
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
    items_per_group = max(3, min(MAX_RESULTS_PER_SEARCH, limit // len(grouped_terms) + 1))
    
    opts = {
        'quiet': True, 
        'extract_flat': True, 
        'nocheckcertificate': True, 
        'compat_opts': ['no-youtube-js'], 
        'socket_timeout': 8, 
        'retries': 3,
        'sleep_interval_requests': 1,
    }
    apply_common_opts(opts)
        
    results = []
    
    # Reduzido de 5 para 2 threads simultâneas: evita disparar rajadas de
    # requisições concorrentes que o YouTube identifica como bot (403 Forbidden)
    with ThreadPoolExecutor(max_workers=2) as executor:
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

    with ThreadPoolExecutor(max_workers=4) as executor:
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
        print(f"[Voxify Engine Warning] Falha na busca paralela de {group}: [{type(e).__name__}] {repr(e)}")
    return []