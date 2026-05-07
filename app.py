from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from functools import wraps
import sqlite3, os, uuid, datetime, json, io, hmac, hashlib
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import razorpay

app = Flask(__name__)
app.secret_key = 'maktronics-secret-key-2024'
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

# ─── RAZORPAY CONFIG ──────────────────────────────────────────────────────────
# Replace with your actual Razorpay Key ID and Secret from https://dashboard.razorpay.com
RAZORPAY_KEY_ID     = 'rzp_test_SmNSnbXRbFyUas'
RAZORPAY_KEY_SECRET = 'mUiDDcYZ72EzW7pUvtCi4asH'

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

@app.context_processor
def inject_globals():
    def _has_perm(perm):
        if not session.get('user_id'):
            return False
        if session.get('role') == 'admin':
            return True
        db = get_db()
        user = db.execute("SELECT permissions FROM users WHERE id=?", (session['user_id'],)).fetchone()
        if not user:
            return False
        try:
            return json.loads(user['permissions'] or '{}').get(perm, False)
        except Exception:
            return False
    return dict(has_perm=_has_perm)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

DB_PATH = 'maktronics.db'

# ─── DB SETUP ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','technician','manager')),
                name TEXT NOT NULL,
                permissions TEXT DEFAULT "{}",
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                customer_name TEXT,
                customer_phone TEXT,
                customer_email TEXT,
                item_description TEXT,
                item_type TEXT,
                barcode TEXT,
                status TEXT DEFAULT 'received',
                assigned_tech_id INTEGER,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                repair_findings TEXT,
                parts_cost REAL DEFAULT 0,
                labour_cost REAL DEFAULT 0,
                total_amount REAL DEFAULT 0,
                invoice_number TEXT,
                payment_status TEXT DEFAULT 'pending',
                payment_received_at TIMESTAMP,
                tracking_number TEXT,
                dispatch_date TEXT,
                expected_delivery TEXT,
                delivered_at TIMESTAMP,
                notes TEXT,
                FOREIGN KEY(assigned_tech_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS job_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                photo_path TEXT NOT NULL,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                action TEXT NOT NULL,
                performed_by INTEGER,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(performed_by) REFERENCES users(id)
            );
        ''')

        # Add new columns one by one with error handling
        new_columns = {
            'invoice_path': 'TEXT',
            'courier_name': 'TEXT',
            'courier_receipt_path': 'TEXT',
            'estimate_amount': 'REAL DEFAULT 0',
            'estimate_notes': 'TEXT',
            'estimate_sent_at': 'TIMESTAMP',
            'estimate_approved_at': 'TIMESTAMP',
            'not_repairable_reason': 'TEXT',
            'sent_back_to_customer_at': 'TIMESTAMP',
            'inspection_findings': 'TEXT',
            'invoice_generate_date': 'TIMESTAMP',  
            'invoice_total_amount': 'REAL DEFAULT 0'
        }
        
        for col_name, col_type in new_columns.items():
            try:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
                db.commit()
            except Exception:
                # Column already exists, skip
                pass

        # Payment gateway columns
        payment_cols = {
            'razorpay_order_id': 'TEXT',
            'razorpay_payment_id': 'TEXT',
            'payment_link': 'TEXT',
            'payment_token': 'TEXT',
        }
        for col_name, col_type in payment_cols.items():
            try:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
                db.commit()
            except Exception:
                pass

        # Migrate: add permissions column if missing
        try:
            db.execute("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT '{}'")
            db.commit()
        except Exception:
            pass

        # Seed default admin
        existing = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not existing:
            db.execute("INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                ('admin', generate_password_hash('admin123'), 'admin', 'Admin User'))
            db.execute("INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                ('tech1', generate_password_hash('tech123'), 'technician', 'Ravi Kumar'))
            db.execute("INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                ('tech2', generate_password_hash('tech456'), 'technician', 'Suresh Patil'))
        db.commit()

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_job_id():
    now = datetime.datetime.now()
    return f"MAK-{now.strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

def log_action(db, job_id, action, user_id, details=''):
    db.execute("INSERT INTO job_logs (job_id, action, performed_by, details) VALUES (?,?,?,?)",
               (job_id, action, user_id, details))

# Updated STATUS_FLOW
STATUS_FLOW = {
    'received':           {'label': 'Received',          'icon': '📦', 'color': '#64B5F6'},
    'barcode_gen':        {'label': 'Barcode Generated', 'icon': '🏷️', 'color': '#CE93D8'},
    'sent_for_inspection':{'label': 'Sent for Inspection', 'icon': '🔍', 'color': '#FFB74D'},
    'inspection_completed':{'label': 'Inspection Completed', 'icon': '✅', 'color': '#81C784'},
    'not_repairable':     {'label': 'Not Repairable',    'icon': '❌', 'color': '#EF5350'},
    'estimate_sent':      {'label': 'Estimate Sent',     'icon': '📄', 'color': '#FFD54F'},
    'estimate_approved':  {'label': 'Estimate Approved', 'icon': '👍', 'color': '#4DB6AC'},
    'estimate_rejected':  {'label': 'Estimate Rejected', 'icon': '🚫', 'color': '#EF5350'},
    'in_repair':          {'label': 'In Repair',         'icon': '🔧', 'color': '#F06292'},
    'repair_done':        {'label': 'Repair Completed',  'icon': '✅', 'color': '#4DB6AC'},
    'invoice_uploaded':   {'label': 'Invoice Uploaded',  'icon': '📎', 'color': '#AED581'},
    'payment_received':   {'label': 'Payment Received',  'icon': '💰', 'color': '#66BB6A'},
    'dispatched':         {'label': 'Dispatched',        'icon': '🚚', 'color': '#4DD0E1'},
    'delivered':          {'label': 'Delivered',         'icon': '🏠', 'color': '#81C784'},
    'closed':             {'label': 'Job Closed',        'icon': '🔒', 'color': '#90A4AE'},
}

# ─── AUTH ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') not in ('admin', 'manager'):
            flash('Admin access required.', 'error')
            return redirect(url_for('tech_dashboard'))
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    if 'user_id' in session:
        if session['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('tech_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['name'] = user['name']
            if user['role'] == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('tech_dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('shared/login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── ADMIN ROUTES ─────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()
    search = request.args.get('search', '').strip()

    stats = {
        'total':           db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        'in_repair':       db.execute("SELECT COUNT(*) FROM jobs WHERE status='in_repair'").fetchone()[0],
        'payment_pending': db.execute("SELECT COUNT(*) FROM jobs WHERE payment_status='pending' AND status='invoice_uploaded'").fetchone()[0],
        'dispatched':      db.execute("SELECT COUNT(*) FROM jobs WHERE status='dispatched'").fetchone()[0],
        'closed':          db.execute("SELECT COUNT(*) FROM jobs WHERE status='closed'").fetchone()[0],
    }

    rev_row = db.execute("""
        SELECT
            COALESCE(SUM(total_amount), 0) AS total_revenue,
            COALESCE(SUM(parts_cost), 0) AS total_parts,
            COALESCE(SUM(labour_cost), 0) AS total_labour,
            COALESCE(SUM(CASE WHEN payment_status='paid' THEN total_amount ELSE 0 END), 0) AS paid_revenue
        FROM jobs
    """).fetchone()

    status_rows = db.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status").fetchall()
    status_counts = {r['status']: r['cnt'] for r in status_rows}

    tech_rows = db.execute("""
        SELECT
            COALESCE(u.name, 'Unassigned') AS name,
            COALESCE(SUM(j.total_amount), 0) AS revenue,
            COALESCE(SUM(j.parts_cost), 0) AS parts,
            COALESCE(SUM(j.labour_cost), 0) AS labour
        FROM jobs j
        LEFT JOIN users u ON j.assigned_tech_id = u.id
        GROUP BY j.assigned_tech_id
        ORDER BY revenue DESC
    """).fetchall()
    tech_revenue = [dict(r) for r in tech_rows]

    monthly_rows = db.execute("""
        SELECT
            strftime('%Y-%m', received_at) AS month,
            COUNT(*) AS jobs,
            COALESCE(SUM(total_amount), 0) AS revenue
        FROM jobs
        WHERE received_at >= date('now', '-6 months')
        GROUP BY month
        ORDER BY month
    """).fetchall()
    monthly = [dict(r) for r in monthly_rows]

    item_rows = db.execute("""
        SELECT item_type, COUNT(*) AS count
        FROM jobs
        WHERE item_type IS NOT NULL AND item_type != ''
        GROUP BY item_type
        ORDER BY count DESC
    """).fetchall()
    item_types = [dict(r) for r in item_rows]

    analytics = {
        'total_revenue': rev_row['total_revenue'],
        'total_parts': rev_row['total_parts'],
        'total_labour': rev_row['total_labour'],
        'paid_revenue': rev_row['paid_revenue'],
        'status_counts': status_counts,
        'tech_revenue': tech_revenue,
        'monthly': monthly,
        'item_types': item_types,
    }

    if search:
        recent_jobs = db.execute("""
            SELECT j.*, u.name as tech_name FROM jobs j
            LEFT JOIN users u ON j.assigned_tech_id = u.id
            WHERE j.job_id LIKE ? OR j.barcode LIKE ? OR j.customer_name LIKE ? OR j.customer_phone LIKE ?
            ORDER BY j.updated_at DESC LIMIT 50
        """, (f'%{search}%', f'%{search}%', f'%{search}%', f'%{search}%')).fetchall()
    else:
        recent_jobs = db.execute("""
            SELECT j.*, u.name as tech_name FROM jobs j
            LEFT JOIN users u ON j.assigned_tech_id = u.id
            ORDER BY j.updated_at DESC LIMIT 20
        """).fetchall()

    technicians = db.execute("SELECT * FROM users WHERE role='technician'").fetchall()
    return render_template('admin/dashboard.html', stats=stats, jobs=recent_jobs,
                           technicians=technicians, status_flow=STATUS_FLOW,
                           search=search, analytics=analytics)

@app.route('/admin/jobs')
@admin_required
def admin_jobs():
    db = get_db()
    status_filter = request.args.get('status', '')
    search = request.args.get('search', '')
    query = """
        SELECT j.*, u.name as tech_name FROM jobs j
        LEFT JOIN users u ON j.assigned_tech_id = u.id
        WHERE 1=1
    """
    params = []
    if status_filter:
        query += " AND j.status=?"
        params.append(status_filter)
    if search:
        query += " AND (j.job_id LIKE ? OR j.barcode LIKE ? OR j.customer_name LIKE ? OR j.customer_phone LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%', f'%{search}%'])
    query += " ORDER BY j.updated_at DESC"
    jobs = db.execute(query, params).fetchall()
    technicians = db.execute("SELECT * FROM users WHERE role='technician'").fetchall()
    return render_template('admin/jobs.html', jobs=jobs, status_flow=STATUS_FLOW,
                           technicians=technicians, status_filter=status_filter, search=search)

@app.route('/admin/jobs/new', methods=['GET', 'POST'])
@admin_required
def admin_new_job():
    if request.method == 'POST':
        db = get_db()
        job_id = generate_job_id()
        barcode = job_id
        
        data = {
            'job_id': job_id,
            'customer_name': request.form.get('customer_name'),
            'customer_phone': request.form.get('customer_phone'),
            'customer_email': request.form.get('customer_email'),
            'item_description': request.form.get('item_description'),
            'item_type': request.form.get('item_type'),
            'barcode': barcode,
            'assigned_tech_id': request.form.get('assigned_tech_id') or None,
            'status': 'sent_for_inspection',
            'notes': request.form.get('notes'),
        }
        db.execute("""
            INSERT INTO jobs (job_id, customer_name, customer_phone, customer_email,
            item_description, item_type, barcode, assigned_tech_id, status, notes)
            VALUES (:job_id,:customer_name,:customer_phone,:customer_email,
            :item_description,:item_type,:barcode,:assigned_tech_id,:status,:notes)
        """, data)

        photos = request.files.getlist('photos')
        for photo in photos[:3]:
            if photo and allowed_file(photo.filename):
                filename = secure_filename(f"{job_id}_{uuid.uuid4().hex[:6]}_{photo.filename}")
                photo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                db.execute("INSERT INTO job_photos (job_id, photo_path) VALUES (?,?)",
                           (job_id, filename))

        log_action(db, job_id, 'Job Created', session['user_id'], f"Customer: {data['customer_name']}")
        db.commit()
        flash(f'Job {job_id} created successfully!', 'success')
        return redirect(url_for('admin_job_detail', job_id=job_id))

    db = get_db()
    technicians = db.execute("SELECT * FROM users WHERE role='technician'").fetchall()
    return render_template('admin/new_job.html', technicians=technicians)

@app.route('/admin/jobs/<job_id>')
@admin_required
def admin_job_detail(job_id):
    db = get_db()
    job = db.execute("""
        SELECT j.*, u.name as tech_name FROM jobs j
        LEFT JOIN users u ON j.assigned_tech_id = u.id
        WHERE j.job_id=?
    """, (job_id,)).fetchone()
    if not job:
        flash('Job not found', 'error')
        return redirect(url_for('admin_jobs'))
    photos = db.execute("SELECT * FROM job_photos WHERE job_id=?", (job_id,)).fetchall()
    logs = db.execute("""
        SELECT l.*, u.name as user_name FROM job_logs l
        LEFT JOIN users u ON l.performed_by = u.id
        WHERE l.job_id=? ORDER BY l.created_at DESC
    """, (job_id,)).fetchall()
    technicians = db.execute("SELECT * FROM users WHERE role='technician'").fetchall()
    return render_template('admin/job_detail.html', job=job, photos=photos,
                           logs=logs, technicians=technicians, status_flow=STATUS_FLOW)

@app.route('/admin/jobs/<job_id>/update', methods=['POST'])
@admin_required
def admin_update_job(job_id):
    db = get_db()
    action = request.form.get('action')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if action == 'update_status':
        new_status = request.form.get('status')
        db.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (new_status, now, job_id))
        log_action(db, job_id, f'Status → {new_status}', session['user_id'])

    elif action == 'assign_tech':
        tech_id = request.form.get('tech_id')
        db.execute("UPDATE jobs SET assigned_tech_id=?, updated_at=? WHERE job_id=?", (tech_id, now, job_id))
        tech = db.execute("SELECT name FROM users WHERE id=?", (tech_id,)).fetchone()
        log_action(db, job_id, f'Assigned to {tech["name"]}', session['user_id'])

    elif action == 'send_for_inspection':
        db.execute("UPDATE jobs SET status='sent_for_inspection', updated_at=? WHERE job_id=?", (now, job_id))
        log_action(db, job_id, 'Sent for Inspection', session['user_id'])

    elif action == 'complete_inspection':
        findings = request.form.get('inspection_findings')
        is_repairable = request.form.get('is_repairable') == 'yes'
        
        if is_repairable:
            db.execute("UPDATE jobs SET status='inspection_completed', inspection_findings=?, updated_at=? WHERE job_id=?", 
                       (findings, now, job_id))
            log_action(db, job_id, 'Inspection Completed - Repairable', session['user_id'], findings)
        else:
            reason = request.form.get('not_repairable_reason')
            db.execute("UPDATE jobs SET status='not_repairable', inspection_findings=?, not_repairable_reason=?, sent_back_to_customer_at=?, updated_at=? WHERE job_id=?", 
                       (findings, reason, now, now, job_id))
            log_action(db, job_id, 'Device Not Repairable', session['user_id'], reason)

    elif action == 'dispatch_not_repairable':
        tracking = request.form.get('tracking_number')
        courier_name = request.form.get('courier_name')
        dispatch_date = request.form.get('dispatch_date')
        
        receipt_file = request.files.get('courier_receipt')
        receipt_path = None
        
        if receipt_file and allowed_file(receipt_file.filename):
            filename = secure_filename(f"RETURN_{job_id}_{uuid.uuid4().hex[:8]}_{receipt_file.filename}")
            receipt_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            receipt_path = filename
        
        db.execute("""
            UPDATE jobs SET status='dispatched', tracking_number=?, courier_name=?,
            courier_receipt_path=?, dispatch_date=?, updated_at=? WHERE job_id=?
        """, (tracking, courier_name, receipt_path, dispatch_date, now))
        log_action(db, job_id, 'Non-repairable item dispatched back to customer', session['user_id'], f'Courier: {courier_name}, Tracking: {tracking}')

    elif action == 'generate_estimate':
        estimate_amount = float(request.form.get('estimate_amount', 0))
        estimate_notes = request.form.get('estimate_notes', '')
        db.execute("""
            UPDATE jobs SET status='estimate_sent', estimate_amount=?, 
            estimate_notes=?, estimate_sent_at=?, updated_at=? WHERE job_id=?
        """, (estimate_amount, estimate_notes, now, now, job_id))
        log_action(db, job_id, f'Estimate Generated: ₹{estimate_amount}', session['user_id'], estimate_notes)

    elif action == 'approve_estimate':
        db.execute("UPDATE jobs SET status='estimate_approved', estimate_approved_at=?, updated_at=? WHERE job_id=?", 
                   (now, now, job_id))
        log_action(db, job_id, 'Estimate Approved by Customer', session['user_id'])

    elif action == 'reject_estimate':
        reason = request.form.get('rejection_reason', '')
        db.execute("UPDATE jobs SET status='estimate_rejected', notes=?, updated_at=? WHERE job_id=?", 
                   (reason, now, job_id))
        log_action(db, job_id, 'Estimate Rejected by Customer', session['user_id'], reason)

    elif action == 'start_repair':
        db.execute("UPDATE jobs SET status='in_repair', updated_at=? WHERE job_id=?", (now, job_id))
        log_action(db, job_id, 'Repair Started', session['user_id'])

    elif action == 'repair_complete':
        findings = request.form.get('repair_findings')
        parts = float(request.form.get('parts_cost', 0))
        labour = float(request.form.get('labour_cost', 0))
        total = parts + labour
        db.execute("""
            UPDATE jobs SET status='repair_done', repair_findings=?, parts_cost=?,
            labour_cost=?, total_amount=?, updated_at=? WHERE job_id=?
        """, (findings, parts, labour, total, now, job_id))
        log_action(db, job_id, 'Repair Completed', session['user_id'], f'Total: ₹{total}')

    elif action == 'upload_invoice':
        invoice_file = request.files.get('invoice_file')
        invoice_number = request.form.get('invoice_number')
        invoice_total_amount = float(request.form.get('invoice_total_amount', 0))
        invoice_generate_date = request.form.get('invoice_generate_date')
        
        if invoice_file and allowed_file(invoice_file.filename):
            filename = secure_filename(f"INV_{job_id}_{uuid.uuid4().hex[:8]}_{invoice_file.filename}")
            invoice_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            invoice_file.save(invoice_path)
            
            # If invoice date not provided, use today's date
            if not invoice_generate_date:
                invoice_generate_date = datetime.datetime.now().strftime('%Y-%m-%d')
            
            db.execute("""
                UPDATE jobs SET 
                    invoice_path=?, 
                    invoice_number=?, 
                    invoice_total_amount=?,
                    invoice_generate_date=?,
                    total_amount=?,
                    status='invoice_uploaded', 
                    updated_at=? 
                WHERE job_id=?
            """, (filename, invoice_number, invoice_total_amount, invoice_generate_date, 
                  invoice_total_amount, now, job_id))
            
            log_action(db, job_id, f'Invoice Uploaded: {invoice_number} (₹{invoice_total_amount})', 
                       session['user_id'], f'Date: {invoice_generate_date}')
            flash('Invoice uploaded successfully!', 'success')
        else:
            flash('Please upload a valid invoice file', 'error')

    elif action == 'send_payment_notification':
        # Create Razorpay order and generate a secure payment link
        job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        amount_paise = int(float(job['invoice_total_amount'] or job['total_amount'] or 0) * 100)
        if amount_paise <= 0:
            flash('Invoice amount must be greater than 0 to create a payment link.', 'error')
            return redirect(url_for('admin_job_detail', job_id=job_id))

        try:
            order = razorpay_client.order.create({
                'amount': amount_paise,
                'currency': 'INR',
                'receipt': job_id,
                'notes': {
                    'job_id': job_id,
                    'customer': job['customer_name'],
                    'invoice': job['invoice_number'] or '',
                }
            })
            token = uuid.uuid4().hex
            pay_link = url_for('customer_pay', job_id=job_id, token=token, _external=True)
            db.execute("""
                UPDATE jobs SET razorpay_order_id=?, payment_token=?, payment_link=?, updated_at=? WHERE job_id=?
            """, (order['id'], token, pay_link, now, job_id))
            log_action(db, job_id, 'Payment Notification Sent', session['user_id'],
                       f'Razorpay Order: {order["id"]}, Amount: ₹{amount_paise/100}')
            flash(f'Payment link created! Share this with the customer: {pay_link}', 'success')
        except Exception as e:
            flash(f'Razorpay error: {str(e)}', 'error')
        return redirect(url_for('admin_job_detail', job_id=job_id))

    elif action == 'payment_received':
        # Manual override: mark payment as received without gateway
        db.execute("""
            UPDATE jobs SET payment_status='paid', payment_received_at=?,
            status='payment_received', updated_at=? WHERE job_id=?
        """, (now, now, job_id))
        log_action(db, job_id, 'Payment Received (Manual)', session['user_id'])

    elif action == 'dispatch':
        tracking = request.form.get('tracking_number')
        courier_name = request.form.get('courier_name')
        dispatch_date = request.form.get('dispatch_date')
        expected = request.form.get('expected_delivery')
        
        receipt_file = request.files.get('courier_receipt')
        receipt_path = None
        
        if receipt_file and allowed_file(receipt_file.filename):
            filename = secure_filename(f"COURIER_{job_id}_{uuid.uuid4().hex[:8]}_{receipt_file.filename}")
            receipt_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            receipt_path = filename
        
        db.execute("""
            UPDATE jobs SET status='dispatched', tracking_number=?, courier_name=?,
            courier_receipt_path=?, dispatch_date=?, expected_delivery=?, updated_at=? WHERE job_id=?
        """, (tracking, courier_name, receipt_path, dispatch_date, expected, now, job_id))
        log_action(db, job_id, 'Dispatched', session['user_id'], f'Courier: {courier_name}, Tracking: {tracking}')

    elif action == 'delivered':
        db.execute("UPDATE jobs SET status='delivered', delivered_at=?, updated_at=? WHERE job_id=?",
                   (now, now, job_id))
        log_action(db, job_id, 'Delivered to Customer', session['user_id'])

    elif action == 'close':
        db.execute("UPDATE jobs SET status='closed', updated_at=? WHERE job_id=?", (now, job_id))
        log_action(db, job_id, 'Job Closed', session['user_id'])

    elif action == 'send_reminder':
        reminder_type = request.form.get('reminder_type', 'payment')
        log_action(db, job_id, f'{reminder_type.capitalize()} Reminder Sent via WhatsApp', session['user_id'])
        flash(f'{reminder_type.capitalize()} reminder sent via WhatsApp!', 'success')

    db.commit()
    flash('Job updated successfully!', 'success')
    return redirect(url_for('admin_job_detail', job_id=job_id))

@app.route('/admin/jobs/<job_id>/delete', methods=['POST'])
@admin_required
def admin_delete_job(job_id):
    db = get_db()
    db.execute("DELETE FROM job_photos WHERE job_id=?", (job_id,))
    db.execute("DELETE FROM job_logs WHERE job_id=?", (job_id,))
    db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
    db.commit()
    flash(f'Job {job_id} deleted.', 'success')
    return redirect(url_for('admin_jobs'))

# ─── TECHNICIAN ROUTES ────────────────────────────────────────────────────────
@app.route('/tech')
@login_required
def tech_dashboard():
    db = get_db()
    tech_id = session['user_id']
    stats = {
        'total': db.execute("SELECT COUNT(*) FROM jobs WHERE assigned_tech_id=?", (tech_id,)).fetchone()[0],
        'in_repair': db.execute("SELECT COUNT(*) FROM jobs WHERE assigned_tech_id=? AND status='in_repair'", (tech_id,)).fetchone()[0],
        'repair_done': db.execute("SELECT COUNT(*) FROM jobs WHERE assigned_tech_id=? AND status='repair_done'", (tech_id,)).fetchone()[0],
        'closed': db.execute("SELECT COUNT(*) FROM jobs WHERE assigned_tech_id=? AND status='closed'", (tech_id,)).fetchone()[0],
    }
    my_jobs = db.execute("""
        SELECT * FROM jobs WHERE assigned_tech_id=? ORDER BY updated_at DESC LIMIT 10
    """, (tech_id,)).fetchall()
    return render_template('technician/dashboard.html', stats=stats, jobs=my_jobs,
                           status_flow=STATUS_FLOW)

@app.route('/tech/jobs')
@login_required
def tech_jobs():
    db = get_db()
    tech_id = session['user_id']
    status_filter = request.args.get('status', '')
    search = request.args.get('search', '')
    query = "SELECT * FROM jobs WHERE assigned_tech_id=?"
    params = [tech_id]
    if status_filter:
        query += " AND status=?"
        params.append(status_filter)
    if search:
        query += " AND (job_id LIKE ? OR barcode LIKE ? OR customer_name LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    query += " ORDER BY updated_at DESC"
    jobs = db.execute(query, params).fetchall()
    return render_template('technician/jobs.html', jobs=jobs, status_flow=STATUS_FLOW,
                           status_filter=status_filter, search=search)

@app.route('/tech/jobs/<job_id>')
@login_required
def tech_job_detail(job_id):
    db = get_db()
    tech_id = session['user_id']
    if session['role'] == 'admin':
        job = db.execute("SELECT j.*, u.name as tech_name FROM jobs j LEFT JOIN users u ON j.assigned_tech_id=u.id WHERE j.job_id=?", (job_id,)).fetchone()
    else:
        job = db.execute("SELECT * FROM jobs WHERE job_id=? AND assigned_tech_id=?", (job_id, tech_id)).fetchone()
    if not job:
        flash('Job not found or not assigned to you', 'error')
        return redirect(url_for('tech_jobs'))
    photos = db.execute("SELECT * FROM job_photos WHERE job_id=?", (job_id,)).fetchall()
    logs = db.execute("""
        SELECT l.*, u.name as user_name FROM job_logs l
        LEFT JOIN users u ON l.performed_by=u.id
        WHERE l.job_id=? ORDER BY l.created_at DESC
    """, (job_id,)).fetchall()
    return render_template('technician/job_detail.html', job=job, photos=photos,
                           logs=logs, status_flow=STATUS_FLOW)

@app.route('/tech/jobs/<job_id>/update', methods=['POST'])
@login_required
def tech_update_job(job_id):
    db = get_db()
    tech_id = session['user_id']
    action = request.form.get('action')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if session['role'] != 'admin':
        job = db.execute("SELECT * FROM jobs WHERE job_id=? AND assigned_tech_id=?",
                         (job_id, tech_id)).fetchone()
        if not job:
            flash('Access denied', 'error')
            return redirect(url_for('tech_jobs'))

    if action == 'start_repair':
        db.execute("UPDATE jobs SET status='in_repair', updated_at=? WHERE job_id=?", (now, job_id))
        log_action(db, job_id, 'Repair Started', tech_id)

    elif action == 'update_progress':
        notes = request.form.get('notes')
        db.execute("UPDATE jobs SET notes=?, updated_at=? WHERE job_id=?", (notes, now, job_id))
        log_action(db, job_id, 'Progress Updated', tech_id, notes)

    elif action == 'repair_complete':
        findings = request.form.get('repair_findings')
        parts = float(request.form.get('parts_cost', 0))
        labour = float(request.form.get('labour_cost', 0))
        total = parts + labour
        db.execute("""
            UPDATE jobs SET status='repair_done', repair_findings=?, parts_cost=?,
            labour_cost=?, total_amount=?, updated_at=? WHERE job_id=?
        """, (findings, parts, labour, total, now, job_id))
        log_action(db, job_id, 'Repair Completed', tech_id, f'Findings: {findings}, Total: ₹{total}')

    elif action == 'add_photo':
        photos = request.files.getlist('photos')
        for photo in photos[:3]:
            if photo and allowed_file(photo.filename):
                filename = secure_filename(f"{job_id}_{uuid.uuid4().hex[:6]}_{photo.filename}")
                photo.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                db.execute("INSERT INTO job_photos (job_id, photo_path) VALUES (?,?)",
                           (job_id, filename))
        log_action(db, job_id, 'Photos Added', tech_id)

    db.commit()
    flash('Job updated!', 'success')
    return redirect(url_for('tech_job_detail', job_id=job_id))

# ─── BARCODE SCANNER ──────────────────────────────────────────────────────────

@app.route('/scanner')
@login_required
def scanner():
    return render_template('shared/scanner.html')

@app.route('/api/scan', methods=['GET'])
@login_required
def api_scan():
    """Lookup a job by job_id or barcode value, return redirect URL."""
    code = request.args.get('code', '').strip()
    if not code:
        return jsonify({'found': False, 'error': 'No code provided'}), 400

    db = get_db()
    job = db.execute(
        "SELECT job_id FROM jobs WHERE job_id=? OR barcode=?", (code, code)
    ).fetchone()

    if not job:
        return jsonify({'found': False, 'error': f'No job found for: {code}'}), 404

    # Redirect admins to admin detail, technicians to tech detail
    role = session.get('role')
    if role in ('admin', 'manager'):
        url = url_for('admin_job_detail', job_id=job['job_id'])
    else:
        url = url_for('tech_job_detail', job_id=job['job_id'])

    return jsonify({'found': True, 'job_id': job['job_id'], 'redirect': url})

# ─── CUSTOMER PAYMENT PAGE ────────────────────────────────────────────────────

@app.route('/pay/<job_id>/<token>')
def customer_pay(job_id, token):
    """Public page the customer sees when they click Pay Now."""
    db = get_db()
    job = db.execute(
        "SELECT * FROM jobs WHERE job_id=? AND payment_token=?", (job_id, token)
    ).fetchone()
    if not job:
        return "Invalid or expired payment link.", 404
    if job['payment_status'] == 'paid':
        return render_template('shared/payment_success.html', job=job, already_paid=True)
    amount = float(job['invoice_total_amount'] or job['total_amount'] or 0)
    return render_template(
        'shared/customer_payment.html',
        job=job,
        amount=amount,
        razorpay_key=RAZORPAY_KEY_ID,
    )


@app.route('/pay/<job_id>/verify', methods=['POST'])
def verify_payment(job_id):
    """Called client-side after Razorpay checkout succeeds."""
    db = get_db()
    data = request.get_json() or request.form.to_dict()
    razorpay_order_id   = data.get('razorpay_order_id', '')
    razorpay_payment_id = data.get('razorpay_payment_id', '')
    razorpay_signature  = data.get('razorpay_signature', '')

    body = f"{razorpay_order_id}|{razorpay_payment_id}".encode()
    expected_sig = hmac.new(
        RAZORPAY_KEY_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, razorpay_signature):
        return jsonify({'status': 'error', 'message': 'Signature mismatch'}), 400

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute("""
        UPDATE jobs SET
            payment_status='paid',
            payment_received_at=?,
            razorpay_payment_id=?,
            status='payment_received',
            updated_at=?
        WHERE job_id=?
    """, (now, razorpay_payment_id, now, job_id))
    db.execute(
        "INSERT INTO job_logs (job_id, action, performed_by, details) VALUES (?,?,?,?)",
        (job_id, 'Payment Received via Razorpay', None,
         f'Payment ID: {razorpay_payment_id}, Order: {razorpay_order_id}')
    )
    db.commit()
    return jsonify({'status': 'success', 'redirect': url_for('payment_success', job_id=job_id)})


@app.route('/pay/<job_id>/success')
def payment_success(job_id):
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        return "Job not found.", 404
    return render_template('shared/payment_success.html', job=job, already_paid=False)


@app.route('/razorpay/webhook', methods=['POST'])
def razorpay_webhook():
    """Server-to-server webhook from Razorpay as backup confirmation."""
    webhook_secret = os.environ.get('RAZORPAY_WEBHOOK_SECRET', '')
    payload = request.get_data()
    received_sig = request.headers.get('X-Razorpay-Signature', '')

    if webhook_secret:
        expected = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_sig):
            return 'Invalid signature', 400

    event = request.json
    if event and event.get('event') == 'payment.captured':
        payment = event['payload']['payment']['entity']
        order_id = payment.get('order_id')
        payment_id = payment.get('id')
        db = get_db()
        job = db.execute("SELECT * FROM jobs WHERE razorpay_order_id=?", (order_id,)).fetchone()
        if job and job['payment_status'] != 'paid':
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            db.execute("""
                UPDATE jobs SET payment_status='paid', payment_received_at=?,
                razorpay_payment_id=?, status='payment_received', updated_at=?
                WHERE razorpay_order_id=?
            """, (now, payment_id, now, order_id))
            db.execute(
                "INSERT INTO job_logs (job_id, action, performed_by, details) VALUES (?,?,?,?)",
                (job['job_id'], 'Payment Confirmed via Webhook', None, f'Payment ID: {payment_id}')
            )
            db.commit()
    return 'OK', 200


init_db()  

if __name__ == '__main__':
    app.run(debug=True, port=5010)
