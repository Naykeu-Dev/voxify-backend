# ===================================================
# FIX DEFINITIVO DE SSL — DEVE SER A PRIMEIRA COISA DO ARQUIVO
# Força o Python/urllib3/yt-dlp a usarem o bundle de certificados
# do certifi, resolvendo o CERTIFICATE_VERIFY_FAILED no Render.
# ===================================================
import os
import certifi

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["CURL_CA_BUNDLE"] = certifi.where()  # cobre libcurl/pycurl usados por algumas libs internas

from flask import Flask, request, jsonify, send_file, send_from_directory, redirect
from flask import Response
from flask_cors import CORS
import shutil, io, zipfile, time, re, json, random
import urllib.request
import urllib.parse
import yt_dlp
import database as db

# ===================================================
# BYPASS GLOBAL DE VERIFICAÇÃO SSL — VERSÃO CORRIGIDA
# IMPORTANTE: ssl._create_default_https_context SÓ afeta urllib.request
# puro (stdlib). O yt-dlp usa por baixo urllib3/requests, que chamam a
# função PÚBLICA ssl.create_default_context() — por isso o fix anterior
# não resolvia o erro no extractor youtube:search.
# Aqui patcheamos as DUAS, cobrindo qualquer biblioteca HTTP usada.
# ===================================================
import ssl

try:
    # 1) Cobre urllib.request puro (stdlib)
    ssl._create_default_https_context = ssl._create_unverified_context

    # 2) Cobre urllib3 / requests / yt-dlp (a função pública real usada por elas)
    _original_create_default_context = ssl.create_default_context

    def _patched_create_default_context(*args, **kwargs):
        ctx = _original_create_default_context(*args, **kwargs)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ssl.create_default_context = _patched_create_default_context

    print("[BOOT SUCCESS] Verificação global de certificados SSL desativada (urllib + urllib3/requests).")
except Exception as e:
    print(f"[BOOT WARNING] Falha ao desativar a validação de certificados SSL: {e}")

# ===================================================
# 3) PATCH DIRETO NO URLLIB3 — ESTE É O VERDADEIRO CULPADO
# O yt-dlp usa 'requests' como backend de rede por padrão nas versões
# recentes, e o 'requests' usa 'urllib3' por baixo. O urllib3 NÃO lê
# ssl.create_default_context() — ele tem sua própria função interna
# (urllib3.util.ssl_.create_urllib3_context). Sem patchear ela
# diretamente, o CERTIFICATE_VERIFY_FAILED persiste mesmo com
# nocheckcertificate=True no yt-dlp e com os patches acima.
# ===================================================
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    from urllib3.util import ssl_ as _urllib3_ssl_module

    _original_urllib3_create_context = _urllib3_ssl_module.create_urllib3_context

    def _patched_urllib3_create_context(*args, **kwargs):
        ctx = _original_urllib3_create_context(*args, **kwargs)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    _urllib3_ssl_module.create_urllib3_context = _patched_urllib3_create_context
    print("[BOOT SUCCESS] urllib3 SSL context patcheado com sucesso (cobre requests/yt-dlp).")
except ImportError:
    print("[BOOT INFO] urllib3 não encontrado no ambiente, pulando patch específico.")
except Exception as e:
    print(f"[BOOT WARNING] Falha ao patchear urllib3: {e}")

print(f"[BOOT] SSL_CERT_FILE apontando para: {os.environ.get('SSL_CERT_FILE')}")


# Importações corrigidas de downloader
from downloader import (
    fetch_results, get_ydl_opts, search_single, 
    generate_custom_playlist, generate_cd_playlist,
    download_with_fallback, verify_track_duration,
    TMP_DIR, BASE_DIR
)

# IMPORTADOS DO NOVO MÓDULO EXTRATOR ESPECIALIZADO
from extractor import (
    extract_video_chapters, shazam_extract_mix, 
    analyze_mix_markers_for_slicer, execute_slicer_cuts_physical,
    generate_youtube_mix
)

from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__, static_folder='../dist', static_url_path='')
CORS(app)
db.init_db()

# CHECAGEM DE AMBIENTE NO BOOT
import sys
print(f"[BOOT] Interpretador Python em uso: {sys.executable}")
try:
    import shazamio
    print(f"[BOOT] shazamio encontrado OK em: {shazamio.__file__}")
