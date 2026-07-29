# backend/database.py
import sqlite3
import json
from datetime import datetime

DB_NAME = 'playlists.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS playlists (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS playlist_musics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER,
        title TEXT, video_url TEXT,
        FOREIGN KEY(playlist_id) REFERENCES playlists(id))''')
    
    # Criador da tabela de marcadores persistidos
    c.execute('''CREATE TABLE IF NOT EXISTS mix_markers (
        video_id TEXT PRIMARY KEY,
        title TEXT,
        duration INTEGER,
        markers_json TEXT
    )''')
    
    # SISTEMA DE MIGRAÇÃO AUTOMÁTICA
    try:
        c.execute("SELECT thumbnail FROM playlist_musics LIMIT 1")
    except sqlite3.OperationalError:
        print("[MIGRAÇÃO DE BANCO] Adicionando coluna 'thumbnail' à tabela 'playlist_musics'...")
        c.execute("ALTER TABLE playlist_musics ADD COLUMN thumbnail TEXT")
        
    try:
        c.execute("SELECT video_id FROM playlist_musics LIMIT 1")
    except sqlite3.OperationalError:
        print("[MIGRAÇÃO DE BANCO] Adicionando coluna 'video_id' à tabela 'playlist_musics'...")
        c.execute("ALTER TABLE playlist_musics ADD COLUMN video_id TEXT")

    # Migração para incluir títulos nos marcadores sônicos
    try:
        c.execute("SELECT title FROM mix_markers LIMIT 1")
    except sqlite3.OperationalError:
        print("[MIGRAÇÃO DE BANCO] Adicionando coluna 'title' à tabela 'mix_markers'...")
        c.execute("ALTER TABLE mix_markers ADD COLUMN title TEXT")
        
    conn.commit()
    conn.close()

# GERENCIAMENTO DE HISTÓRICO SÔNICO
def save_mix_markers_db(video_id, title, duration, markers):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO mix_markers (video_id, title, duration, markers_json) VALUES (?,?,?,?)",
              (video_id, title, duration, json.dumps(markers)))
    conn.commit()
    conn.close()

def get_mix_markers_db(video_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT title, duration, markers_json FROM mix_markers WHERE video_id = ?", (video_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'videoId': video_id,
            'title': row[0] or "Mix sintonizado",
            'duration': row[1],
            'markers': json.loads(row[2])
        }
    return None

def get_all_mix_history():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT video_id, title, duration, markers_json FROM mix_markers ORDER BY video_id DESC")
    data = []
    for row in c.fetchall():
        data.append({
            'videoId': row[0],
            'title': row[1] or "Mix sintonizado",
            'duration': row[2],
            'markers': json.loads(row[3])
        })
    conn.close()
    return data

def delete_mix_markers_db(video_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM mix_markers WHERE video_id = ?", (video_id,))
    conn.commit()
    conn.close()

def save_playlist_db(name, musics):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO playlists (name, created_at) VALUES (?, ?)", (name, datetime.now().isoformat()))
    p_id = c.lastrowid
    for m in musics:
        c.execute("INSERT INTO playlist_musics (playlist_id, title, video_url, thumbnail, video_id) VALUES (?,?,?,?,?)",
                  (p_id, m['title'], m['url'], m['thumbnail'], m['videoId']))
    conn.commit()
    conn.close()
    return p_id

def append_to_playlist_db(p_id, musics):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM playlist_musics WHERE playlist_id=?", (p_id,))
    for m in musics:
        c.execute("INSERT INTO playlist_musics (playlist_id, title, video_url, thumbnail, video_id) VALUES (?,?,?,?,?)",
                  (p_id, m['title'], m['url'], m['thumbnail'], m['videoId']))
    conn.commit()
    conn.close()

def get_all_playlists():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, name, created_at FROM playlists ORDER BY created_at DESC")
    data = []
    for row in c.fetchall():
        c2 = conn.cursor()
        c2.execute("SELECT COUNT(*) FROM playlist_musics WHERE playlist_id=?", (row[0],))
        
        c3 = conn.cursor()
        c3.execute("SELECT thumbnail FROM playlist_musics WHERE playlist_id=? LIMIT 1", (row[0],))
        thumb_row = c3.fetchone()
        thumbnail = thumb_row[0] if thumb_row else None
        
        data.append({
            'id': row[0], 
            'name': row[1], 
            'created_at': row[2], 
            'count': c2.fetchone()[0],
            'thumbnail': thumbnail
        })
    conn.close()
    return data

def get_playlist_musics(p_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT title, video_url, thumbnail, video_id FROM playlist_musics WHERE playlist_id=?", (p_id,))
    rows = c.fetchall()
    conn.close()
    return [{'title': r[0], 'url': r[1], 'thumbnail': r[2], 'videoId': r[3]} for r in rows]

def rename_playlist_db(playlist_id, new_name):
    conn = sqlite3.connect('playlists.db')
    c = conn.cursor()
    c.execute("UPDATE playlists SET name = ? WHERE id = ?", (new_name, playlist_id))
    conn.commit()
    conn.close()
    
def delete_playlist_db(p_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM playlist_musics WHERE playlist_id=?", (p_id,))
    c.execute("DELETE FROM playlists WHERE id=?", (p_id,))
    conn.commit()
    conn.close()

def get_title_by_video_id(video_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT title FROM playlist_musics WHERE video_id = ? LIMIT 1", (video_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None