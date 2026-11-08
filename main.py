import os, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime, json, hashlib, hmac, threading
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from flask_socketio import SocketIO, emit

# Global process tracker
running_procs = {}
start_times = {}

# Initialize SocketIO
socketio = SocketIO()

def get_db():
    db_path = os.path.join(os.getcwd(), 'storage/LUCKY_host.db')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists('storage'): os.makedirs('storage')
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT, pfp TEXT DEFAULT 'default.png',
        role TEXT DEFAULT 'free', 
        status TEXT DEFAULT 'active',
        server_limit INTEGER DEFAULT 1,
        notifications TEXT DEFAULT '',
        expiry_at TEXT DEFAULT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        created_by TEXT DEFAULT NULL
    )''')
    user_columns = [row['name'] for row in db.execute('PRAGMA table_info(users)').fetchall()]
    if 'expiry_at' not in user_columns:
        db.execute('ALTER TABLE users ADD COLUMN expiry_at TEXT DEFAULT NULL')
    if 'created_at' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT ''")
    db.execute("UPDATE users SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL OR created_at=''")
    if 'created_by' not in user_columns:
        db.execute('ALTER TABLE users ADD COLUMN created_by TEXT DEFAULT NULL')
    db.execute('CREATE TABLE IF NOT EXISTS expired_accounts (user_id INTEGER PRIMARY KEY, email_token TEXT NOT NULL, username_token TEXT DEFAULT \"\", expired_at TEXT NOT NULL)')
    expired_columns = [row['name'] for row in db.execute('PRAGMA table_info(expired_accounts)').fetchall()]
    if 'username_token' not in expired_columns:
        db.execute("ALTER TABLE expired_accounts ADD COLUMN username_token TEXT DEFAULT ''")
    # Free accounts always receive one server slot; premium/admin-issued plans may have more.
    db.execute("UPDATE users SET server_limit=1 WHERE lower(COALESCE(role, 'free'))='free'")
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, name TEXT, folder TEXT, 
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active'
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY, 
        username TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')
    
    if not db.execute('SELECT * FROM admin_settings WHERE id=1').fetchone():
        db.execute('INSERT INTO admin_settings (id, username, password) VALUES (1, "redluckyxyz", "78657865")')
    
    db.commit()
    db.close()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET', 'LUCKY_host_ultra_pro_max_99')
    app.config['BASE_STORAGE'] = os.path.join(os.getcwd(), 'storage/instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')
    
    if not os.path.exists(app.config['BASE_STORAGE']):
        os.makedirs(app.config['BASE_STORAGE'])
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
        
    init_db()
    socketio.init_app(app)

    def parse_expiry(value):
        if not value:
            return None
        raw = str(value).strip()
        try:
            if len(raw) == 10:
                return datetime.datetime.combine(datetime.date.fromisoformat(raw), datetime.time(23, 59, 59))
            dt = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
            return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
        except (TypeError, ValueError):
            return None

    def make_email_token(email):
        return hmac.new(app.config['SECRET_KEY'].encode(), (email or '').strip().lower().encode(), hashlib.sha256).hexdigest()

    def write_json_file(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp.' + str(os.getpid()) + '.' + str(threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def sync_user_json(db):
        rows = db.execute('SELECT * FROM users ORDER BY id').fetchall()
        user_data, admin_data = [], []
        for u in rows:
            expiry = u['expiry_at'] or ''
            user_data.append({
                'user_id': u['id'], 'name': u['fname'] or '', 'username': u['username'] or '',
                'gmail': u['email'] or '',
                'password_hash': generate_password_hash(str(u['password'] or '')),
                'validity': 'no_expiry' if not expiry else expiry,
                'expiry_time': expiry, 'plan': u['role'] or 'free',
                'server_limit': u['server_limit'] or 1, 'status': u['status'] or 'active',
                'created_at': u['created_at'] or ''
            })
            if u['created_by']:
                admin_data.append({
                    'user_id': u['id'], 'name': u['fname'] or '', 'gmail': u['email'] or '',
                    'created_by': u['created_by'], 'created_at': u['created_at'] or '',
                    'plan': u['role'] or 'free', 'server_limit': u['server_limit'] or 1,
                    'validity': 'no_expiry' if not expiry else expiry, 'expiry_time': expiry
                })
        storage = os.path.join(os.getcwd(), 'storage')
        write_json_file(os.path.join(storage, 'user.json'), user_data)
        write_json_file(os.path.join(storage, 'admin_create.json'), admin_data)

    def remove_expired_accounts(db):
        now = datetime.datetime.now()
        rows = db.execute("SELECT id, email, username, pfp, expiry_at FROM users WHERE expiry_at IS NOT NULL AND TRIM(expiry_at) != ''").fetchall()
        expired = []
        for row in rows:
            expiry_dt = parse_expiry(row['expiry_at'])
            if expiry_dt and expiry_dt <= now:
                expired.append((row['id'], row['email'], row['username'], row['pfp']))
        for uid, email, username, pfp in expired:
            # Keep keyed fingerprints only, so the expired user can still see the notice without retaining PII.
            db.execute('INSERT OR REPLACE INTO expired_accounts (user_id, email_token, username_token, expired_at) VALUES (?, ?, ?, ?)',
                       (uid, make_email_token(email), make_email_token(username), now.isoformat(timespec='seconds')))
            servers = db.execute('SELECT folder FROM servers WHERE user_id=?', (uid,)).fetchall()
            for srv in servers:
                folder = srv['folder']
                proc = running_procs.get(folder)
                if proc and proc.poll() is None:
                    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        try: proc.terminate()
                        except Exception: pass
                running_procs.pop(folder, None)
                start_times.pop(folder, None)
                folder_path = os.path.join(app.config['BASE_STORAGE'], folder)
                if os.path.isdir(folder_path): shutil.rmtree(folder_path, ignore_errors=True)
            db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
            db.execute('DELETE FROM tickets WHERE user_id=?', (uid,))
            db.execute('DELETE FROM users WHERE id=?', (uid,))
            if pfp and pfp != 'default.png':
                still_used = db.execute('SELECT 1 FROM users WHERE pfp=? LIMIT 1', (pfp,)).fetchone()
                pfp_path = os.path.join(app.config['UPLOAD_FOLDER'], pfp)
                if not still_used and os.path.isfile(pfp_path):
                    try: os.remove(pfp_path)
                    except OSError: pass
        if expired:
            db.commit()
        storage = os.path.join(os.getcwd(), 'storage')
        if expired or not os.path.exists(os.path.join(storage, 'user.json')) or not os.path.exists(os.path.join(storage, 'admin_create.json')):
            sync_user_json(db)
        return len(expired)

    def refresh_expired_users(db):
        return remove_expired_accounts(db)

    _startup_db = get_db()
    try:
        remove_expired_accounts(_startup_db)
    finally:
        _startup_db.close()

    def expiry_cleanup_worker():
        while True:
            db = None
            try:
                db = get_db()
                remove_expired_accounts(db)
            except Exception as exc:
                print(f'[LUCKY EXPIRY] Cleanup warning: {exc}')
            finally:
                if db is not None:
                    db.close()
            time.sleep(5)

    threading.Thread(target=expiry_cleanup_worker, daemon=True, name='lucky-expiry-cleaner').start()

    @app.before_request
    def expiry_guard_and_json_sync():
        db = get_db()
        try:
            uid = session.get('user_id')
            if uid:
                current = db.execute('SELECT email, expiry_at FROM users WHERE id=?', (uid,)).fetchone()
                if current and current['expiry_at']:
                    dt = parse_expiry(current['expiry_at'])
                    if dt and dt <= datetime.datetime.now():
                        session['plan_expired'] = True
                        session.pop('user_id', None)
                elif not current and db.execute('SELECT 1 FROM expired_accounts WHERE user_id=?', (uid,)).fetchone():
                    session['plan_expired'] = True
                    session.pop('user_id', None)
            remove_expired_accounts(db)
        finally:
            db.close()

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        
        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        if minutes > 0: parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    def is_proc_running(folder, pid):
        if folder in running_procs and running_procs[folder].poll() is None:
            return True
        if pid and psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    return True
            except: pass
        return False

    @app.route('/')
    def home():
        return render_template('index.html')

    @app.route('/favicon.ico')
    def favicon():
        return '', 204

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        if request.method == 'POST':
            fname = request.form.get('fname')
            lname = request.form.get('lname')
            username = request.form.get('username')
            email = request.form.get('email')
            pwd = request.form.get('password')
            cpwd = request.form.get('confirm_password')
            pfp = request.files.get('pfp')

            if pwd != cpwd:
                return jsonify({'status': 'error', 'msg': 'Passwords do not match!'}), 400

            db = get_db()
            existing_user = db.execute('SELECT id FROM users WHERE email=? OR username=?', (email, username)).fetchone()
            if existing_user:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Email or Username already taken!'}), 400

            pfp_name = 'default.png'
            if pfp and pfp.filename:
                pfp_name = secure_filename(pfp.filename)
                pfp.save(os.path.join(app.config['UPLOAD_FOLDER'], pfp_name))

            db.execute('''INSERT INTO users 
                (fname, lname, username, email, password, pfp, server_limit, role, status) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (fname, lname, username, email, pwd, pfp_name, 1, 'free', 'active'))
            
            db.commit()
            sync_user_json(db)
            db.close()
            return jsonify({'status': 'success', 'url': url_for('login')})
        
        return render_template('web/signup.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email')
            pwd = request.form.get('password')
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE (email=? OR username=?) AND password=?', (email, email, pwd)).fetchone()
            expired_match = db.execute('SELECT user_id FROM expired_accounts WHERE email_token=? OR username_token=?', (make_email_token(email), make_email_token(email))).fetchone()
            if user:
                expiry_dt = parse_expiry(user['expiry_at'])
                if expiry_dt and expiry_dt <= datetime.datetime.now():
                    session['plan_expired'] = True
                    db.close()
                    return jsonify({'status': 'expired', 'msg': 'Your plan has expired.', 'url': url_for('expired_plan')}), 403
            db.close()
            if not user and expired_match:
                session['plan_expired'] = True
                return jsonify({'status': 'expired', 'msg': 'Your plan has expired.', 'url': url_for('expired_plan')}), 403

            if user:
                if user['status'] == 'banned':
                    return jsonify({'status': 'banned', 'msg': 'Your account is suspended!'}), 403
                if user['status'] == 'expired':
                    return jsonify({'status': 'expired', 'msg': 'Your account has expired. Please contact support.'}), 403
                session.pop('plan_expired', None)
                session['user_id'] = user['id']
                return jsonify({'status': 'success', 'url': url_for('dashboard')}), 200
            else:
                return jsonify({'status': 'error', 'msg': 'Invalid credentials!'}), 401
        return render_template('web/login.html')

    @app.route('/dashboard')
    def dashboard():
        if session.get('plan_expired'): return redirect(url_for('expired_plan'))
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if user:
            refresh_expired_users(db)
            user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    @app.route('/expired')
    def expired_plan():
        if not session.get('plan_expired'): return redirect(url_for('login'))
        return render_template('web/hero.html')

    @app.route('/api/expiry-status')
    def expiry_status():
        return jsonify({'expired': bool(session.get('plan_expired'))})

    @app.route('/profile/update', methods=['POST'])
    def update_profile():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        uid = session['user_id']
        fname = request.form.get('fname')
        lname = request.form.get('lname')
        pwd = request.form.get('password')
        db = get_db()
        if pwd:
            db.execute('UPDATE users SET fname=?, lname=?, password=? WHERE id=?', (fname, lname, pwd, uid))
        else:
            db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, uid))
        db.commit()
        sync_user_json(db)
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/ticket/create', methods=['POST'])
    def create_ticket():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO tickets (user_id, subject, message) VALUES (?,?,?)', (session['user_id'], d['subject'], d['message']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/api/announcement')
    def get_announcement():
        db = get_db()
        conf = db.execute('SELECT popup_title, popup_msg, popup_img, show_popup FROM admin_settings WHERE id=1').fetchone()
        db.close()
        return jsonify(dict(conf))

    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            user, pwd = request.form.get('username'), request.form.get('password')
            db = get_db()
            admin = db.execute('SELECT * FROM admin_settings WHERE username=? AND password=?', (user, pwd)).fetchone()
            db.close()
            if admin:
                session['admin_logged'] = True
                session['admin_username'] = admin['username']
                return redirect(url_for('admin_panel'))
        return render_template('web/admin_login.html')

    @app.route('/admin/panel')
    def admin_panel():
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        if not session.get('admin_logged'):
            return jsonify({'users': [], 'error': 'unauthorized'}), 401
        db = get_db()
        refresh_expired_users(db)
        users = db.execute('SELECT * FROM users ORDER BY id DESC').fetchall()
        user_list = []
        total_cpu = psutil.cpu_percent()
        total_ram = psutil.virtual_memory().percent
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = sum(1 for s in srvs if is_proc_running(s['folder'], s['pid']))
            user_list.append({
                'id': u['id'], 'fname': u['fname'] or u['username'] or u['email'] or 'User',
                'username': u['username'] or '', 'email': u['email'] or '',
                'srv_count': len(srvs), 'active_srvs': active_srvs,
                'status': u['status'] or 'active', 'role': u['role'] or 'free',
                'server_limit': u['server_limit'] if u['server_limit'] is not None else 1,
                'expiry_at': u['expiry_at'] or ''
            })
        db.commit()
        db.close()
        return jsonify({'users': user_list, 'sys_cpu': f"{total_cpu}%", 'sys_ram': f"{total_ram}%"})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Unauthorized'}), 401
        d = request.get_json(silent=True) or {}
        uid = d.get('user_id')
        if not uid:
            return jsonify({'status': 'error', 'msg': 'Missing user ID'}), 400
        updates, values = [], []
        if 'role' in d:
            role = str(d.get('role', 'free')).lower()
            if role not in {'free', 'premium'}:
                return jsonify({'status': 'error', 'msg': 'Invalid plan'}), 400
            updates.append('role=?'); values.append(role)
        if 'status' in d:
            status = str(d.get('status', 'active')).lower()
            if status not in {'active', 'banned', 'expired'}:
                return jsonify({'status': 'error', 'msg': 'Invalid status'}), 400
            updates.append('status=?'); values.append(status)
        if 'limit' in d:
            try:
                limit = int(d.get('limit'))
                if limit < 1 or limit > 10000: raise ValueError
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'msg': 'Server limit must be between 1 and 10000'}), 400
            if str(d.get('role', '')).lower() == 'free':
                limit = 1
            updates.append('server_limit=?'); values.append(limit)
        elif str(d.get('role', '')).lower() == 'free':
            updates.append('server_limit=?'); values.append(1)
        if 'expiry_at' in d:
            expiry_at = str(d.get('expiry_at') or '').strip() or None
            if expiry_at and not parse_expiry(expiry_at):
                return jsonify({'status': 'error', 'msg': 'Invalid expiry date/time'}), 400
            if expiry_at and parse_expiry(expiry_at) <= datetime.datetime.now():
                return jsonify({'status': 'error', 'msg': 'Expiry date/time must be in the future'}), 400
            updates.append('expiry_at=?'); values.append(expiry_at)
        if not updates:
            return jsonify({'status': 'error', 'msg': 'No changes supplied'}), 400
        db = get_db()
        if not db.execute('SELECT id FROM users WHERE id=?', (uid,)).fetchone():
            db.close()
            return jsonify({'status': 'error', 'msg': 'User not found'}), 404
        values.append(uid)
        db.execute('UPDATE users SET ' + ', '.join(updates) + ' WHERE id=?', values)
        db.execute('DELETE FROM expired_accounts WHERE user_id=?', (uid,))
        db.commit()
        sync_user_json(db)
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/set-popup', methods=['POST'])
    def set_popup():
        if not session.get('admin_logged'): return jsonify({'status':'error'})
        title, msg, show = request.form.get('title'), request.form.get('msg'), request.form.get('show')
        img = request.files.get('image')
        db = get_db()
        old_data = db.execute('SELECT popup_img FROM admin_settings WHERE id=1').fetchone()
        img_name = old_data['popup_img'] if old_data else None
        if img and img.filename:
            img_name = secure_filename(img.filename)
            img.save(os.path.join(app.config['UPLOAD_FOLDER'], img_name))
        db.execute('UPDATE admin_settings SET popup_title=?, popup_msg=?, popup_img=?, show_popup=? WHERE id=1', (title, msg, img_name, 1 if show == 'true' else 0))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/send-warning', methods=['POST'])
    def send_warning():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET notifications=? WHERE id=?', (d['message'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        session.pop('plan_expired', None)
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user_servers(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        servers = []
        for r in rows:
            f = r['folder']
            online = is_proc_running(f, r['pid'])
            servers.append({'id': r['id'], 'name': r['name'], 'folder': f, 'online': online, 'status': r['server_status']})
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        status = request.json.get('status')
        db = get_db()
        db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        db = get_db()
        srv = db.execute('SELECT folder FROM servers WHERE id=?', (sid,)).fetchone()
        if srv:
            folder = srv['folder']
            if folder in running_procs:
                try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except: pass
                del running_procs[folder]
            db.execute('DELETE FROM servers WHERE id=?', (sid,))
            db.commit()
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path): shutil.rmtree(path)
            db.close()
            return jsonify({'status': 'deleted'})
        db.close()
        return jsonify({'status': 'error', 'msg': 'Server not found'})

    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Unauthorized'}), 401
        d = request.get_json(silent=True) or {}
        name = str(d.get('name', '')).strip()
        email = str(d.get('email', '')).strip().lower()
        password = str(d.get('pass', ''))
        role = str(d.get('role', 'premium')).lower()
        expiry_at = str(d.get('expiry_at') or '').strip() or None
        if not name or not email or not password:
            return jsonify({'status': 'error', 'msg': 'Name, email and password are required'}), 400
        if role not in {'free', 'premium'}:
            return jsonify({'status': 'error', 'msg': 'Invalid plan'}), 400
        try:
            limit = int(d.get('limit', 1))
            if limit < 1 or limit > 10000: raise ValueError
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Server limit must be between 1 and 10000'}), 400
        if expiry_at and not parse_expiry(expiry_at):
            return jsonify({'status': 'error', 'msg': 'Invalid expiry date/time'}), 400
        if expiry_at and parse_expiry(expiry_at) <= datetime.datetime.now():
            return jsonify({'status': 'error', 'msg': 'Expiry date/time must be in the future'}), 400
        if role == 'free':
            limit = 1
        db = get_db()
        if db.execute('SELECT id FROM users WHERE lower(email)=lower(?)', (email,)).fetchone():
            db.close()
            return jsonify({'status': 'error', 'msg': 'Email already exists'}), 409
        status = 'active'
        created_by = session.get('admin_username', 'admin')
        created_at = datetime.datetime.now().isoformat(timespec='seconds')
        db.execute("INSERT INTO users (fname, email, password, role, status, server_limit, expiry_at, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (name, email, password, role, status, limit, expiry_at, created_at, created_by))
        db.commit()
        sync_user_json(db)
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        db = get_db()
        srvs = db.execute('SELECT folder FROM servers WHERE user_id=?', (uid,)).fetchall()
        for s in srvs:
            path = os.path.join(app.config['BASE_STORAGE'], s['folder'])
            if os.path.exists(path): shutil.rmtree(path)
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.execute('DELETE FROM expired_accounts WHERE user_id=?', (uid,))
        db.commit()
        sync_user_json(db)
        db.close()
        return jsonify({'status': 'deleted'})

    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    # ── FILE MANAGER ROUTES ──────────────────────────────────────────────────────

    @app.route('/files/list/<folder>')
    def flist(folder):
        sub_path = request.args.get('path', '')
        full_path = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path))
        if not full_path.startswith(app.config['BASE_STORAGE']): return jsonify([])
        if not os.path.exists(full_path): return jsonify([])
        items = []
        for f in sorted(os.listdir(full_path)):
            if f == 'console.log': continue
            p = os.path.join(full_path, f)
            items.append({'name': f, 'is_dir': os.path.isdir(p), 'is_zip': f.lower().endswith('.zip'), 'rel_path': os.path.join(sub_path, f)})
        return jsonify(items)

    # Read file content — supports both /files/content/<folder>/<name> and /files/read/<folder>
    @app.route('/files/content/<folder>/<name>')
    def fcontent(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read()})
        except: return jsonify({'content': 'Error reading file'})

    @app.route('/files/read/<folder>')
    def fread(folder):
        name = request.args.get('name', '')
        sub_path = request.args.get('path', '')
        p = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name))
        if not p.startswith(app.config['BASE_STORAGE']): return jsonify({'content': 'Access denied'})
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read()})
        except: return jsonify({'content': 'Error reading file'})

    # Save file — accepts JSON body {name, content, path}
    @app.route('/files/save/<folder>', methods=['POST'])
    def fsave_body(folder):
        d = request.json or {}
        name = d.get('name', '')
        sub_path = d.get('path', '')
        content = d.get('content', '')
        p = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name))
        if not p.startswith(app.config['BASE_STORAGE']): return jsonify({'status': 'error', 'msg': 'Access denied'})
        try:
            with open(p, 'w', encoding='utf-8') as f: f.write(content)
            return jsonify({'status': 'saved'})
        except Exception as e: return jsonify({'status': 'error', 'msg': str(e)})

    # Legacy save route (folder/name in URL)
    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    def fsave(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'w', encoding='utf-8') as f: f.write(request.json.get('content', ''))
            return jsonify({'status': 'saved'})
        except: return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        d = request.json
        sub_path, names = d.get('path', ''), d.get('names', [])
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        for name in names:
            p = os.path.join(base, name)
            if name == 'console.log': continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except: pass
        return jsonify({"status": "ok"})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        with open(p, 'w') as f: f.write("")
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub_path = request.form.get('path', '')
        file = request.files['file']
        dest = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not os.path.exists(dest): os.makedirs(dest)
        saved_path = os.path.join(dest, secure_filename(file.filename))
        file.save(saved_path)
        
        # Auto-unzip if it's a zip file
        if saved_path.lower().endswith('.zip') and zipfile.is_zipfile(saved_path):
            try:
                with zipfile.ZipFile(saved_path, 'r') as z:
                    z.extractall(dest)
                os.remove(saved_path)  # Remove the zip after extraction
                return jsonify({'status': 'success', 'auto_unzipped': True})
            except Exception as e:
                return jsonify({'status': 'success', 'msg': f'Unzip failed: {str(e)}'})
        
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        d = request.json
        base = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''))
        os.rename(os.path.join(base, d['old']), os.path.join(base, d['new']))
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    def download_file(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name))
        if not p.startswith(app.config['BASE_STORAGE']): return "Access Denied", 403
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    def zip_bulk(folder):
        d = request.json
        names, sub_path = d.get('names', []), d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = os.path.join(base, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as z:
            for n in names:
                p = os.path.join(base, n)
                if n == zip_name: continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        d = request.json
        zip_name = d.get('name')
        sub_path = d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        zip_path = os.path.join(base, zip_name)
        
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, 'r') as z:
                    z.extractall(base)
                return jsonify({'status': 'success'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': str(e)})
        return jsonify({'status': 'error', 'msg': 'Invalid zip file'})

    # ── SERVER CONTROL ROUTES ────────────────────────────────────────────────────

    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        srv_data = db.execute('SELECT server_status, startup, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'This server is suspended by Admin.'})

        path = os.path.join(app.config['BASE_STORAGE'], folder)
        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if act == 'install':
            req_path = os.path.join(path, 'requirements.txt')
            if os.path.exists(req_path):
                f_log = open(log_file_path, 'a')
                f_log.write(f"\n[{now}] Package Installation Started...\n")
                f_log.flush()
                subprocess.Popen(['pip', 'install', '-r', 'requirements.txt'], cwd=path, stdout=f_log, stderr=f_log)
                db.close()
                return jsonify({'status': 'installing'})
            db.close()
            return jsonify({'status': 'error', 'msg': 'requirements.txt not found'})

        if act in ['start', 'restart']:
            old_pid = srv_data['pid'] if srv_data else None
            # Kill existing process
            if folder in running_procs:
                try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except: pass
                del running_procs[folder]
            elif old_pid and psutil.pid_exists(old_pid):
                try: os.killpg(os.getpgid(old_pid), signal.SIGKILL)
                except: pass

            startup_file = (srv_data['startup'] if srv_data and srv_data['startup'] else 'main.py')
            startup_path = os.path.join(path, startup_file)
            
            if not os.path.exists(startup_path):
                db.close()
                return jsonify({'status': 'error', 'msg': f'Startup file "{startup_file}" not found. Upload it first!'})

            f_log = open(log_file_path, 'a')
            f_log.write(f"\n[{now}] Instance {act.upper()}ED | Startup: {startup_file}\n")
            proc = subprocess.Popen(
                ['python3', startup_file], cwd=path,
                stdout=f_log, stderr=f_log, preexec_fn=os.setsid
            )
            running_procs[folder] = proc
            start_times[folder] = time.time()
            db.execute('UPDATE servers SET pid=?, status=? WHERE folder=?', (proc.pid, 'Online', folder))
            db.commit()
            db.close()
            return jsonify({'status': 'started', 'pid': proc.pid})

        elif act == 'stop':
            old_pid = srv_data['pid'] if srv_data else None
            t_pid = running_procs[folder].pid if folder in running_procs else old_pid
            if t_pid:
                try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            if folder in running_procs: del running_procs[folder]
            if folder in start_times: del start_times[folder]
            db.execute('UPDATE servers SET pid=NULL, status=? WHERE folder=?', ('Offline', folder))
            db.commit()
            db.close()
            with open(log_file_path, 'a') as f: f.write(f"\n[{now}] Instance STOPPED\n")
            return jsonify({'status': 'stopped'})

        db.close()
        return jsonify({'status': 'ok'})

    @app.route('/server/log/<folder>')
    def server_log(folder):
        db = get_db()
        row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
        db.close()
        pid = row['pid'] if row else None
        online = is_proc_running(folder, pid)
        uptime = get_precise_uptime(start_times.get(folder)) if online and folder in start_times else ("Online" if online else "Offline")
        
        log_path = os.path.join(app.config['BASE_STORAGE'], folder, 'console.log')
        log_content = 'Waiting for logs...'
        if os.path.exists(log_path):
            with open(log_path, 'r') as f: log_content = f.read()[-8000:]
        return jsonify({'log': log_content, 'online': online, 'uptime': uptime})

    @app.route('/server/command/<folder>', methods=['POST'])
    def server_command(folder):
        d = request.json or {}
        cmd = d.get('command', '').strip()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        log_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if not cmd:
            return jsonify({'status': 'error', 'msg': 'Empty command'})
        
        with open(log_path, 'a') as f:
            f.write(f"\n[{now}] $ {cmd}\n")
        
        try:
            result = subprocess.run(
                cmd, shell=True, cwd=path,
                capture_output=True, text=True, timeout=10
            )
            output = (result.stdout + result.stderr).strip()
            with open(log_path, 'a') as f:
                f.write(output + "\n" if output else "")
            return jsonify({'status': 'ok', 'output': output})
        except subprocess.TimeoutExpired:
            with open(log_path, 'a') as f: f.write("Command timed out.\n")
            return jsonify({'status': 'timeout'})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': str(e)})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    def set_startup(folder):
        cmd = request.json.get('file', 'main.py')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'file': cmd})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        db = get_db()
        srv_data = db.execute('SELECT server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended servers cannot be deleted!'})
        t_pid = running_procs[folder].pid if folder in running_procs else (srv_data['pid'] if srv_data else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        if folder in start_times: del start_times[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        if os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session: return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = is_proc_running(f, saved_pid)
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram = "0%", "0MB"
            if online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu = f"{process.cpu_percent(interval=None)}%"
                    ram = f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass
            srvs.append({
                'name': r['name'], 'folder': f, 'online': online,
                'startup': r['startup'] or 'main.py',
                'uptime': uptime, 'cpu': cpu, 'ram': ram,
                'status': r['server_status']
            })
        return jsonify({'servers': srvs})

    @app.route('/add', methods=['POST'])
    def add_srv():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        refresh_expired_users(db)
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not user or user['status'] != 'active':
            db.close()
            session.clear()
            return jsonify({'status': 'error', 'msg': 'Your account is not active. Please contact support.'}), 403
        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (session['user_id'],)).fetchone()['count']
        if count >= user['server_limit']:
            db.close()
            return jsonify({'status': 'error', 'msg': f"Server limit reached! Max: {user['server_limit']}"})
        name = request.json.get('name', '').strip()
        if not name:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server name cannot be empty'})
        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)',
                   (session['user_id'], name, folder, 'Offline', 'main.py'))
        db.commit()
        db.close()
        os.makedirs(os.path.join(app.config['BASE_STORAGE'], folder), exist_ok=True)
        return jsonify({'status': 'success', 'folder': folder})

    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 42353))
    print(f"[LUCKY HOST] Starting server on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