except ImportError:
    print("[BOOT ALERTA] shazamio NÃO está instalado neste interpretador. "
          "Rode: python -m pip install shazamio (usando este mesmo 'python').")

STORAGE_DIR = os.path.join(BASE_DIR, 'musicas_salvas')
if not os.path.exists(STORAGE_DIR): os.makedirs(STORAGE_DIR)

def fetch_lyrics_from_lrclib(query):
    clean_q = re.sub(r'(\[.*?\]|\(.*?\))', '', query)
    clean_q = re.sub(r'(official( music)? video|ao vivo|lyrics?|lyric video|ft\.|feat\.|participação|hd|4k)', '', clean_q, flags=re.IGNORECASE)
    clean_q = clean_q.strip()
    
    url = f"https://lrclib.net/api/search?q={urllib.parse.quote(clean_q)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MusicFlowPro/1.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data and isinstance(data, list) and len(data) > 0:
                for item in data:
                    if item.get('syncedLyrics') or item.get('plainLyrics'):
                        return item
                return data[0]
    except Exception as e:
        print(f"[Lyrics API Error] {e}")
    return None

@app.route('/lyrics', methods=['GET'])
def get_lyrics():
    query = request.args.get('q')
    if not query:
        return jsonify({'error': 'Falta o parâmetro de busca q'}), 400
    lyrics_data = fetch_lyrics_from_lrclib(query)
    if lyrics_data:
        return jsonify({
            'success': True,
            'plainLyrics': lyrics_data.get('plainLyrics'),
            'syncedLyrics': lyrics_data.get('syncedLyrics'),
            'trackName': lyrics_data.get('trackName'),
            'artistName': lyrics_data.get('artistName')
        })
    else:
        return jsonify({'success': False, 'error': 'Letra não encontrada'}), 404


