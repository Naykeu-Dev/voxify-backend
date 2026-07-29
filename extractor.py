# backend/extractor.py
import os
import shutil
import random
import asyncio
import subprocess
import yt_dlp
import re
import traceback
import threading

# CORREÇÃO: Removido generate_youtube_mix da importação de downloader (já que está definida nativamente abaixo)
from downloader import verify_track_duration, is_valid_cookie_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(BASE_DIR, 'downloads')
COOKIE_FILE = os.path.join(BASE_DIR, 'youtube_cookies.txt')

if not os.path.exists(TMP_DIR):
    os.makedirs(TMP_DIR)

# Injeção dinâmica do FFmpeg nas variáveis de ambiente do Windows/Linux
ffmpeg_dirs = [
    r'C:\ffmpeg\bin',
    r'C:\Program Files\ffmpeg\bin',
    r'C:\ffmpeg',
    r'/usr/bin',
    r'/usr/local/bin'
]
for d in ffmpeg_dirs:
    if os.path.exists(d) and d not in os.environ['PATH']:
        os.environ['PATH'] += os.pathsep + d
        print(f"[SONIC PATH] Diretório do FFmpeg injetado dinamicamente: {d}")

def format_url(url_or_id):
    if not url_or_id: return ""
    if "http://" in url_or_id or "https://" in url_or_id: return url_or_id
    return f"https://www.youtube.com/watch?v={url_or_id}"

def download_audio_defensive_local(target_url, work_dir, high_quality=False):
    formats_to_try = ['bestaudio/best', 'worstaudio/worst', 'best']
    if not high_quality:
        formats_to_try = ['worstaudio/worst', 'bestaudio/best', 'best']

    cookie_options = [True, False]
    info = None
    err_msg = ""
    
    for use_cookies in cookie_options:
        for fmt in formats_to_try:
            opts = {
                'format': fmt,
                'outtmpl': f'{work_dir}/audio.%(ext)s',
                'quiet': True,
                'noplaylist': True,
                'nocheckcertificate': True,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['mweb', 'android', 'web_embedded'],
                        'player_skip': ['tv', 'web']
                    }
                }
            }
            if use_cookies and is_valid_cookie_file(COOKIE_FILE):
                opts['cookiefile'] = COOKIE_FILE
                
            try:
                print(f"[SONIC DOWNLOAD] Baixando áudio (cookies={use_cookies}, formato='{fmt}')...")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target_url, download=True)
                    if info:
                        duration = info.get('duration', 1800)
                        return info, duration
            except Exception as e:
                err_msg = str(e)
                print(f"[SONIC DOWNLOAD TRY] Falha no método atual: {e}")
                for f in os.listdir(work_dir):
                    try: os.remove(os.path.join(work_dir, f))
                    except: pass
                    
    raise Exception(f"YTDLP_EXTRACTION_FAILED: {err_msg}")

