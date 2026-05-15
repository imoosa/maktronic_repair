import sqlite3
import os

DB_PATH = 'maktronics.db'

def fix_database():
    """Completely rebuild the database from scratch"""
    print("Starting database repair...")
    
    # Backup old database if exists
    if os.path.exists(DB_PATH):
        import shutil
        backup_name = f'{DB_PATH}.backup_{__import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")}'
        shutil.copy2(DB_PATH, backup_name)
        print(f"Backed up old database to {backup_name}")
        
        # Remove corrupted database
        os.remove(DB_PATH)
        print("Removed corrupted database")
    
    # Create fresh database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create all tables fresh
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','technician','manager')),
            name TEXT NOT NULL,
            permissions TEXT DEFAULT '{}',
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
            status TEXT DEFAULT 'sent_for_inspection',
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
            invoice_path TEXT,
            courier_name TEXT,
            courier_receipt_path TEXT,
            estimate_amount REAL DEFAULT 0,
            estimate_notes TEXT,
            estimate_sent_at TIMESTAMP,
            estimate_approved_at TIMESTAMP,
            not_repairable_reason TEXT,
            sent_back_to_customer_at TIMESTAMP,
            inspection_findings TEXT,
            invoice_generate_date TIMESTAMP,
            invoice_total_amount REAL DEFAULT 0,
            razorpay_order_id TEXT,
            razorpay_payment_id TEXT,
            payment_link TEXT,
            payment_token TEXT,
            whatsapp_message_id TEXT,
            payment_method TEXT DEFAULT 'razorpay',
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

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id INTEGER NOT NULL,
            job_id TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(recipient_id) REFERENCES users(id)
        );
    ''')
    
    # Insert default admin user
    from werkzeug.security import generate_password_hash
    cursor.execute('''
        INSERT INTO users (username, password, role, name, permissions)
        VALUES (?, ?, ?, ?, ?)
    ''', ('admin', generate_password_hash('Admin@123'), 'admin', 'Admin User', '{}'))
    
    # Insert default technicians
    cursor.execute('''
        INSERT INTO users (username, password, role, name, permissions)
        VALUES (?, ?, ?, ?, ?)
    ''', ('tech1', generate_password_hash('Tech@123'), 'technician', 'Ravi Kumar', '{}'))
    
    cursor.execute('''
        INSERT INTO users (username, password, role, name, permissions)
        VALUES (?, ?, ?, ?, ?)
    ''', ('tech2', generate_password_hash('Tech@456'), 'technician', 'Suresh Patil', '{}'))
    
    conn.commit()
    conn.close()
    
    print("Database rebuilt successfully!")
    print("\nLogin credentials:")
    print("Admin - Username: admin, Password: Admin@123")
    print("Technician - Username: tech1, Password: Tech@123")
    print("Technician - Username: tech2, Password: Tech@456")

if __name__ == '__main__':
    fix_database()
