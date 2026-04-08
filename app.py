"""
World Conquest v3 — Full-Featured Multiplayer Strategy Game
Run:  pip install flask && python app.py
Open: http://localhost:5000
Admin: admin / admin123
Note: Username 'Kasper' (any case) auto-gets admin on registration
"""

from flask import Flask, request, jsonify, session, send_file
import sqlite3, hashlib, random, time, os, json, math
import urllib.request, threading, queue

# ── Spectator tracking ────────────────────────────────────────────────────────
_spectators = {}   # ip -> {flag, country, last_seen}
_geo_cache  = {}   # ip -> {flag, country}  — persists for process lifetime
_geo_queue  = queue.Queue()   # IPs to geo-lookup, processed by one background thread

def _country_flag(code):
    if not code or len(code) != 2: return '🌐'
    try: return chr(0x1F1E6+ord(code[0].upper())-65)+chr(0x1F1E6+ord(code[1].upper())-65)
    except: return '🌐'

def _geo_worker():
    """Single long-lived thread that processes geo lookups from the queue."""
    while True:
        try:
            ip = _geo_queue.get(timeout=60)
            if ip in _geo_cache:
                _geo_queue.task_done(); continue
            if ip in ('127.0.0.1', '::1', ''):
                _geo_cache[ip] = {'flag':'🖥','country':'Localhost','city':''}
                _geo_queue.task_done(); continue
            try:
                url = f'http://ip-api.com/json/{ip}?fields=countryCode,country,city,status'
                with urllib.request.urlopen(url, timeout=4) as r:
                    data = json.loads(r.read())
                if data.get('status') == 'success':
                    _geo_cache[ip] = {'flag':_country_flag(data['countryCode']), 'country':data.get('country','?'), 'city':data.get('city','')}
                else:
                    _geo_cache[ip] = {'flag':'🌐','country':'Unknown'}
            except:
                _geo_cache[ip] = {'flag':'🌐','country':'Unknown'}
            _geo_queue.task_done()
        except queue.Empty:
            continue
        except Exception:
            try: _geo_queue.task_done()
            except: pass

# Start one persistent worker thread (not a new thread per request)
_geo_thread = threading.Thread(target=_geo_worker, daemon=True)
_geo_thread.start()

def _touch_spectator(ip):
    """Record a spectator visit. Geo lookup is async via queue."""
    geo = _geo_cache.get(ip, {'flag':'🌐','country':'?'})
    _spectators[ip] = {**geo, 'last_seen': time.time()}
    if ip not in _geo_cache:
        try: _geo_queue.put_nowait(ip)
        except queue.Full: pass


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'wc_v3_secret_xK9m_2024_!@#')

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'game.db'))

# ── Game Constants ────────────────────────────────────────────────────────────
GRID            = 0.18     # degrees per cell (~20 km)
TROOP_COST      = 8        # money per troop
BOAT_COST       = 800      # money per boat (single-use overseas landing)
PLANE_COST      = 1200     # money per plane (single-use overseas strike)
AUTO_COLLECT_CD = 10       # seconds between auto-accruals per territory
MAX_ACCUM_MINS  = 120      # max offline accrual cap (2hrs)
WIN_THRESHOLD   = 150      # territories to win a round
WIN_COUNTDOWN   = 45       # seconds before game resets after win
CLAIM_COST      = 25       # base claim cost (scales with territory count)
BOAT_RANGE      = 4        # max cells for naval attack
PLANE_RANGE_DEF = 5        # default plane range (cells)
PLANE_RANGE_BLZ = 8        # plane range with blitzkrieg

AUTO_ADMIN_NAMES = {'kasper'}
SELL_RATES = {'food': 2, 'wood': 4, 'metal': 6, 'oil': 10}

TERRAIN_RES = {
    'plains':    ('food',   9),
    'forest':    ('wood',   12),
    'mountains': ('metal',  9),
    'desert':    ('money',  7),
    'tundra':    ('metal',  5),
    'city':      ('money',  18),
    'oil':       ('oil',    14),
}

POP_BASE  = {'city':80000,'plains':3000,'forest':1200,'mountains':800,'desert':300,'tundra':150,'oil':900}
POP_RANGE = {'city':420000,'plains':17000,'forest':8000,'mountains':3200,'desert':1700,'tundra':850,'oil':6100}

RANKS = [
    (0,   '🪓', 'Settler'),
    (3,   '⚔',  'Warrior'),
    (10,  '🛡', 'Commander'),
    (25,  '🏰', 'Warlord'),
    (60,  '👑', 'Emperor'),
    (150, '🌍', 'Conqueror'),
]

RESEARCH_TREE = {
    'agri':      {'name':'Agriculture',      'icon':'🌾','cost':100,'branch':'economy', 'requires':[],           'desc':'+25% food & wood yield'},
    'trade':     {'name':'Trade Routes',     'icon':'💹','cost':150,'branch':'economy', 'requires':['agri'],     'desc':'+15% money yield'},
    'industry':  {'name':'Industrialization','icon':'⚙','cost':250,'branch':'economy', 'requires':['trade'],    'desc':'+25% metal & oil yield'},
    'iron':      {'name':'Iron Weapons',     'icon':'⚔','cost':100,'branch':'military','requires':[],           'desc':'+20% attack strength'},
    'castle':    {'name':'Castle Walls',     'icon':'🏰','cost':100,'branch':'military','requires':[],           'desc':'+30% defense bonus'},
    'gunpowder': {'name':'Gunpowder',        'icon':'💥','cost':250,'branch':'military','requires':['iron'],     'desc':'+30% attack, -20% troop cost'},
    'shipyard':  {'name':'Shipbuilding',     'icon':'⚓','cost':200,'branch':'naval',   'requires':[],           'desc':'Unlocks Boats (cross-water attacks)'},
    'airforce':  {'name':'Air Force',        'icon':'✈','cost':400,'branch':'naval',   'requires':['shipyard'], 'desc':'Unlocks Planes (long-range attacks)'},
    'blitz':     {'name':'Blitzkrieg',       'icon':'⚡','cost':600,'branch':'naval',   'requires':['airforce'], 'desc':'Planes range +3, +20% power'},
}