async def _shazam_scan_timestamps(filepath, total_duration, work_dir):
    from shazamio import Shazam
    shazam = Shazam()
    ffmpeg_exe = shutil.which('ffmpeg') or r'C:\ffmpeg\bin\ffmpeg.exe'

    step = 90  
    detections = []  

    print("[SHAZAM SCANNER] Iniciando escaneamento sônico para geração de marcadores...")
    for start_time in range(15, int(total_duration) - 15, step):
        chunk_path = os.path.join(work_dir, f"chunk_{start_time}.mp3")
        try:
            cmd = [
                ffmpeg_exe, '-y', '-ss', str(start_time), '-t', '10',
                '-i', filepath, '-vn', '-ar', '44100', '-ac', '1', '-b:a', '128k', chunk_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                with open(chunk_path, 'rb') as f:
                    chunk_bytes = f.read()
                out = await shazam.recognize(chunk_bytes)
                if out and 'track' in out:
                    title = out['track'].get('title')
                    artist = out['track'].get('subtitle')
                    if title and artist:
                        full_name = f"{artist} - {title}"
                        detections.append((start_time, full_name))
                        print(f"[SHAZAM SCANNER MATCH] Tempo: {start_time}s -> {full_name}")
        except Exception as e:
            print(f"[SHAZAM CHUNK ERROR] {e}")
        finally:
            if os.path.exists(chunk_path):
                try: os.remove(chunk_path)
                except: pass

    return detections

async def _identify_segment_async(filepath, start_time):
    from shazamio import Shazam
    shazam = Shazam()
    ffmpeg_exe = shutil.which('ffmpeg') or r'C:\ffmpeg\bin\ffmpeg.exe'
    
    chunk_path = f"{filepath}_auto_id_{start_time}.mp3"
    try:
        cmd = [
            ffmpeg_exe, '-y', 
            '-ss', str(start_time + 5), 
            '-t', '10',
            '-i', filepath, 
            '-vn', '-ar', '44100', '-ac', '1', '-b:a', '128k', 
            chunk_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            with open(chunk_path, 'rb') as f:
                chunk_bytes = f.read()
            out = await shazam.recognize(chunk_bytes)
            if out and 'track' in out:
                title = out['track'].get('title')
                artist = out['track'].get('subtitle')
                if title and artist:
                    return f"{artist} - {title}"
    except Exception as e:
        print(f"[AUTO-ID EXCEPTION] Falha ao processar assinatura sônica nos {start_time}s: {e}")
    finally:
        if os.path.exists(chunk_path):
            try: os.remove(chunk_path)
            except: pass
    return None

def identify_segment_shazam(filepath, start_time):
    res_box = []
    def target():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            val = loop.run_until_complete(_identify_segment_async(filepath, start_time))
            res_box.append(val)
            loop.close()
        except Exception:
            pass
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    return res_box[0] if res_box else None


async def _shazam_analyze_local_file(filepath, total_duration, work_dir):
    from shazamio import Shazam
    ffmpeg_exe = shutil.which('ffmpeg') or r'C:\ffmpeg\bin\ffmpeg.exe'
    shazam = Shazam()

    chunk_length = 10
    step = 120
    if total_duration < 600:
        step = 45
    elif total_duration < 1800:
        step = 75

    titles = []
    print(f"[PASSO 6 - BACKEND] Fatiando áudio local a cada {step}s...")

    for start_time in range(15, int(total_duration) - 15, step):
        chunk_path = os.path.join(work_dir, f"chunk_{start_time}.mp3")

        try:
            cmd_extract = [
                ffmpeg_exe, '-y', '-ss', str(start_time), '-t', str(chunk_length),
                '-i', filepath, '-vn', '-ar', '44100', '-ac', '1', '-b:a', '128k', chunk_path
            ]
            result = subprocess.run(cmd_extract, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if result.returncode != 0:
                continue

            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                with open(chunk_path, 'rb') as f:
                    chunk_bytes = f.read()

                out = await shazam.recognize(chunk_bytes)

                if out and 'track' in out:
                    title = out['track'].get('title')
                    artist = out['track'].get('subtitle')
                    if title and artist:
                        full_name = f"{artist} - {title}"
                        if full_name not in titles:
                            titles.append(full_name)
                            print(f"[SONIC ID MATCH] Identificado nos {start_time}s -> {full_name}")
        except Exception as e:
            print(f"[SONIC CHUNK EXCEPTION] Erro no bloco de {start_time}s: {e}")
        finally:
            if os.path.exists(chunk_path):
                try: os.remove(chunk_path)
                except: pass

    return titles

def shazam_extract_mix(video_id):
    target_url = format_url(video_id)
    work_dir = os.path.join(TMP_DIR, f"shazam_{random.randint(1000, 9999)}")
    if not os.path.exists(work_dir): os.makedirs(work_dir)

    print("\n" + "="*80)
    print(f"[PASSO 1 - BACKEND] REQUISIÇÃO DE EXTRAÇÃO SÔNICA (ESTÚDIO)")
    print("="*80)

    titles = []
    error_reason = None  
    try:
        info, duration = download_audio_defensive_local(target_url, work_dir, high_quality=False)

        downloaded_file = None
        for f in os.listdir(work_dir):
            if f.startswith('audio.'):
                downloaded_file = os.path.join(work_dir, f)
                break

        if downloaded_file:
            def run_async_isolated(coro):
                res_box = []
                err_box = []
                def target():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        val = loop.run_until_complete(coro)
                        res_box.append(val)
                        loop.close()
                    except Exception as ex:
                        err_box.append(ex)
                thread = threading.Thread(target=target)
                thread.start()
                thread.join()
                if err_box: raise err_box[0]
                return res_box[0] if res_box else []

            titles = run_async_isolated(_shazam_analyze_local_file(downloaded_file, duration, work_dir))
    except Exception as e:
        error_reason = str(e)
        traceback.print_exc()
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

    return titles, error_reason


# ===================================================
# ETAPA 1 DO PRECISION SLICER - INTEGRADO COM BANCO SQLITE (CACHE HIT)
# ===================================================
def analyze_mix_markers_for_slicer(video_id):
    """
    Tenta recuperar do SQLite local os marcadores já calculados/editados anteriormente.
    Se encontrar, retorna na hora. Se for a primeira vez, faz a varredura e salva no banco.
    """
    cached_data = db.get_mix_markers_db(video_id)
    if cached_data:
        print(f"[SLICER CACHE HIT] Marcadores recuperados do SQLite local para o Vídeo {video_id}!")
        return cached_data, None

    target_url = format_url(video_id)
    work_dir = os.path.join(TMP_DIR, f"slicer_cache_{video_id}")
    if not os.path.exists(work_dir): os.makedirs(work_dir)

    print(f"\n[SLICER CACHE MISS] Analisando marcadores sônicos do zero para o Video ID: {video_id}")
    
    error_reason = None
    meta = {
        'videoId': video_id,
        'title': "Mix Sintonizado",
        'duration': 0,
        'markers': []
    }

    try:
        downloaded_file = None
        for f in os.listdir(work_dir):
            if f.startswith('audio.'):
                downloaded_file = os.path.join(work_dir, f)
                break

        if downloaded_file:
            opts = {'quiet': True}
            if is_valid_cookie_file(COOKIE_FILE):
                opts['cookiefile'] = COOKIE_FILE
                
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                duration = info.get('duration', 1800)
                meta['title'] = info.get('title', "Mix Sintonizado")
        else:
            info, duration = download_audio_defensive_local(target_url, work_dir, high_quality=True)
            meta['title'] = info.get('title', "Mix Sintonizado")
            for f in os.listdir(work_dir):
                if f.startswith('audio.'):
                    downloaded_file = os.path.join(work_dir, f)
                    break

        meta['duration'] = duration

        chapters = info.get('chapters') or []
        if chapters:
            print("[SLICER] Encontrados capítulos oficiais. Importando marcadores...")
            for chap in chapters:
                meta['markers'].append({
                    'time': chap.get('start_time', 0),
                    'title': chap.get('title', 'Sem título').strip()
                })
        else:
            print("[SLICER] Sintonizando Shazam de Alta Densidade...")
            def run_async_isolated(coro):
                res_box = []
                def target():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    val = loop.run_until_complete(coro)
                    res_box.append(val)
                    loop.close()
                thread = threading.Thread(target=target)
                thread.start()
                thread.join()
                return res_box[0] if res_box else []

            detections = run_async_isolated(_shazam_scan_timestamps(downloaded_file, duration, work_dir))

            seen_titles = set()
            for det in detections:
                t, title = det
                if title not in seen_titles:
                    seen_titles.add(title)
                    meta['markers'].append({
                        'time': t,
                        'title': title
                    })

        # SALVA NO SQLITE
        db.save_mix_markers_db(video_id, meta['title'], duration, meta['markers'])

    except Exception as e:
        error_reason = str(e)
        traceback.print_exc()

    return meta, error_reason


# ===================================================
# ETAPA 2 DO PRECISION SLICER - SALVA OS AJUSTES FINOS MANUAIS NO BANCO E EXECUTA
# ===================================================
def execute_slicer_cuts_physical(video_id, markers, storage_dir):
    """
    Recebe os tempos de corte customizados ajustados manualmente pelo usuário no React,
    executa a auto-identificação sônica via Shazam para cada segmento individual,
    e fatias os arquivos .mp3 na pasta musicas_salvas.
    """
    work_dir = os.path.join(TMP_DIR, f"slicer_cache_{video_id}")
    ffmpeg_exe = shutil.which('ffmpeg') or r'C:\ffmpeg\bin\ffmpeg.exe'

    tracks = []
    error_reason = None

    try:
        downloaded_file = None
        if os.path.exists(work_dir):
            for f in os.listdir(work_dir):
                if f.startswith('audio.'):
                    downloaded_file = os.path.join(work_dir, f)
                    break

        if not downloaded_file:
            raise Exception("O arquivo original de áudio não foi localizado no cache do servidor. Por favor, reanalise o Mix.")

        # Obtém informações básicas do Mix
        opts = {'quiet': True}
        if is_valid_cookie_file(COOKIE_FILE):
            opts['cookiefile'] = COOKIE_FILE
            
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)
            title_mix = info.get('title', "Mix Sintonizado")

        # PERSISTE OS AJUSTES MANUAIS
        print("[SLICER CACHE] Persistindo os ajustes manuais do usuário no banco SQLite...")
        duration = 0
        db_markers = []
        for m in markers:
            db_markers.append({
                'time': m['start'],
                'title': m['title']
            })
            duration = max(duration, m['end'])
            
        db.save_mix_markers_db(video_id, title_mix, duration, db_markers)

        print(f"[SLICER CUTS] Fatiando áudio em {len(markers)} MP3s...")

        for idx, m in enumerate(markers):
            start = m.get('start', 0)
            end = m.get('end', 0)
            user_title = m.get('title', f"Faixa {idx + 1}").strip()
            
            print(f"[SLICER IA] Identificando de ouvido o trecho {idx + 1} ({start}s - {end}s)...")
            shazam_title = identify_segment_shazam(downloaded_file, start)
            
            if shazam_title:
                print(f"[SLICER IA SUCCESS] Sintonizado e reconhecido: '{shazam_title}'")
                final_title = shazam_title
            else:
                print(f"[SLICER IA WARNING] Mantendo nome do editor: '{user_title}'")
                final_title = user_title

            safe_title = re.sub(r'[\\/*?:"<>|]', "", final_title)
            output_filename = f"{safe_title}.mp3"
            output_filepath = os.path.join(storage_dir, output_filename)

            # Executa o corte estéreo síncrono e de alta fidelidade
            cmd = [
                ffmpeg_exe, '-y',
                '-ss', str(start),
                '-to', str(end),
                '-i', downloaded_file,
                '-vn', '-ar', '44100', '-ac', '2', '-b:a', '192k',
                output_filepath
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if os.path.exists(output_filepath) and os.path.getsize(output_filepath) > 0:
                print(f"[SLICER SUCCESS] Faixa '{final_title}' recortada perfeitamente no HD.")
                tracks.append({
                    'title': final_title,
                    'url': f"https://www.youtube.com/watch?v={video_id}",
                    'thumbnail': f"https://img.youtube.com/vi/{video_id}/0.jpg",
                    'videoId': f"local_cut_{video_id}_{idx}"  
                })

        # Limpa o cache temporário
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

    except Exception as e:
        error_reason = str(e)
        traceback.print_exc()

    return tracks, error_reason

def generate_youtube_mix(video_id, limit=30):
    results = []
    opts = {
        'quiet': True, 
        'extract_flat': True, 
        'nocheckcertificate': True, 
        'compat_opts': ['no-youtube-js']
    }
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
        
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}", download=False)
            if 'entries' in info:
                entries = list(info['entries'])
                for entry in entries[1:limit+1]:
                    if not entry.get('id'): continue
                    results.append({'title': entry.get('title', 'Sem título'), 'url': f"https://www.youtube.com/watch?v={entry.get('id')}", 'thumbnail': entry.get('thumbnails')[0]['url'] if entry.get('thumbnails') else None, 'videoId': entry.get('id')})
    except Exception as e: print(f"Erro no Mix: {e}")
    return results

def extract_video_chapters(url_or_id):
    target_url = format_url(url_or_id)
    opts = {
        'quiet': True, 
        'extract_flat': False, 
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'android', 'web_embedded'],
                'player_skip': ['tv', 'web']
            }
        }
    }
    if is_valid_cookie_file(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
        
    titles = []
    print(f"\n[PASSO 1 - BACKEND CHAPTERS] Verificando capítulos para: {target_url}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            for chap in (info.get('chapters') or []):
                clean_title = chap['title'].strip()
                if clean_title: titles.append(clean_title)
            print(f"[PASSO 1 - BACKEND CHAPTERS] Total de capítulos encontrados: {len(titles)}")
    except Exception as e: 
        print(f"[PASSO 1 - BACKEND CHAPTERS ERROR] Falha na leitura de capítulos: {e}")
    return titles