def extract_spotify_tracks(playlist_url):
    match = re.search(r'playlist/([a-zA-Z0-9]+)', playlist_url)
    if not match:
        return []
    
    playlist_id = match.group(1)
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    try:
        req = urllib.request.Request(embed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read().decode('utf-8')
            json_matches = re.findall(r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)
            for json_str in json_matches:
                try:
                    data = json.loads(json_str)
                    
                    if 'tracks' in data:
                        items = data['tracks'].get('items', [])
                    elif 'resource' in data:
                        items = data['resource'].get('tracks', {}).get('items', [])
                    elif 'tracks' in data.get('state', {}):
                        items = data['state']['tracks'].get('items', [])
                    else:
                        continue
                        
                    queries = []
                    for item in items:
                        track = item.get('track', {}) if isinstance(item, dict) else {}
                        if not track: continue
                        name = track.get('name')
                        artists = track.get('artists', [])
                        artist_name = artists[0].get('name') if artists else ""
                        if name:
                            query = f"{artist_name} - {name}" if artist_name else name
                            queries.append(query)
                    if queries:
                        return queries
                except Exception:
                    continue
    except Exception as e:
        print(f"[Spotify Import Error] {e}")
    return []

@app.route('/import-spotify', methods=['POST'])
def import_spotify():
    url = request.json.get('url')
    if not url:
        return jsonify({'error': 'Falta a URL do Spotify'}), 400
        
    print(f"\n================ [SPOTIFY IMPORT REQUEST] ================")
    print(f"[LOG] URL recebida: {url}")
    
    queries = extract_spotify_tracks(url)
    if not queries:
        print("[LOG ERROR] Nenhuma música pôde ser extraída desta playlist.")
        return jsonify({'error': 'Não foi possível extrair músicas dessa playlist. Verifique se ela é pública.'}), 404
        
    queries = queries[:40]
    print(f"[LOG] Extraídas {len(queries)} faixas do Spotify. Buscando correspondências no YouTube...")
    results = fetch_results(queries)
    
    response = jsonify({'queries': queries, 'results': results})
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/')
def index(): return send_from_directory(app.static_folder, 'index.html')

@app.route('/search-single', methods=['GET'])
def search_single_route(): return jsonify(search_single(request.args.get('q')))


@app.route('/stream/<video_id>')
def stream_audio(video_id):
    print(f"\n================ 📥 [NOVA REQUISIÇÃO DE STREAM: {video_id}] ================")
    title = request.args.get('title')
    print(f"[PYTHON LOG] Título solicitado: {title}")
    
    if title:
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        permanent_path = os.path.join(STORAGE_DIR, f"{safe_title}.mp3")
        
        if os.path.exists(permanent_path):
            print(f"[PYTHON LOG] CACHE HIT! Enviando arquivo MP3 permanente local: {permanent_path}")
            return send_file(permanent_path)

    print(f"[PYTHON LOG] CACHE MISS. Extraindo link direto do YouTube via yt-dlp...")
    # Usa a mesma lógica centralizada do downloader.py (cookie + proxy + impersonate)
    from downloader import apply_common_opts
    
    opts = {
        'format': 'bestaudio/best', 
        'quiet': True, 
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'android', 'web_embedded'], 
                'player_skip': ['tv', 'web'] 
            }
        }
    }
    apply_common_opts(opts)
    print("[PYTHON LOG] Opções de cookie/proxy/impersonate aplicadas via downloader.py.")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            target_url = "https://www.youtube.com/watch?v=" + video_id
            info = ydl.extract_info(target_url, download=False)
            
            url = info.get('url')
            if not url and 'formats' in info:
                audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                if audio_formats:
                    url = audio_formats[-1]['url'] 
                else:
                    url = info['formats'][-1]['url'] 
            
            if url:
                print(f"[PYTHON LOG] Redirecionando navegador diretamente para o CDN de alta velocidade do Google!")
                return redirect(url)
            else:
                return jsonify({'error': 'Nenhuma URL extraída'}), 500
                
    except Exception as e: 
        print(f"[PYTHON LOG ERROR] Falha crítica de extração na Engine: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/extract-chapters', methods=['POST'])
def extract_chapters():
    return jsonify({'titles': extract_video_chapters(request.json.get('videoId'))})

@app.route('/shazam-mix', methods=['POST'])
def shazam_mix():
    titles, error_reason = shazam_extract_mix(request.json.get('videoId'))
    return jsonify({'titles': titles, 'error': error_reason})

# MOTOR SÔNICO UNIFICADO (STUDIO VS RECORTAR COM DAW MARCADORES)
@app.route('/extract-mix', methods=['POST'])
def extract_mix():
    data = request.json
    video_id = data.get('videoId')
    action = data.get('action', 'original')  # 'original' ou 'cut'
    
    print(f"\n================ 🪄 [REQUISIÇÃO EXTRACT MIX - MODO: {action.upper()}] ================")

    if action == 'cut':
        # ETAPA 1 DO SLICER: Apenas gera e sintoniza os marcadores estimados no terminal de edição manual
        slicer_meta, error_reason = analyze_mix_markers_for_slicer(video_id)
        if error_reason:
            return jsonify({'error': error_reason}), 500
        return jsonify(slicer_meta)
    else:
        # Modo estúdio original
        titles = extract_video_chapters(video_id)
        if len(titles) == 0:
            titles, error_reason = shazam_extract_mix(video_id)
            return jsonify({'titles': titles, 'error': error_reason})
        return jsonify({'titles': titles, 'error': None})

# ETAPA 2 DO SLICER: Recebe os marcadores finais ajustados manualmente pelo usuário e fatia fisicamente
@app.route('/execute-slice-cuts', methods=['POST'])
def execute_slice_cuts():
    data = request.json
    video_id = data.get('videoId')
    markers = data.get('markers', [])  # List de {"start": float, "end": float, "title": str}
    
    print(f"\n================ 🪄 [SLICER] EXECUÇÃO DE CORTES CIRÚRGICOS MANUAIS ({len(markers)} faixas) ================")
    
    tracks, error_reason = execute_slicer_cuts_physical(video_id, markers, STORAGE_DIR)
    if error_reason:
        return jsonify({'error': error_reason}), 500
    return jsonify({'tracks': tracks})

# HISTÓRICO DE RECORTE SÔNICO DO SQLITE
@app.route('/list-mix-markers')
def list_mix_markers():
    return jsonify(db.get_all_mix_history())

@app.route('/delete-mix-markers/<string:video_id>', methods=['DELETE'])
def delete_mix_markers(video_id):
    db.delete_mix_markers_db(video_id)
    return jsonify({'success': True})


@app.route('/generate-cd', methods=['POST'])
def gen_cd():
    artist = request.json.get('artist')
    limit = int(request.json.get('limit', 20))
    return jsonify(generate_cd_playlist(artist, limit))

@app.route('/rename-playlist/<int:id>', methods=['PUT'])
def rename_p(id):
    db.rename_playlist_db(id, request.json['name'])
    return jsonify({'success': True})

@app.route('/generate-similar', methods=['POST'])
def gen_similar(): return jsonify(generate_youtube_mix(request.json.get('videoId'), int(request.json.get('limit', 15))))

@app.route('/generate-custom', methods=['POST'])
def gen_custom(): return jsonify(generate_custom_playlist(request.json.get('query'), int(request.json.get('limit', 15))))

@app.route('/search-bulk', methods=['POST'])
def search(): return jsonify(fetch_results(request.json.get('queries', [])))

@app.route('/download-mp3', methods=['POST'])
def download_mp3():
    data = request.json
    v_id, title = data.get('videoId'), data.get('title', 'audio').replace('/', '_')
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    permanent_path = os.path.join(STORAGE_DIR, f"{safe_title}.mp3")

    if os.path.exists(permanent_path):
        buf = io.BytesIO()
        with open(permanent_path, 'rb') as f: buf.write(f.read())
        buf.seek(0)
        response = send_file(buf, as_attachment=True, download_name=f"{safe_title}.mp3")
        response.headers['X-Cache-Hit'] = 'true' 
        response.headers['Access-Control-Expose-Headers'] = 'X-Cache-Hit'
        return response

    work_dir = os.path.join(TMP_DIR, v_id)
    if not os.path.exists(work_dir): os.makedirs(work_dir)
    
    success = download_with_fallback(v_id, work_dir)

    if success:
        try:
            temp_file = os.path.join(work_dir, [f for f in os.listdir(work_dir) if f.endswith('.mp3')][0])
            shutil.copy2(temp_file, permanent_path) 
            
            buf = io.BytesIO()
            with open(temp_file, 'rb') as f: buf.write(f.read())
            buf.seek(0)
            shutil.rmtree(work_dir)
            return send_file(buf, as_attachment=True, download_name=f"{safe_title}.mp3")
        except Exception:
            if os.path.exists(work_dir): shutil.rmtree(work_dir)
            return jsonify({'error': 'Erro ao processar arquivo baixado'}), 500
    else:
        if os.path.exists(work_dir): shutil.rmtree(work_dir)
        return jsonify({'error': 'Falha no download após fallback'}), 500

@app.route('/download-zip', methods=['POST'])
def download_zip():
    data = request.json
    musics, playlist_name = data.get('musics', []), data.get('playlistName', 'MusicFlow_Playlist')
    safe_playlist_name = re.sub(r'[\\/*?:"<>|]', "_", playlist_name)
    if not musics: return jsonify({'error': 'Lista vazia'}), 400

    playlist_folder_path = os.path.join(STORAGE_DIR, safe_playlist_name)
    if not os.path.exists(playlist_folder_path): os.makedirs(playlist_folder_path)
    
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for m in musics:
            v_id, safe_title = m['videoId'], re.sub(r'[\\/*?:"<>|]', "", m['title'])
            
            general_path = os.path.join(STORAGE_DIR, f"{safe_title}.mp3")
            specific_path = os.path.join(playlist_folder_path, f"{safe_title}.mp3")
            
            target_existing_path = None
            if os.path.exists(general_path): target_existing_path = general_path
            elif os.path.exists(specific_path): target_existing_path = specific_path

            if target_existing_path:
                print(f"CACHE HIT: {safe_title} já existe! Puxando do HD.")
                if not os.path.exists(specific_path): shutil.copy2(target_existing_path, specific_path)
                zf.write(specific_path, f"{safe_playlist_name}/{safe_title}.mp3")
                continue 
            
            print(f"BAIXANDO: {safe_title} do YouTube...")
            work_dir = os.path.join(TMP_DIR, f"zip_{v_id}")
            if not os.path.exists(work_dir): os.makedirs(work_dir)
            time.sleep(2) 
            success = download_with_fallback(v_id, work_dir)
            
            if success:
                try:
                    temp_file = os.path.join(work_dir, [f for f in os.listdir(work_dir) if f.endswith('.mp3')][0])
                    shutil.copy2(temp_file, specific_path)
                    zf.write(specific_path, f"{safe_playlist_name}/{safe_title}.mp3")
                except Exception:
                    pass 
            
            if os.path.exists(work_dir): shutil.rmtree(work_dir)
            
    zip_buf.seek(0)
    response = send_file(zip_buf, mimetype='application/zip', as_attachment=True, download_name=f"{safe_playlist_name}.zip")
    response.headers['X-Cache-Hit'] = 'true' 
    response.headers['Access-Control-Expose-Headers'] = 'X-Cache-Hit'
    return response

@app.route('/save-playlist', methods=['POST'])
def save():
    data = request.json
    db.save_playlist_db(data['name'], data['musics'])
    return jsonify({'success': True})

@app.route('/list-playlists')
def list_p(): return jsonify(db.get_all_playlists())

@app.route('/get-playlist-musics/<int:id>')
def get_p_musics(id): return jsonify(db.get_playlist_musics(id))

@app.route('/delete-playlist/<int:id>', methods=['DELETE'])
def delete_p(id):
    db.delete_playlist_db(id)
    return jsonify({'success': True})

@app.route('/update-playlist/<int:id>', methods=['PUT'])
def update_p(id):
    musics = request.json.get('musics', [])
    db.append_to_playlist_db(id, musics)
    return jsonify({'success': True})

# CRIADOR MÁGICO COM INTELIGÊNCIA ARTIFICIAL
@app.route('/generate-magic', methods=['POST'])
def generate_magic():
    data = request.json
    mode = data.get('mode', 'prompt')  
    limit = int(data.get('limit', 20))
    exclude_ids = data.get('exclude_ids', []) 

    print(f"\n================ 🪄 [REQUISIÇÃO CRIADOR MÁGICO - MODO: {mode.upper()}] ================")

    if mode == 'playlist':
        playlist_id = data.get('playlist_id')
        if not playlist_id:
            return jsonify({'error': 'Nenhuma playlist selecionada.'}), 400

        playlist_tracks = db.get_playlist_musics(playlist_id)
        if len(playlist_tracks) < 2:
            return jsonify({'error': 'A playlist precisa ter no mínimo 2 músicas para permitir uma análise sônica.'}), 400

        for t in playlist_tracks:
            if t['videoId'] not in exclude_ids:
                exclude_ids.append(t['videoId'])

        print(f"[Voxify Magic] Analisando playlist: {len(playlist_tracks)} faixas. Excluindo duplicadas...")

        seed_count = min(4, len(playlist_tracks))
        seeds = random.sample(playlist_tracks, seed_count)
        
        candidates = []
        with ThreadPoolExecutor(max_workers=seed_count) as executor:
            futures = [executor.submit(generate_youtube_mix, s['videoId'], 15) for s in seeds]
            for f in futures:
                candidates.extend(f.result())

        seen_ids = set(exclude_ids)
        final_tracks = []
        for c in candidates:
            if c['videoId'] in seen_ids:
                continue

            title_lower = c['title'].lower()
            forbidden_terms = [
                'cover', 'covers', 'karaoke', 'karaokê', 'instrumental', 
                'acustico', 'acústico', 'set ', 'mix', 'dvd', 'completo', 
                'album', 'coletanea', 'coletânea', 'playlist', ' full ', 'complete'
            ]
            if any(term in title_lower for term in forbidden_terms):
                continue

            seen_ids.add(c['videoId'])
            final_tracks.append(c)

        print(f"[Voxify Magic] Sintonização sônica instantânea concluída de forma segura!")
        random.shuffle(final_tracks)
        return jsonify(final_tracks[:limit])

    elif mode == 'prompt':
        prompt = data.get('prompt', '')
        if not prompt:
            return jsonify({'error': 'O campo de busca/gênero não pode ficar vazio.'}), 400

        print(f"[Voxify Magic] Buscando do zero pelo prompt: '{prompt}'")
        results = generate_custom_playlist(prompt, limit * 2)

        filtered = [r for r in results if r['videoId'] not in exclude_ids][:limit]
        return jsonify(filtered)

    return jsonify({'error': 'Modo de criação inválido.'}), 400

if __name__ == '__main__': 
    # Habilitado host='0.0.0.0' para conexões externas na rede Wi-Fi local
    app.run(debug=True, host='0.0.0.0', port=5000)