PLAYER_COLORS = [
    '#e74c3c','#3498db','#2ecc71','#9b59b6','#e67e22','#1abc9c',
    '#e91e63','#00bcd4','#ff5722','#8bc34a','#ff9800','#f06292',
    '#4db6ac','#aed581','#ba68c8','#d35400','#16a085','#8e44ad',
    '#c0392b','#27ae60',
]

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT UNIQUE NOT NULL COLLATE NOCASE,
        password    TEXT NOT NULL,
        is_admin    INTEGER DEFAULT 0,
        is_banned   INTEGER DEFAULT 0,
        reset_pin   TEXT DEFAULT NULL,
        research    TEXT DEFAULT '[]',
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen   INTEGER DEFAULT 0,
        last_claim  INTEGER DEFAULT 0,
        food        REAL DEFAULT 100,
        wood        REAL DEFAULT 100,
        metal       REAL DEFAULT 100,
        oil         REAL DEFAULT 25,
        money       REAL DEFAULT 200,
        color       TEXT DEFAULT '#e74c3c'
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS territories (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        grid_key       TEXT UNIQUE NOT NULL,
        owner_id       INTEGER REFERENCES users(id),
        terrain        TEXT NOT NULL,
        garrison       INTEGER DEFAULT 0,
        boats          INTEGER DEFAULT 0,
        planes         INTEGER DEFAULT 0,
        population     INTEGER DEFAULT 0,
        last_collected INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS announcements (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        message    TEXT NOT NULL,
        image_url  TEXT DEFAULT NULL,
        author     TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS battle_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        attacker   TEXT NOT NULL,
        defender   TEXT NOT NULL,
        grid_key   TEXT NOT NULL,
        result     TEXT NOT NULL,
        mode       TEXT DEFAULT 'land',
        details    TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS game_settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        type       TEXT NOT NULL,
        message    TEXT NOT NULL,
        data       TEXT DEFAULT '{}',
        is_read    INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS alliances (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL REFERENCES users(id),
        target_id    INTEGER NOT NULL REFERENCES users(id),
        status       TEXT DEFAULT 'pending',
        created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(requester_id, target_id)
    )''')

    # Seed admin
    ph = hashlib.sha256('admin123'.encode()).hexdigest()
    c.execute('''INSERT OR IGNORE INTO users
                 (username,password,is_admin,food,wood,metal,oil,money,color)
                 VALUES (?,?,1,9999,9999,9999,9999,99999,'#ffd700')''', ('admin', ph))

    c.execute('''INSERT OR IGNORE INTO announcements (id,message,author)
                 VALUES (1,'🌍 Welcome to World Conquest! Claim your first territory to begin.','System')''')

    conn.commit(); conn.close()

# ── Pure helpers ──────────────────────────────────────────────────────────────

def simple_hash(glat, glng):
    s = f"{glat},{glng}"; h = 0
    for ch in s: h = (h*31 + ord(ch)) % 10007
    return h

def get_terrain(glat, glng):
    h = simple_hash(glat, glng); lat = glat * GRID
    if abs(lat) > 65:      return 'tundra'
    elif abs(lat) > 55:    opts=['tundra','tundra','forest','mountains','plains']
    elif abs(lat) > 40:    opts=['plains','plains','forest','forest','mountains','city']
    elif abs(lat) > 20:    opts=['plains','desert','desert','mountains','city','oil','forest']
    else:                  opts=['forest','forest','forest','plains','desert','city','oil']
    return opts[h % len(opts)]

def get_population(terrain, glat, glng):
    h = simple_hash(glat, glng)
    return POP_BASE[terrain] + (h % POP_RANGE[terrain])

def parse_key(k):
    p = k.split(','); return int(p[0]), int(p[1])

def adj_keys(gl, gg):
    return [f"{gl+dl},{gg+dg}" for dl in (-1,0,1) for dg in (-1,0,1) if dl or dg]

def cell_distance(k1, k2):
    """Chebyshev distance between two grid keys."""
    a, b = parse_key(k1); c, d = parse_key(k2)
    return max(abs(a-c), abs(b-d))

def get_rank(tc):
    r = RANKS[0]
    for threshold, icon, name in RANKS:
        if tc >= threshold: r = (threshold, icon, name)
    return {'icon': r[1], 'name': r[2]}

def ph(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM game_settings WHERE key=?',(key,)).fetchone()
    return row['value'] if row else default

def set_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO game_settings(key,value) VALUES(?,?)',(key,str(value)))

def are_allied(uid1, uid2, conn):
    """Return True if uid1 and uid2 have an active alliance."""
    row = conn.execute(
        "SELECT 1 FROM alliances WHERE status='active' AND "
        "((requester_id=? AND target_id=?) OR (requester_id=? AND target_id=?))",
        (uid1, uid2, uid2, uid1)
    ).fetchone()
    return row is not None

def create_notification(conn, user_id, ntype, message, data=None):
    conn.execute(
        'INSERT INTO notifications (user_id, type, message, data) VALUES (?,?,?,?)',
        (user_id, ntype, message, json.dumps(data or {}))
    )

# ── Research helpers ───────────────────────────────────────────────────────────

def user_research(conn, uid):
    row = conn.execute('SELECT research FROM users WHERE id=?',(uid,)).fetchone()
    try: return set(json.loads(row['research'] or '[]'))
    except: return set()

def res_mult(res_type, research):
    m = 1.0
    if res_type in ('food','wood') and 'agri' in research: m *= 1.25
    if res_type == 'money' and 'trade' in research: m *= 1.15
    if res_type in ('metal','oil') and 'industry' in research: m *= 1.25
    return m

def atk_mult(research):
    m = 1.0
    if 'iron' in research: m *= 1.20
    if 'gunpowder' in research: m *= 1.30
    return m

def def_bonus(research):
    b = 1.25
    if 'castle' in research: b *= 1.30
    return b

def troop_cost(research):
    c = TROOP_COST
    if 'gunpowder' in research: c = max(1, int(c * 0.75))  # 25% discount
    return c

# ── Banning / auth ────────────────────────────────────────────────────────────

def touch_last_seen(uid):
    try:
        conn = get_db()
        conn.execute('UPDATE users SET last_seen=? WHERE id=?',(int(time.time()),uid))
        conn.commit(); conn.close()
    except: pass

def require_login(f):
    from functools import wraps
    @wraps(f)
    def wrap(*a, **kw):
        if 'user_id' not in session:
            return jsonify({'error':'Not authenticated'}),401
        conn = get_db()
        u = conn.execute('SELECT is_banned FROM users WHERE id=?',(session['user_id'],)).fetchone()
        conn.close()
        if not u:
            session.clear()
            return jsonify({'error':'Account not found'}),401
        if u['is_banned']:
            session.clear()
            return jsonify({'error':'🚫 Your account has been banned'}),403
        touch_last_seen(session['user_id'])
        return f(*a, **kw)
    return wrap

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def wrap(*a, **kw):
        if 'user_id' not in session:
            return jsonify({'error':'Not authenticated'}),401
        conn = get_db()
        u = conn.execute('SELECT is_admin,is_banned FROM users WHERE id=?',(session['user_id'],)).fetchone()
        conn.close()
        if not u or u['is_banned']:
            session.clear()
            return jsonify({'error':'Banned or not found'}),403
        if not u['is_admin']:
            return jsonify({'error':'Admin required'}),403
        touch_last_seen(session['user_id'])
        return f(*a, **kw)
    return wrap

# ── Auto resource collection ──────────────────────────────────────────────────

def auto_collect(uid, conn):
    now = int(time.time())
    rsch = user_research(conn, uid)
    rows = conn.execute(
        'SELECT grid_key,terrain,last_collected FROM territories WHERE owner_id=?',(uid,)
    ).fetchall()
    totals = {'food':0.,'wood':0.,'metal':0.,'oil':0.,'money':0.}
    updated = []
    for row in rows:
        elapsed = now - (row['last_collected'] or 0)
        if elapsed < AUTO_COLLECT_CD: continue
        rt, rate = TERRAIN_RES[row['terrain']]
        minutes  = min(elapsed/60., MAX_ACCUM_MINS)
        totals[rt] += rate * res_mult(rt, rsch) * minutes
        updated.append(row['grid_key'])
    if updated:
        for k in updated:
            conn.execute('UPDATE territories SET last_collected=? WHERE grid_key=?',(now,k))
        sets = ','.join(f'{r}={r}+?' for r in totals)
        conn.execute(f'UPDATE users SET {sets} WHERE id=?',list(totals.values())+[uid])

# ── Win-condition check ───────────────────────────────────────────────────────

def check_win(uid, conn):
    """Win when a single player reaches WIN_THRESHOLD territories."""
    existing = get_setting(conn,'winner_id')
    if existing: return False
    # Find the player with the most territories
    leader = conn.execute('''
        SELECT u.id, u.username, COUNT(t.id) tc FROM users u
        JOIN territories t ON t.owner_id=u.id
        WHERE u.is_banned=0
        GROUP BY u.id
        ORDER BY tc DESC LIMIT 1
    ''').fetchone()
    if not leader or leader['tc'] < WIN_THRESHOLD:
        return False
    set_setting(conn,'winner_id', leader['id'])
    set_setting(conn,'winner_name', leader['username'])
    set_setting(conn,'win_time', int(time.time()))
    return True

def do_game_reset(conn):
    conn.execute('UPDATE territories SET owner_id=NULL,garrison=0,boats=0,planes=0')
    conn.execute('UPDATE users SET food=100,wood=100,metal=100,oil=25,money=200,research=\'[]\'')
    conn.execute("DELETE FROM game_settings WHERE key IN ('winner_id','winner_name','win_time')")
    conn.execute('DELETE FROM battle_log')
    conn.execute("INSERT OR IGNORE INTO announcements (message,author) VALUES ('🔄 A new round has started! Claim territories and conquer the world.','System')")

# ── Static ────────────────────────────────────────────────────────────────────

@app.route('/')
def index(): return send_file('index.html')

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    d  = request.json or {}
    un = d.get('username','').strip()
    pw = d.get('password','')
    pin= d.get('reset_pin','').strip()
    if not un or not pw: return jsonify({'error':'Username and password required'}),400
    if len(un)<3 or len(un)>20: return jsonify({'error':'Username must be 3–20 characters'}),400
    if len(pw)<4: return jsonify({'error':'Password must be at least 4 characters'}),400
    if pin and (not pin.isdigit() or len(pin)<4 or len(pin)>8):
        return jsonify({'error':'Reset PIN must be 4–8 digits'}),400
    is_admin = 1 if un.lower() in AUTO_ADMIN_NAMES else 0
    color    = random.choice(PLAYER_COLORS)
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username,password,color,is_admin,reset_pin) VALUES (?,?,?,?,?)',
            (un, ph(pw), color, is_admin, pin or None)
        )
        conn.commit(); conn.close()
        msg = '✅ Account created!'
        if is_admin: msg += ' Admin privileges granted.'
        return jsonify({'success':True,'message':msg,'is_admin':bool(is_admin)})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error':'Username already taken — each name can only be registered once.'}),409

@app.route('/api/login', methods=['POST'])
def login():
    d  = request.json or {}
    un = d.get('username','')
    pw = d.get('password','')
    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE username=? COLLATE NOCASE AND password=?',(un, ph(pw))
    ).fetchone()
    conn.close()
    if not user: return jsonify({'error':'Invalid username or password'}),401
    if user['is_banned']:
        return jsonify({'error':'🚫 This account has been banned'}),403
    session['user_id']  = user['id']
    session['username'] = user['username']
    touch_last_seen(user['id'])
    return jsonify({'success':True,'user':{
        'id':user['id'],'username':user['username'],
        'is_admin':bool(user['is_admin']),'color':user['color']
    }})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success':True})

@app.route('/api/forgot_password', methods=['POST'])
def forgot_password():
    d   = request.json or {}
    un  = d.get('username','').strip()
    pin = d.get('reset_pin','').strip()
    pw  = d.get('new_password','')
    if not un or not pin or not pw:
        return jsonify({'error':'Username, PIN, and new password required'}),400
    if len(pw)<4:
        return jsonify({'error':'Password must be at least 4 characters'}),400
    conn = get_db()
    user = conn.execute(
        'SELECT id,reset_pin FROM users WHERE username=? COLLATE NOCASE',(un,)
    ).fetchone()
    if not user or not user['reset_pin']:
        conn.close()
        return jsonify({'error':'No reset PIN set for this account. Contact an admin.'}),404
    if user['reset_pin'] != pin:
        conn.close()
        return jsonify({'error':'Incorrect PIN'}),401
    conn.execute('UPDATE users SET password=? WHERE id=?',(ph(pw), user['id']))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Password reset successfully!'})

@app.route('/api/me')
@require_login
def me():
    conn = get_db()
    auto_collect(session['user_id'], conn); conn.commit()
    u  = conn.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    tc = conn.execute('SELECT COUNT(*) c FROM territories WHERE owner_id=?',(session['user_id'],)).fetchone()['c']
    pop= conn.execute('SELECT COALESCE(SUM(population),0) p FROM territories WHERE owner_id=?',(session['user_id'],)).fetchone()['p']
    rsch = list(json.loads(u['research'] or '[]'))
    rank = get_rank(tc)
    claim_ready_in = 0
    # Fetch active/pending alliances
    uid = session['user_id']
    ally_rows = conn.execute('''
        SELECT a.id, a.status,
               r.id r_id, r.username r_name, r.color r_color,
               t.id t_id, t.username t_name, t.color t_color
        FROM alliances a
        JOIN users r ON a.requester_id=r.id
        JOIN users t ON a.target_id=t.id
        WHERE (a.requester_id=? OR a.target_id=?) AND a.status IN ('active','pending')
    ''', (uid, uid)).fetchall()
    alliances = []
    for row in ally_rows:
        is_req    = (row['r_id'] == uid)
        ally_id   = row['t_id']    if is_req else row['r_id']
        ally_name = row['t_name']  if is_req else row['r_name']
        ally_col  = row['t_color'] if is_req else row['r_color']
        alliances.append({'id': row['id'], 'status': row['status'],
                          'ally_id': ally_id, 'ally_name': ally_name,
                          'ally_color': ally_col, 'is_requester': is_req})
    conn.close()
    return jsonify({
        'id':u['id'],'username':u['username'],
        'is_admin':bool(u['is_admin']),'color':u['color'],
        'food':round(u['food']),'wood':round(u['wood']),
        'metal':round(u['metal']),'oil':round(u['oil']),'money':round(u['money']),
        'territory_count':tc,'population':int(pop),
        'research':rsch,'rank':rank,
        'has_pin':bool(u['reset_pin']),
        'claim_ready_in': claim_ready_in,
        'alliances': alliances,
    })

# ── Profile ───────────────────────────────────────────────────────────────────

@app.route('/api/profile/change_password', methods=['POST'])
@require_login
def change_password():
    d    = request.json or {}
    curr = d.get('current_password','')
    new  = d.get('new_password','')
    if not curr or not new: return jsonify({'error':'Both passwords required'}),400
    if len(new)<4: return jsonify({'error':'New password must be at least 4 characters'}),400
    conn = get_db()
    u = conn.execute('SELECT password FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if u['password'] != ph(curr):
        conn.close()
        return jsonify({'error':'Current password is incorrect'}),401
    conn.execute('UPDATE users SET password=? WHERE id=?',(ph(new), session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Password changed successfully!'})

@app.route('/api/profile/change_username', methods=['POST'])
@require_login
def change_username():
    d   = request.json or {}
    new = d.get('new_username','').strip()
    pw  = d.get('password','')
    if not new or not pw: return jsonify({'error':'New username and password required'}),400
    if len(new)<3 or len(new)>20: return jsonify({'error':'Username must be 3–20 characters'}),400
    conn = get_db()
    u = conn.execute('SELECT password FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if u['password'] != ph(pw):
        conn.close()
        return jsonify({'error':'Incorrect password'}),401
    # Block auto-admin names for other users
    if new.lower() in AUTO_ADMIN_NAMES:
        existing = conn.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',(new,)).fetchone()
        if existing and existing['id'] != session['user_id']:
            conn.close()
            return jsonify({'error':'Username taken'}),409
    try:
        conn.execute('UPDATE users SET username=? WHERE id=?',(new, session['user_id']))
        conn.commit(); conn.close()
        session['username'] = new
        return jsonify({'success':True,'message':f'Username changed to {new}!'})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error':'Username already taken'}),409

@app.route('/api/profile/set_pin', methods=['POST'])
@require_login
def set_pin():
    d   = request.json or {}
    pin = d.get('pin','').strip()
    pw  = d.get('password','')
    if not pin or not pw: return jsonify({'error':'PIN and password required'}),400
    if not pin.isdigit() or len(pin)<4 or len(pin)>8:
        return jsonify({'error':'PIN must be 4–8 digits'}),400
    conn = get_db()
    u = conn.execute('SELECT password FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if u['password'] != ph(pw):
        conn.close()
        return jsonify({'error':'Incorrect password'}),401
    conn.execute('UPDATE users SET reset_pin=? WHERE id=?',(pin, session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Recovery PIN set!'})

# ── Online players ────────────────────────────────────────────────────────────

@app.route('/api/spectate', methods=['POST'])
def spectate():
    """Called by guests to register their presence on the map."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if ip:
        _touch_spectator(ip)  # fast: just dict update, geo is queued async
    return jsonify({'success': True})

@app.route('/api/online')
def online_users():
    cutoff = int(time.time()) - 180
    conn = get_db()
    rows = conn.execute('''
        SELECT u.username,u.color,u.is_admin,COUNT(t.id) territories
        FROM users u LEFT JOIN territories t ON t.owner_id=u.id
        WHERE u.last_seen>? AND u.is_banned=0
        GROUP BY u.id ORDER BY u.last_seen DESC
    ''',(cutoff,)).fetchall()
    conn.close()
    players = [{'username':r['username'],'color':r['color'],
                'is_admin':bool(r['is_admin']),'territories':r['territories'],
                'type':'player'} for r in rows]
    # Add active spectators (last 3 min, not logged-in players)
    player_ips = set()  # we don't track player IPs, just avoid double-count
    now = time.time()
    guests = []
    for ip, s in list(_spectators.items()):
        if now - s['last_seen'] < 180:
            guests.append({'username': f"{s['flag']} {ip}", 'color':'#607090',
                           'is_admin':False,'territories':0,'type':'spectator',
                           'flag': s['flag'], 'country': s.get('country','?'),
                           'city': s.get('city',''), 'ip': ip})
    return jsonify(players + guests)

# ── Sell resources ────────────────────────────────────────────────────────────

@app.route('/api/resources/sell', methods=['POST'])
@require_login
def sell_resources():
    d  = request.json or {}
    rt = d.get('resource','')
    am = max(1, int(d.get('amount',1)))
    if rt not in SELL_RATES: return jsonify({'error':'Invalid resource'}),400
    conn = get_db()
    u = conn.execute(f'SELECT {rt} FROM users WHERE id=?',(session['user_id'],)).fetchone()
    have = round(u[rt])
    if have < am:
        conn.close()
        return jsonify({'error':f'Not enough {rt}. Have {have}, need {am}'}),400
    earned = am * SELL_RATES[rt]
    conn.execute(f'UPDATE users SET {rt}={rt}-?,money=money+? WHERE id=?',(am,earned,session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success':True,'earned':earned,'message':f'Sold {am} {rt} for {earned}💰'})

# ── Research ──────────────────────────────────────────────────────────────────

@app.route('/api/research')
@require_login
def get_research():
    conn = get_db()
    rsch = user_research(conn, session['user_id'])
    conn.close()
    return jsonify({'research': list(rsch), 'tree': RESEARCH_TREE})

@app.route('/api/research/unlock', methods=['POST'])
@require_login
def unlock_research():
    d    = request.json or {}
    tech = d.get('tech','')
    if tech not in RESEARCH_TREE:
        return jsonify({'error':'Unknown technology'}),400
    info = RESEARCH_TREE[tech]
    conn = get_db()
    rsch = user_research(conn, session['user_id'])
    if tech in rsch:
        conn.close()
        return jsonify({'error':'Already researched'}),400
    for req in info['requires']:
        if req not in rsch:
            conn.close()
            return jsonify({'error':f'Requires {RESEARCH_TREE[req]["name"]} first'}),400
    u = conn.execute('SELECT money FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if round(u['money']) < info['cost']:
        conn.close()
        return jsonify({'error':f'Need {info["cost"]}💰, have {round(u["money"])}💰'}),400
    rsch.add(tech)
    conn.execute('UPDATE users SET research=?,money=money-? WHERE id=?',
                 (json.dumps(list(rsch)), info['cost'], session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':f'Researched {info["name"]}!'})

# ── Territories ───────────────────────────────────────────────────────────────

@app.route('/api/territories')
def get_territories():
    conn = get_db()
    rows = conn.execute('''
        SELECT t.grid_key,t.owner_id,t.terrain,t.garrison,t.boats,t.planes,t.population,
               u.username,u.color
        FROM territories t LEFT JOIN users u ON t.owner_id=u.id
        WHERE t.owner_id IS NOT NULL
    ''').fetchall()
    conn.close()
    return jsonify([{'grid_key':r['grid_key'],'owner_id':r['owner_id'],
                     'owner':r['username'],'color':r['color'] or '#888',
                     'terrain':r['terrain'],'garrison':r['garrison'],
                     'boats':r['boats'],'planes':r['planes'],'population':r['population']} for r in rows])

@app.route('/api/territory/<path:grid_key>')
def territory_detail(grid_key):
    conn = get_db()
    row  = conn.execute('''
        SELECT t.*,u.username,u.color FROM territories t
        LEFT JOIN users u ON t.owner_id=u.id WHERE t.grid_key=?
    ''',(grid_key,)).fetchone()
    conn.close()
    if row:
        return jsonify({'grid_key':row['grid_key'],'owner_id':row['owner_id'],
                        'owner':row['username'],'color':row['color'],
                        'terrain':row['terrain'],'garrison':row['garrison'],
                        'boats':row['boats'],'planes':row['planes'],
                        'population':row['population'],'last_collected':row['last_collected']})
    try:
        gl, gg  = parse_key(grid_key)
        terrain = get_terrain(gl, gg)
        pop     = get_population(terrain, gl, gg)
        return jsonify({'grid_key':grid_key,'owner_id':None,'owner':None,
                        'terrain':terrain,'garrison':0,'boats':0,'planes':0,
                        'population':pop,'last_collected':0})
    except: return jsonify({'error':'Invalid grid key'}),400

@app.route('/api/territory/claim', methods=['POST'])
@require_login
def claim_territory():
    d  = request.json or {}
    gk = d.get('grid_key','').strip()
    if not gk: return jsonify({'error':'grid_key required'}),400
    try: gl, gg = parse_key(gk)
    except: return jsonify({'error':'Invalid grid_key'}),400

    conn = get_db()
    existing = conn.execute('SELECT owner_id FROM territories WHERE grid_key=?',(gk,)).fetchone()
    if existing and existing['owner_id']:
        conn.close(); return jsonify({'error':'Territory already owned'}),409

    # Claim cost check (1000 if player owns 300+ territories, else 30)
    mc = conn.execute('SELECT COUNT(*) c FROM territories WHERE owner_id=?',(session['user_id'],)).fetchone()['c']
    # Tiered claim cost — scales to slow late-game snowball
    if mc >= 200:   cost = 1200
    elif mc >= 100: cost = 400
    elif mc >= 50:  cost = 150
    elif mc >= 20:  cost = 80
    elif mc >= 8:   cost = 40
    else:           cost = CLAIM_COST
    user_money = conn.execute('SELECT money FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if round(user_money['money']) < cost:
        conn.close(); return jsonify({'error':f'Need {cost}💰 to claim (you have {round(user_money["money"])}💰)'}),400

    if mc > 0:
        ak = adj_keys(gl, gg)
        owned_adj = conn.execute(
            f'SELECT COUNT(*) c FROM territories WHERE owner_id=? AND grid_key IN ({",".join("?"*len(ak))})',
            [session['user_id']]+ak
        ).fetchone()['c']
        if owned_adj == 0:
            conn.close(); return jsonify({'error':'Must be adjacent to one of your territories'}),400

    terrain = get_terrain(gl, gg)
    pop     = get_population(terrain, gl, gg)
    now     = int(time.time())
    if existing:
        conn.execute('UPDATE territories SET owner_id=?,garrison=3,boats=0,planes=0,population=?,last_collected=? WHERE grid_key=?',
                     (session['user_id'],pop,now,gk))
    else:
        conn.execute('INSERT INTO territories (grid_key,owner_id,terrain,garrison,boats,planes,population,last_collected) VALUES (?,?,?,3,0,0,?,?)',
                     (gk,session['user_id'],terrain,pop,now))

    conn.execute('UPDATE users SET money=money-? WHERE id=?',(cost,session['user_id']))
    check_win(session['user_id'], conn)
    conn.commit(); conn.close()
    return jsonify({'success':True,'terrain':terrain,'population':pop,'message':f'Territory claimed! ({terrain})','cost':cost})

# ── Troops ────────────────────────────────────────────────────────────────────

@app.route('/api/troops/build', methods=['POST'])
@require_login
def build_troops():
    d  = request.json or {}
    gk = d.get('grid_key','')
    am = max(1, min(int(d.get('amount',1)), 500))
    conn = get_db()
    if not conn.execute('SELECT 1 FROM territories WHERE grid_key=? AND owner_id=?',(gk,session['user_id'])).fetchone():
        conn.close(); return jsonify({'error':'You do not own this territory'}),403
    rsch = user_research(conn, session['user_id'])
    cost = am * troop_cost(rsch)
    u = conn.execute('SELECT money FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if round(u['money']) < cost:
        conn.close(); return jsonify({'error':f'Need {cost}💰, have {round(u["money"])}💰'}),400
    conn.execute('UPDATE users SET money=money-? WHERE id=?',(cost,session['user_id']))
    conn.execute('UPDATE territories SET garrison=garrison+? WHERE grid_key=?',(am,gk))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':f'Recruited {am} troops for {cost}💰'})

@app.route('/api/troops/move', methods=['POST'])
@require_login
def move_troops():
    d    = request.json or {}
    fk   = d.get('from_key','')
    tk   = d.get('to_key','')
    am   = max(1, int(d.get('amount',1)))
    try:
        fl,fg = parse_key(fk); tl,tg = parse_key(tk)
        if max(abs(fl-tl),abs(fg-tg)) > 1: return jsonify({'error':'Not adjacent'}),400
    except: return jsonify({'error':'Invalid keys'}),400
    conn = get_db()
    uid = session['user_id']
    ft = conn.execute('SELECT garrison, owner_id FROM territories WHERE grid_key=?',(fk,)).fetchone()
    tt = conn.execute('SELECT owner_id FROM territories WHERE grid_key=?',(tk,)).fetchone()
    if not ft:
        conn.close(); return jsonify({'error':'Source territory not found'}),404
    ft_owner = ft['owner_id']
    tt_owner = tt['owner_id'] if tt else None
    # Allow move if you own from OR it belongs to an ally
    if ft_owner != uid and not (ft_owner and are_allied(uid, ft_owner, conn)):
        conn.close(); return jsonify({'error':'You do not have access to this territory'}),403
    # Allow move to your territory OR an allied territory
    if tt_owner != uid and not (tt_owner and are_allied(uid, tt_owner, conn)):
        conn.close(); return jsonify({'error':'Target must be your territory or an ally\'s'}),403
    if ft['garrison'] - am < 1:
        conn.close(); return jsonify({'error':'Must leave at least 1 troop behind'}),400
    conn.execute('UPDATE territories SET garrison=garrison-? WHERE grid_key=?',(am,fk))
    conn.execute('UPDATE territories SET garrison=garrison+? WHERE grid_key=?',(am,tk))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':f'Moved {am} troops'})

# ── Boats ─────────────────────────────────────────────────────────────────────

@app.route('/api/boats/build', methods=['POST'])
@require_login
def build_boats():
    d  = request.json or {}
    gk = d.get('grid_key','')
    am = max(1, min(int(d.get('amount',1)), 50))
    conn = get_db()
    if not conn.execute('SELECT 1 FROM territories WHERE grid_key=? AND owner_id=?',(gk,session['user_id'])).fetchone():
        conn.close(); return jsonify({'error':'You do not own this territory'}),403
    cost = am * BOAT_COST
    u = conn.execute('SELECT money FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if round(u['money']) < cost:
        conn.close(); return jsonify({'error':f'Need {cost}💰, have {round(u["money"])}💰'}),400
    conn.execute('UPDATE users SET money=money-? WHERE id=?',(cost,session['user_id']))
    conn.execute('UPDATE territories SET boats=boats+? WHERE grid_key=?',(am,gk))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':f'Built {am} boat(s) for {cost}💰 — ready for overseas landing!'})

@app.route('/api/boats/attack', methods=['POST'])
@require_login
def boats_attack():
    """Single-use overseas naval landing. Boats are fully consumed on launch."""
    d          = request.json or {}
    from_key   = d.get('from_key','').strip()
    target_key = d.get('target_key','').strip()
    boats_sent = max(1, int(d.get('boats',1)))

    conn = get_db()
    ft = conn.execute('SELECT * FROM territories WHERE grid_key=? AND owner_id=?',(from_key,session['user_id'])).fetchone()
    if not ft:
        conn.close(); return jsonify({'error':'You do not own the launching territory'}),403
    if ft['boats'] < boats_sent:
        conn.close(); return jsonify({'error':f'Only have {ft["boats"]} boat(s), need {boats_sent}'}),400

    dist = cell_distance(from_key, target_key)
    if dist <= 1:
        conn.close(); return jsonify({'error':'Boats are for overseas (non-adjacent) territories only. Use land attack for adjacent ones.'}),400

    # Check not own territory
    target_t = conn.execute('SELECT * FROM territories WHERE grid_key=?',(target_key,)).fetchone()
    if target_t and target_t['owner_id']==session['user_id']:
        conn.close(); return jsonify({'error':'Cannot attack your own territory'}),400

    def_garrison = target_t['garrison'] if target_t else 0
    def_owner_id = target_t['owner_id'] if target_t else None
    def_name     = 'wilderness'
    if def_owner_id:
        if are_allied(session['user_id'], def_owner_id, conn):
            conn.close(); return jsonify({'error': '🤝 Cannot attack an ally!'}), 400
        du = conn.execute('SELECT username FROM users WHERE id=?',(def_owner_id,)).fetchone()
        def_name = du['username'] if du else 'unknown'

    try: gl,gg = parse_key(target_key)
    except: conn.close(); return jsonify({'error':'Invalid target key'}),400

    rsch     = user_research(conn, session['user_id'])
    atk_str  = boats_sent * random.uniform(0.8,1.4) * atk_mult(rsch) * 1.1
    def_str  = def_garrison * random.uniform(0.9,1.45) * def_bonus(rsch)

    # Boats are always fully consumed (single-use)
    conn.execute('UPDATE territories SET boats=boats-? WHERE grid_key=?',(boats_sent,from_key))

    if atk_str > def_str:
        # Troops that "landed" — boats become garrison on arrival
        landed = max(1, boats_sent - max(0, int(boats_sent*random.uniform(0.1,0.3))))
        terrain = target_t['terrain'] if target_t else get_terrain(gl,gg)
        pop     = target_t['population'] if target_t else get_population(terrain,gl,gg)
        if target_t:
            conn.execute('UPDATE territories SET owner_id=?,garrison=?,boats=0,planes=0,last_collected=? WHERE grid_key=?',
                         (session['user_id'],landed,int(time.time()),target_key))
        else:
            conn.execute('INSERT INTO territories (grid_key,owner_id,terrain,garrison,boats,planes,population,last_collected) VALUES (?,?,?,?,0,0,?,?)',
                         (target_key,session['user_id'],terrain,landed,pop,int(time.time())))
        result = 'victory'
        msg    = f'⚓ Naval Landing! Seized {def_name}. {landed} troops landed from {boats_sent} boats.'
        check_win(session['user_id'], conn)
    else:
        def_losses = max(0, int(def_garrison*random.uniform(0.1,0.3)))
        if target_t: conn.execute('UPDATE territories SET garrison=MAX(1,garrison-?) WHERE grid_key=?',(def_losses,target_key))
        result = 'defeat'
        msg    = f'⚓ Naval Repelled! {boats_sent} boat(s) lost. {def_name} held their ground.'

    conn.execute('INSERT INTO battle_log (attacker,defender,grid_key,result,mode,details) VALUES (?,?,?,?,?,?)',
                 (session['username'],def_name,target_key,result,'naval',f'{boats_sent} boats vs {def_garrison} garrison'))
    conn.commit(); conn.close()
    return jsonify({'success':True,'attacker_wins':result=='victory','result':result,'message':msg})

# ── Planes ────────────────────────────────────────────────────────────────────

@app.route('/api/planes/build', methods=['POST'])
@require_login
def build_planes():
    d  = request.json or {}
    gk = d.get('grid_key','')
    am = max(1, min(int(d.get('amount',1)), 50))
    conn = get_db()
    if not conn.execute('SELECT 1 FROM territories WHERE grid_key=? AND owner_id=?',(gk,session['user_id'])).fetchone():
        conn.close(); return jsonify({'error':'You do not own this territory'}),403
    cost = am * PLANE_COST
    u = conn.execute('SELECT money FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if round(u['money']) < cost:
        conn.close(); return jsonify({'error':f'Need {cost}💰, have {round(u["money"])}💰'}),400
    conn.execute('UPDATE users SET money=money-? WHERE id=?',(cost,session['user_id']))
    conn.execute('UPDATE territories SET planes=planes+? WHERE grid_key=?',(am,gk))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':f'Built {am} plane(s) for {cost}💰 — ready for overseas strike!'})

@app.route('/api/planes/attack', methods=['POST'])
@require_login
def planes_attack():
    """Single-use overseas air strike. Planes are fully consumed on launch."""
    d          = request.json or {}
    from_key   = d.get('from_key','').strip()
    target_key = d.get('target_key','').strip()
    planes_sent= max(1, int(d.get('planes',1)))

    conn = get_db()
    ft = conn.execute('SELECT * FROM territories WHERE grid_key=? AND owner_id=?',(from_key,session['user_id'])).fetchone()
    if not ft:
        conn.close(); return jsonify({'error':'You do not own the launching territory'}),403
    if ft['planes'] < planes_sent:
        conn.close(); return jsonify({'error':f'Only have {ft["planes"]} plane(s), need {planes_sent}'}),400

    dist = cell_distance(from_key, target_key)
    if dist <= 1:
        conn.close(); return jsonify({'error':'Planes are for overseas (non-adjacent) territories only. Use land attack for adjacent ones.'}),400

    target_t = conn.execute('SELECT * FROM territories WHERE grid_key=?',(target_key,)).fetchone()
    if target_t and target_t['owner_id']==session['user_id']:
        conn.close(); return jsonify({'error':'Cannot attack your own territory'}),400

    def_garrison = target_t['garrison'] if target_t else 0
    def_owner_id = target_t['owner_id'] if target_t else None
    def_name     = 'wilderness'
    if def_owner_id:
        if are_allied(session['user_id'], def_owner_id, conn):
            conn.close(); return jsonify({'error': '🤝 Cannot attack an ally!'}), 400
        du = conn.execute('SELECT username FROM users WHERE id=?',(def_owner_id,)).fetchone()
        def_name = du['username'] if du else 'unknown'

    try: gl,gg = parse_key(target_key)
    except: conn.close(); return jsonify({'error':'Invalid target key'}),400

    rsch    = user_research(conn, session['user_id'])
    atk_str = planes_sent * random.uniform(0.85,1.45) * atk_mult(rsch) * 1.3
    def_str = def_garrison * random.uniform(0.9,1.4) * def_bonus(rsch)

    # Planes fully consumed (single-use)
    conn.execute('UPDATE territories SET planes=planes-? WHERE grid_key=?',(planes_sent,from_key))

    if atk_str > def_str:
        # Some planes survive to act as garrison
        landed = max(1, planes_sent - max(0, int(planes_sent*random.uniform(0.1,0.35))))
        terrain = target_t['terrain'] if target_t else get_terrain(gl,gg)
        pop     = target_t['population'] if target_t else get_population(terrain,gl,gg)
        if target_t:
            conn.execute('UPDATE territories SET owner_id=?,garrison=?,boats=0,planes=0,last_collected=? WHERE grid_key=?',
                         (session['user_id'],landed,int(time.time()),target_key))
        else:
            conn.execute('INSERT INTO territories (grid_key,owner_id,terrain,garrison,boats,planes,population,last_collected) VALUES (?,?,?,?,0,0,?,?)',
                         (target_key,session['user_id'],terrain,landed,pop,int(time.time())))
        result = 'victory'
        msg    = f'✈ Air Strike! Seized {def_name}. {landed} troops deployed from {planes_sent} planes.'
        check_win(session['user_id'], conn)
    else:
        def_losses = max(0, int(def_garrison*random.uniform(0.05,0.2)))
        if target_t: conn.execute('UPDATE territories SET garrison=MAX(1,garrison-?) WHERE grid_key=?',(def_losses,target_key))
        result = 'defeat'
        msg    = f'✈ Strike Failed! {planes_sent} plane(s) lost. {def_name} repelled the attack.'

    conn.execute('INSERT INTO battle_log (attacker,defender,grid_key,result,mode,details) VALUES (?,?,?,?,?,?)',
                 (session['username'],def_name,target_key,result,'air',f'{planes_sent} planes vs {def_garrison} garrison'))
    conn.commit(); conn.close()
    return jsonify({'success':True,'attacker_wins':result=='victory','result':result,'message':msg})

# ── Land attack ───────────────────────────────────────────────────────────────

@app.route('/api/attack', methods=['POST'])
@require_login
def attack():
    d    = request.json or {}
    fk   = d.get('from_key','').strip()
    tk   = d.get('target_key','').strip()
    sent = max(1, int(d.get('troops',1)))
    if not fk or not tk or fk==tk: return jsonify({'error':'Invalid parameters'}),400
    try:
        fl,fg = parse_key(fk); tl,tg = parse_key(tk)
        if max(abs(fl-tl),abs(fg-tg)) > 1: return jsonify({'error':'Target must be adjacent'}),400
    except: return jsonify({'error':'Invalid keys'}),400

    conn = get_db()
    ft   = conn.execute('SELECT * FROM territories WHERE grid_key=? AND owner_id=?',(fk,session['user_id'])).fetchone()
    if not ft: conn.close(); return jsonify({'error':'You do not own the attacking territory'}),403
    if ft['garrison'] < sent+1: conn.close(); return jsonify({'error':f'Need {sent+1} troops (keep 1), have {ft["garrison"]}'}),400

    tt = conn.execute('SELECT * FROM territories WHERE grid_key=?',(tk,)).fetchone()
    if tt and tt['owner_id']==session['user_id']: conn.close(); return jsonify({'error':'Cannot attack your own territory'}),400

    defg     = tt['garrison'] if tt else 0
    def_oid  = tt['owner_id'] if tt else None
    def_name = 'wilderness'
    if def_oid:
        if are_allied(session['user_id'], def_oid, conn):
            conn.close(); return jsonify({'error': '🤝 Cannot attack an ally!'}), 400
        du = conn.execute('SELECT username,research FROM users WHERE id=?',(def_oid,)).fetchone()
        def_name = du['username'] if du else 'unknown'
        def_rsch = set(json.loads(du['research'] or '[]')) if du else set()
    else:
        def_rsch = set()

    rsch     = user_research(conn, session['user_id'])
    atk_str  = sent * random.uniform(0.75,1.35) * atk_mult(rsch)
    def_str  = defg * random.uniform(0.90,1.45) * def_bonus(def_rsch)
    wins     = atk_str > def_str

    if wins:
        losses    = max(1, int(sent*random.uniform(0.20,0.50)))
        survivors = max(1, sent-losses)
        conn.execute('UPDATE territories SET garrison=garrison-? WHERE grid_key=?',(sent,fk))
        terrain = tt['terrain'] if tt else get_terrain(tl,tg)
        pop     = tt['population'] if tt else get_population(terrain,tl,tg)
        if tt:
            conn.execute('UPDATE territories SET owner_id=?,garrison=?,boats=0,planes=0,last_collected=? WHERE grid_key=?',
                         (session['user_id'],survivors,int(time.time()),tk))
        else:
            conn.execute('INSERT INTO territories (grid_key,owner_id,terrain,garrison,boats,planes,population,last_collected) VALUES (?,?,?,?,0,0,?,?)',
                         (tk,session['user_id'],terrain,survivors,pop,int(time.time())))
        result = 'victory'
        msg    = f'⚔ Victory! Conquered {def_name}. Lost {losses}, {survivors} survived.'
        check_win(session['user_id'], conn)
    else:
        dl = max(0, int(defg*random.uniform(0.10,0.35)))
        conn.execute('UPDATE territories SET garrison=garrison-? WHERE grid_key=?',(sent,fk))
        if tt: conn.execute('UPDATE territories SET garrison=MAX(1,garrison-?) WHERE grid_key=?',(dl,tk))
        result = 'defeat'
        msg    = f'💀 Defeat! Lost all {sent} troops attacking {def_name}.'

    conn.execute('INSERT INTO battle_log (attacker,defender,grid_key,result,mode,details) VALUES (?,?,?,?,?,?)',
                 (session['username'],def_name,tk,result,'land',f'{sent} vs {defg}'))
    conn.commit(); conn.close()
    return jsonify({'success':True,'attacker_wins':wins,'result':result,'message':msg})

# ── Notifications ─────────────────────────────────────────────────────────────

@app.route('/api/notifications')
@require_login
def get_notifications():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM notifications WHERE user_id=? AND is_read=0 ORDER BY created_at DESC LIMIT 30',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify([{**dict(r), 'data': json.loads(r['data'] or '{}')} for r in rows])

@app.route('/api/notifications/dismiss', methods=['POST'])
@require_login
def dismiss_notification():
    d   = request.json or {}
    nid = d.get('id')
    conn = get_db()
    conn.execute('UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?', (nid, session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/notifications/dismiss_all', methods=['POST'])
@require_login
def dismiss_all_notifications():
    conn = get_db()
    conn.execute('UPDATE notifications SET is_read=1 WHERE user_id=?', (session['user_id'],))
    conn.commit(); conn.close()
    return jsonify({'success': True})

# ── Gift ──────────────────────────────────────────────────────────────────────

@app.route('/api/gift/send', methods=['POST'])
@require_login
def gift_money():
    d           = request.json or {}
    to_username = d.get('to_username', '').strip()
    try:        amount = max(1, int(d.get('amount', 0)))
    except:     return jsonify({'error': 'Invalid amount'}), 400
    if amount > 1_000_000: return jsonify({'error': 'Amount too large'}), 400
    conn = get_db()
    recipient = conn.execute(
        'SELECT id, username FROM users WHERE username=? COLLATE NOCASE AND is_banned=0', (to_username,)
    ).fetchone()
    if not recipient:
        conn.close(); return jsonify({'error': 'Player not found'}), 404
    if recipient['id'] == session['user_id']:
        conn.close(); return jsonify({'error': 'Cannot gift yourself'}), 400
    sender = conn.execute('SELECT username, money FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if round(sender['money']) < amount:
        conn.close(); return jsonify({'error': f'Not enough money (have {round(sender["money"])}💰)'}), 400
    conn.execute('UPDATE users SET money=money-? WHERE id=?', (amount, session['user_id']))
    conn.execute('UPDATE users SET money=money+? WHERE id=?', (amount, recipient['id']))
    create_notification(conn, recipient['id'], 'gift',
                        f'💰 {sender["username"]} gifted you {amount:,}💰!',
                        {'from': sender['username'], 'amount': amount})
    conn.commit(); conn.close()
    return jsonify({'success': True, 'message': f'Gifted {amount:,}💰 to {recipient["username"]}!'})

# ── Alliance ──────────────────────────────────────────────────────────────────

@app.route('/api/alliance/invite', methods=['POST'])
@require_login
def alliance_invite():
    d               = request.json or {}
    target_username = d.get('username', '').strip()
    conn = get_db()
    target = conn.execute(
        'SELECT id, username FROM users WHERE username=? COLLATE NOCASE AND is_banned=0', (target_username,)
    ).fetchone()
    if not target:
        conn.close(); return jsonify({'error': 'Player not found'}), 404
    if target['id'] == session['user_id']:
        conn.close(); return jsonify({'error': 'Cannot invite yourself'}), 400
    existing = conn.execute(
        'SELECT * FROM alliances WHERE (requester_id=? AND target_id=?) OR (requester_id=? AND target_id=?)',
        (session['user_id'], target['id'], target['id'], session['user_id'])
    ).fetchone()
    if existing:
        if existing['status'] == 'active':
            conn.close(); return jsonify({'error': 'Already allied with this player'}), 400
        if existing['status'] == 'pending':
            conn.close(); return jsonify({'error': 'Alliance invite already pending'}), 400
        conn.execute('DELETE FROM alliances WHERE id=?', (existing['id'],))
    sender = conn.execute('SELECT username FROM users WHERE id=?', (session['user_id'],)).fetchone()
    cur    = conn.execute(
        'INSERT INTO alliances (requester_id, target_id, status) VALUES (?,?,?)',
        (session['user_id'], target['id'], 'pending')
    )
    aid = cur.lastrowid
    create_notification(conn, target['id'], 'alliance_invite',
                        f'🤝 {sender["username"]} wants to form an alliance with you!',
                        {'from': sender['username'], 'from_id': session['user_id'], 'alliance_id': aid})
    conn.commit(); conn.close()
    return jsonify({'success': True, 'message': f'Alliance invite sent to {target["username"]}!'})

@app.route('/api/alliance/respond', methods=['POST'])
@require_login
def alliance_respond():
    d          = request.json or {}
    alliance_id= d.get('alliance_id')
    accept     = bool(d.get('accept', False))
    conn = get_db()
    alliance = conn.execute(
        "SELECT * FROM alliances WHERE id=? AND target_id=? AND status='pending'",
        (alliance_id, session['user_id'])
    ).fetchone()
    if not alliance:
        conn.close(); return jsonify({'error': 'Invite not found or already responded'}), 404
    me        = conn.execute('SELECT username FROM users WHERE id=?', (session['user_id'],)).fetchone()
    requester = conn.execute('SELECT username FROM users WHERE id=?', (alliance['requester_id'],)).fetchone()
    if accept:
        conn.execute("UPDATE alliances SET status='active' WHERE id=?", (alliance_id,))
        create_notification(conn, alliance['requester_id'], 'alliance_accepted',
                            f'🤝 {me["username"]} accepted your alliance!',
                            {'from': me['username'], 'from_id': session['user_id'], 'alliance_id': alliance_id})
        msg = f'Alliance formed with {requester["username"]}!'
    else:
        conn.execute("UPDATE alliances SET status='declined' WHERE id=?", (alliance_id,))
        create_notification(conn, alliance['requester_id'], 'alliance_declined',
                            f'❌ {me["username"]} declined your alliance invite.',
                            {'from': me['username']})
        msg = f'Declined alliance with {requester["username"]}.'
    conn.commit(); conn.close()
    return jsonify({'success': True, 'message': msg})

@app.route('/api/alliance/list')
@require_login
def list_alliances():
    conn = get_db()
    uid  = session['user_id']
    rows = conn.execute('''
        SELECT a.id, a.status, a.created_at,
               r.id r_id, r.username r_name, r.color r_color,
               t.id t_id, t.username t_name, t.color t_color
        FROM alliances a
        JOIN users r ON a.requester_id=r.id
        JOIN users t ON a.target_id=t.id
        WHERE (a.requester_id=? OR a.target_id=?) AND a.status IN ('active','pending')
        ORDER BY a.created_at DESC
    ''', (uid, uid)).fetchall()
    conn.close()
    result = []
    for row in rows:
        is_req    = (row['r_id'] == uid)
        ally_id   = row['t_id']   if is_req else row['r_id']
        ally_name = row['t_name'] if is_req else row['r_name']
        ally_col  = row['t_color']if is_req else row['r_color']
        result.append({
            'id': row['id'], 'status': row['status'],
            'ally_id': ally_id, 'ally_name': ally_name, 'ally_color': ally_col,
            'is_requester': is_req, 'created_at': row['created_at'],
        })
    return jsonify(result)

@app.route('/api/alliance/break', methods=['POST'])
@require_login
def break_alliance():
    d   = request.json or {}
    aid = d.get('alliance_id')
    conn = get_db()
    alliance = conn.execute(
        "SELECT * FROM alliances WHERE id=? AND status='active' AND (requester_id=? OR target_id=?)",
        (aid, session['user_id'], session['user_id'])
    ).fetchone()
    if not alliance:
        conn.close(); return jsonify({'error': 'Alliance not found'}), 404
    me       = conn.execute('SELECT username FROM users WHERE id=?', (session['user_id'],)).fetchone()
    other_id = alliance['target_id'] if alliance['requester_id'] == session['user_id'] else alliance['requester_id']
    conn.execute('DELETE FROM alliances WHERE id=?', (aid,))
    create_notification(conn, other_id, 'alliance_broken',
                        f'💔 {me["username"]} dissolved your alliance.',
                        {'from': me['username']})
    conn.commit(); conn.close()
    return jsonify({'success': True, 'message': 'Alliance dissolved.'})

@app.route('/api/alliance/cancel', methods=['POST'])
@require_login
def cancel_alliance_invite():
    d   = request.json or {}
    aid = d.get('alliance_id')
    conn = get_db()
    alliance = conn.execute(
        "SELECT * FROM alliances WHERE id=? AND requester_id=? AND status='pending'",
        (aid, session['user_id'])
    ).fetchone()
    if not alliance:
        conn.close(); return jsonify({'error': 'Pending invite not found'}), 404
    conn.execute('DELETE FROM alliances WHERE id=?', (aid,))
    conn.commit(); conn.close()
    return jsonify({'success': True, 'message': 'Invite cancelled.'})

# ── Leaderboard / battle log / announcements ──────────────────────────────────

@app.route('/api/leaderboard')
def leaderboard():
    conn = get_db()
    rows = conn.execute('''
        SELECT u.username,u.color,u.is_admin,
               COUNT(t.id) territories,
               COALESCE(SUM(t.population),0) total_pop, u.money
        FROM users u LEFT JOIN territories t ON t.owner_id=u.id
        WHERE u.is_banned=0
        GROUP BY u.id ORDER BY territories DESC LIMIT 20
    ''').fetchall()
    conn.close()
    result = []
    for r in rows:
        rank = get_rank(r['territories'])
        result.append({**dict(r),'rank':rank})
    return jsonify(result)

@app.route('/api/battle_log')
@require_login
def battle_log():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM battle_log WHERE attacker=? OR defender=? ORDER BY created_at DESC LIMIT 30',
        (session['username'],session['username'])
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/battle_log/all')
def battle_log_all():
    conn = get_db()
    rows = conn.execute('SELECT * FROM battle_log ORDER BY created_at DESC LIMIT 30').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/announcements')
def get_announcements():
    conn = get_db()
    rows = conn.execute('SELECT * FROM announcements ORDER BY id DESC LIMIT 10').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── Game status (win condition) ───────────────────────────────────────────────

@app.route('/api/game/status')
def game_status():
    conn = get_db()
    wid   = get_setting(conn,'winner_id')
    wname = get_setting(conn,'winner_name')
    wtime = get_setting(conn,'win_time')
    if wid and wtime:
        elapsed = time.time() - float(wtime)
        if elapsed >= WIN_COUNTDOWN:
            do_game_reset(conn); conn.commit(); conn.close()
            return jsonify({'status':'reset','message':'A new round has started!'})
        conn.close()
        return jsonify({'status':'winner','winner':wname,
                        'reset_in': WIN_COUNTDOWN-int(elapsed),
                        'threshold': WIN_THRESHOLD})
    conn.close()
    # Check current leader
    conn2 = get_db()
    leader = conn2.execute('''
        SELECT u.username, COUNT(t.id) tc FROM users u
        LEFT JOIN territories t ON t.owner_id=u.id
        WHERE u.is_banned=0 GROUP BY u.id ORDER BY tc DESC LIMIT 1
    ''').fetchone()
    conn2.close()
    leader_info = {'name':leader['username'],'count':leader['tc']} if leader else None
    return jsonify({'status':'playing','threshold':WIN_THRESHOLD,'leader':leader_info})

# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route('/api/admin/users')
@require_admin
def admin_users():
    conn = get_db()
    rows = conn.execute('''
        SELECT u.id,u.username,u.is_admin,u.is_banned,u.created_at,u.money,u.color,u.last_seen,
               COUNT(t.id) territory_count, COALESCE(SUM(t.population),0) total_pop
        FROM users u LEFT JOIN territories t ON t.owner_id=u.id
        GROUP BY u.id ORDER BY u.created_at DESC
    ''').fetchall()
    conn.close()
    now = int(time.time())
    return jsonify([{**dict(r),'online':(now-(r['last_seen'] or 0))<180} for r in rows])

@app.route('/api/admin/ban', methods=['POST'])
@require_admin
def admin_ban():
    d = request.json or {}
    uid = d.get('user_id'); ban = 1 if d.get('ban',True) else 0
    conn = get_db()
    # Prevent banning self or other admins
    target = conn.execute('SELECT is_admin,username FROM users WHERE id=?',(uid,)).fetchone()
    if not target:
        conn.close(); return jsonify({'error':'User not found'}),404
    if target['is_admin'] and target['username'].lower()=='admin':
        conn.close(); return jsonify({'error':'Cannot ban the main admin'}),403
    conn.execute('UPDATE users SET is_banned=? WHERE id=?',(ban,uid))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/admin/change_username', methods=['POST'])
@require_admin
def admin_change_username():
    d   = request.json or {}
    uid = d.get('user_id')
    new = d.get('new_username','').strip()
    if not new or len(new)<3 or len(new)>20:
        return jsonify({'error':'Username must be 3–20 characters'}),400
    conn = get_db()
    try:
        conn.execute('UPDATE users SET username=? WHERE id=?',(new,uid))
        conn.commit(); conn.close()
        return jsonify({'success':True,'message':f'Username changed to {new}'})
    except sqlite3.IntegrityError:
        conn.close(); return jsonify({'error':'Username already taken'}),409

@app.route('/api/admin/reset_password', methods=['POST'])
@require_admin
def admin_reset_password():
    d   = request.json or {}
    uid = d.get('user_id')
    new = d.get('new_password','')
    if not new or len(new)<4:
        return jsonify({'error':'Password must be at least 4 characters'}),400
    conn = get_db()
    conn.execute('UPDATE users SET password=? WHERE id=?',(ph(new),uid))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Password reset successfully'})

@app.route('/api/admin/promote', methods=['POST'])
@require_admin
def admin_promote():
    d   = request.json or {}
    uid = d.get('user_id'); val = 1 if d.get('promote',True) else 0
    conn = get_db()
    conn.execute('UPDATE users SET is_admin=? WHERE id=?',(val,uid))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/admin/give_money', methods=['POST'])
@require_admin
def admin_give_money():
    d      = request.json or {}
    uid    = d.get('user_id')
    try:   amount = max(1, int(d.get('amount', 0)))
    except: return jsonify({'error': 'Invalid amount'}), 400
    conn = get_db()
    target = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    if not target:
        conn.close(); return jsonify({'error': 'User not found'}), 404
    conn.execute('UPDATE users SET money=money+? WHERE id=?', (amount, uid))
    create_notification(conn, uid, 'gift',
                        f'💰 Admin granted you {amount:,}💰!',
                        {'from': 'Admin', 'amount': amount})
    conn.commit(); conn.close()
    return jsonify({'success': True, 'message': f'Gave {amount:,}💰 to {target["username"]}'})

@app.route('/api/admin/announce', methods=['POST'])
@require_admin
def admin_announce():
    d   = request.json or {}
    msg = d.get('message','').strip()
    img = d.get('image_url','').strip() or None
    if not msg: return jsonify({'error':'Message required'}),400
    conn = get_db()
    cur  = conn.execute('INSERT INTO announcements (message,image_url,author) VALUES (?,?,?)',
                        (msg, img, session['username']))
    aid  = cur.lastrowid
    conn.commit(); conn.close()
    return jsonify({'success':True,'id':aid})

@app.route('/api/admin/delete_announcement', methods=['POST'])
@require_admin
def admin_del_ann():
    d = request.json or {}
    aid = d.get('id')
    conn = get_db()
    conn.execute('DELETE FROM announcements WHERE id=?',(aid,))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/admin/remove_territories', methods=['POST'])
@require_admin
def admin_remove_territories():
    d = request.json or {}; uid = d.get('user_id')
    conn = get_db()
    conn.execute('UPDATE territories SET owner_id=NULL,garrison=0,boats=0,planes=0 WHERE owner_id=?',(uid,))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/admin/reset_game', methods=['POST'])
@require_admin
def admin_reset_game():
    conn = get_db(); do_game_reset(conn); conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Game has been reset!'})

# ── DB backup (admin) ────────────────────────────────────────────────────────

@app.route('/api/admin/export_db')
@require_admin
def export_db():
    import io
    conn = get_db()
    buf = io.BytesIO()
    for chunk in conn.iterdump():
        buf.write((chunk + '\n').encode())
    conn.close(); buf.seek(0)
    from flask import send_file as _sf
    return _sf(buf, as_attachment=True, download_name='world_conquest_backup.sql', mimetype='text/plain')

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print("\n" + "="*56)
    print("  ⚔   World Conquest v3")
    print("="*56)
    print("  URL     :  http://localhost:5000")
    print("  Admin   :  admin / admin123")
    print("  Auto-mod:  Register as 'Kasper' for admin")
    print(f"  Win at  :  {WIN_THRESHOLD} territories")
    print("="*56 + "\n")
    port = int(os.environ.get('PORT', 5000))
    # use_reloader=False avoids multiprocessing semaphore leaks in dev
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=False)
