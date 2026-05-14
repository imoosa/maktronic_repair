from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from datetime import date, datetime, timedelta
import random
import hashlib
import secrets
from functools import wraps
import os
import json
import re
from werkzeug.utils import secure_filename
import io
import base64
from sqlalchemy import text
from models import (
    db,
    SubscriptionPlan, RegisteredUser, Company, CompanyUser,
    Client, Order, StockItem,
    Invoice, InvoiceItem,
    Estimate, EstimateItem,
    PurchaseInvoice, PurchaseInvoiceItem, StockPurchaseHistory,
)

app = Flask(__name__)
app.secret_key = "nexa-erp-2024-super-secret-key-change-in-production"

# ── Database Configuration ────────────────────────────────────────────────────
# Change to SQLite - creates a file named 'maktroniks.db' in the instance folder
# You can change the path as needed
app.config["SQLALCHEMY_DATABASE_URI"] = (
    'sqlite:///' + os.path.join(os.path.abspath(os.path.dirname(__file__)), 'maktroniks.db')
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# SQLite doesn't support ALTER TABLE as well, so we'll need to handle that
# by setting a pragma for foreign key enforcement
@app.before_request
def before_request():
    if db.engine.url.drivername == 'sqlite':
        db.session.execute(text('PRAGMA foreign_keys=ON'))

db.init_app(app)

# ── Create tables and seed on first startup (works with Gunicorn / Render) ────
with app.app_context():
    db.create_all()
    # seed_database() is defined later in this file; called after all models load

UPLOAD_FOLDER = 'uploads/purchase_invoices'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf', 'tiff', 'bmp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB

# Create upload folder if not exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ── Helper / Auth ─────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

def get_current_user():
    return session.get("user", {})

@app.context_processor
def inject_user():
    """Automatically inject `user` and `company` into every template."""
    return {
        "user": session.get("user", {}),
    }

def get_current_company():
    return session.get("active_company_id") or session.get("user", {}).get("company_id")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            flash("Please login to continue")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user.get("role") not in ["owner", "super_admin"]:
            flash("Only company owner can access this page")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

def super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user.get("role") != "super_admin":
            flash("Super admin access required")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


# ── OCR Extraction Service ────────────────────────────────────────────────────
# OCR via pytesseract has been removed. These stubs keep the rest of the
# codebase intact; the /purchase/upload-ocr endpoint will return a clear
# "not available" message instead of crashing.

def extract_invoice_from_image(image_data):
    """OCR extraction is not available on this deployment."""
    return ""


def extract_invoice_from_pdf(pdf_bytes):
    """OCR extraction is not available on this deployment."""
    return ""


def normalize_date(date_str):
    """Convert various date formats to YYYY-MM-DD"""
    if not date_str:
        return ""

    MONTH_NAMES = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
        'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12
    }

    date_str = date_str.strip()

    # Named month: "January 25, 2016" or "25 January 2016"
    for month_name, month_num in MONTH_NAMES.items():
        if month_name in date_str.lower():
            # Month Day, Year  →  January 25, 2016
            m = re.search(rf'{month_name}\s+(\d{{1,2}}),?\s+(\d{{4}})', date_str, re.IGNORECASE)
            if m:
                return f"{m.group(2)}-{month_num:02d}-{int(m.group(1)):02d}"
            # Day Month Year  →  25 January 2016
            m = re.search(rf'(\d{{1,2}})\s+{month_name}\s+(\d{{4}})', date_str, re.IGNORECASE)
            if m:
                return f"{m.group(2)}-{month_num:02d}-{int(m.group(1)):02d}"

    # YYYY-MM-DD
    m = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', date_str)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    # DD/MM/YYYY (Indian format assumed)
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', date_str)
    if m:
        day, month, year = m.group(1), m.group(2), m.group(3)
        if len(year) == 2:
            year = f"20{year}"
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    return date_str


def _extract_amount(s):
    """Parse a currency string like '$85.00' or '1,234.50' into float."""
    cleaned = re.sub(r'[^\d.]', '', s.replace(',', ''))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_invoice_data(extracted_text):
    """Intelligently parse OCR extracted text to extract invoice information"""
    
    if not extracted_text or len(extracted_text.strip()) < 10:
        return {
            "invoice_number": "",
            "date": "",
            "due_date": "",
            "supplier_name": "",
            "supplier_gst": "",
            "items": [],
            "subtotal": 0,
            "tax_amount": 0,
            "grand_total": 0,
            "payment_terms": "",
            "error": "Could not extract text"
        }
    
    data = {
        "invoice_number": "",
        "date": "",
        "due_date": "",
        "supplier_name": "",
        "supplier_gst": "",
        "items": [],
        "subtotal": 0,
        "tax_amount": 0,
        "grand_total": 0,
        "payment_terms": ""
    }
    
    print("=== OCR Extracted Text ===")
    print(extracted_text[:1000])
    print("==========================")
    
    # Clean and prepare lines
    lines = [line.strip() for line in extracted_text.split('\n') if line.strip()]
    
    # ========== 1. SMART DATE PARSING ==========
    def parse_smart_date(date_str):
        """Convert any date format to YYYY-MM-DD"""
        date_str = date_str.strip()
        
        # Month name to number mapping
        months = {
            'january': 1, 'jan': 1, 'jan.': 1,
            'february': 2, 'feb': 2, 'feb.': 2,
            'march': 3, 'mar': 3, 'mar.': 3,
            'april': 4, 'apr': 4, 'apr.': 4,
            'may': 5,
            'june': 6, 'jun': 6, 'jun.': 6,
            'july': 7, 'jul': 7, 'jul.': 7,
            'august': 8, 'aug': 8, 'aug.': 8,
            'september': 9, 'sep': 9, 'sept': 9, 'sep.': 9,
            'october': 10, 'oct': 10, 'oct.': 10,
            'november': 11, 'nov': 11, 'nov.': 11,
            'december': 12, 'dec': 12, 'dec.': 12
        }
        
        # Try to parse named month format (e.g., "January 25, 2016" or "25 Jan 2016")
        for month_name, month_num in months.items():
            if month_name in date_str.lower():
                # Pattern: Month Day, Year or Day Month Year
                patterns = [
                    rf'{month_name}\s+(\d{{1,2}})[,)]?\s+(\d{{4}})',  # Month Day, Year
                    rf'(\d{{1,2}})\s+{month_name}\s+(\d{{4}})',        # Day Month Year
                ]
                for pattern in patterns:
                    match = re.search(pattern, date_str, re.IGNORECASE)
                    if match:
                        if match.group(1).isdigit() and len(match.group(1)) <= 2:
                            day = int(match.group(1))
                            year = int(match.group(2))
                            return f"{year}-{month_num:02d}-{day:02d}"
        
        # Try numeric formats
        # DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
        match = re.search(r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})', date_str)
        if match:
            day, month, year = match.groups()
            year = int(year)
            if year < 100:
                year = 2000 + year
            day = int(day)
            month = int(month)
            # If day > 12, assume format is DD/MM/YYYY
            if day > 12:
                return f"{year}-{month:02d}-{day:02d}"
            else:
                # Assume MM/DD/YYYY (common in US) or DD/MM/YYYY?
                # Let's check if month > 12, then it must be DD/MM/YYYY
                if month > 12:
                    return f"{year}-{day:02d}-{month:02d}"
                else:
                    # Default to DD/MM/YYYY for Indian invoices
                    return f"{year}-{month:02d}-{day:02d}"
        
        # YYYY-MM-DD format
        match = re.search(r'(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})', date_str)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        
        return date_str
    
    # ========== 2. FIND ALL DATES IN DOCUMENT ==========
    dates_found = []
    for line in lines:
        # Look for date patterns
        if re.search(r'\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}', line) or \
           re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}', line.lower()):
            dates_found.append(line)
    
    # Extract dates (first is invoice date, second might be due date)
    if dates_found:
        date_matches = re.findall(r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})', dates_found[0], re.IGNORECASE)
        if date_matches:
            data['date'] = parse_smart_date(date_matches[0])
            print(f"✓ Invoice Date: {data['date']}")
        
        if len(dates_found) > 1:
            date_matches2 = re.findall(r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})', dates_found[1], re.IGNORECASE)
            if date_matches2:
                data['due_date'] = parse_smart_date(date_matches2[0])
                print(f"✓ Due Date: {data['due_date']}")
    
    # ========== 3. EXTRACT INVOICE NUMBER (Anywhere in document) ==========
    invoice_patterns = [
        r'(?:Invoice|Order|Bill)\s*(?:Number|No|#)?\s*[:.\-]?\s*([A-Z0-9\-/]+)',
        r'(?:INV|INV-|INV#)\s*[:.\-]?\s*([A-Z0-9\-/]+)',
        r'([A-Z0-9]{2,}[\/\-][0-9]{4,})',  # Pattern like ORD-2024-001
        r'(\d{4,}[\/\-][A-Z0-9]+)',         # Pattern like 2024-ORD001
    ]
    
    for pattern in invoice_patterns:
        match = re.search(pattern, extracted_text, re.IGNORECASE)
        if match:
            data['invoice_number'] = match.group(1).strip()
            print(f"✓ Invoice Number: {data['invoice_number']}")
            break
    
    # ========== 4. EXTRACT SUPPLIER NAME ==========
    # Look for "From", "Supplier", "Vendor", "Seller" sections
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(keyword in line_lower for keyword in ['from:', 'supplier:', 'vendor:', 'seller:', 'bill from:']):
            # Get the next few lines for the supplier name
            for j in range(i + 1, min(i + 5, len(lines))):
                candidate = lines[j]
                # Skip if it looks like address lines (contains numbers or common address words)
                if not re.search(r'\d{5,}', candidate) and not any(x in candidate.lower() for x in ['street', 'road', 'lane', 'avenue']):
                    if len(candidate) > 3 and len(candidate) < 100:
                        data['supplier_name'] = candidate
                        print(f"✓ Supplier: {data['supplier_name']}")
                        break
            if data['supplier_name']:
                break
    
    # If not found, try to find company name in first few lines
    if not data['supplier_name']:
        for line in lines[:10]:
            if len(line) > 5 and len(line) < 50 and not re.search(r'\d', line):
                data['supplier_name'] = line
                print(f"✓ Supplier (fallback): {data['supplier_name']}")
                break
    
    # ========== 5. EXTRACT PAYMENT TERMS ==========
    payment_keywords = ['payment due', 'terms', 'net \d+', 'due within', 'payment terms']
    payment_pattern = '|'.join(payment_keywords)
    for line in lines:
        if re.search(payment_pattern, line.lower()):
            data['payment_terms'] = line
            print(f"✓ Payment Terms: {data['payment_terms'][:50]}")
            break
    
    # ========== 6. SMART TABLE DETECTION ==========
    # First, identify where the items table might be
    table_start = -1
    column_mapping = {}
    
    # Common column headers and their mappings
    header_mapping = {
        'description': ['description', 'item', 'product', 'particulars', 'service', 'goods', 'name'],
        'quantity': ['qty', 'quantity', 'qnty', 'quan', 'unit', 'pieces', 'pcs'],
        'rate': ['rate', 'price', 'unit price', 'cost', 'selling price', '₹', 'rs'],
        'amount': ['amount', 'total', 'value', 'subtotal', 'net'],
        'tax': ['tax', 'gst', 'vat', 'cgst', 'sgst', 'igst'],
        'discount': ['discount', 'disc', 'off']
    }
    
    # Find table headers
    for i, line in enumerate(lines):
        line_lower = line.lower()
        for col_type, keywords in header_mapping.items():
            for keyword in keywords:
                if keyword in line_lower and len(line) < 100:  # Header lines are usually short
                    column_mapping[col_type] = i
                    print(f"✓ Found column '{col_type}' at line {i}")
                    
        # If we found multiple headers, this is likely the table start
        if len(column_mapping) >= 2:
            table_start = i
            break
    
    # Extract table data if we found headers
    if table_start >= 0:
        print(f"✓ Table detected starting at line {table_start}")
        
        # Get the header line to understand column positions
        header_line = lines[table_start]
        print(f"  Header: {header_line}")
        
        # Find column positions by splitting on whitespace
        header_parts = re.split(r'\s{2,}', header_line)  # Split on multiple spaces
        
        # Now look at rows below the header
        for i in range(table_start + 1, min(table_start + 30, len(lines))):
            row = lines[i]
            
            # Skip if row looks like total line
            if any(keyword in row.lower() for keyword in ['total', 'sub total', 'grand total', 'tax', 'payment']):
                break
            
            # Extract numbers from the row
            numbers = re.findall(r'(\d+(?:\.\d+)?)', row)
            
            # Clean the description (remove numbers and special chars)
            description = re.sub(r'\d+(?:\.\d+)?', '', row)
            description = re.sub(r'[^\w\s\-\.]', '', description).strip()
            
            # Try to map columns based on position
            if description and len(description) > 2:
                item = {
                    "description": description[:80],
                    "quantity": 1,
                    "rate": 0,
                    "amount": 0,
                    "tax": 0,
                    "discount": 0
                }
                
                # Map numbers to fields based on what columns we found
                if 'quantity' in column_mapping and len(numbers) > 0:
                    item['quantity'] = float(numbers[0]) if numbers else 1
                    numbers = numbers[1:] if len(numbers) > 1 else []
                
                if 'rate' in column_mapping and len(numbers) > 0:
                    item['rate'] = float(numbers[0]) if numbers else 0
                    numbers = numbers[1:] if len(numbers) > 1 else []
                
                if 'amount' in column_mapping and len(numbers) > 0:
                    item['amount'] = float(numbers[0]) if numbers else 0
                elif len(numbers) > 0:
                    # If no amount column, last number is likely amount
                    item['amount'] = float(numbers[-1]) if numbers else 0
                    if item['rate'] == 0 and item['amount'] > 0:
                        item['rate'] = item['amount'] / item['quantity'] if item['quantity'] > 0 else item['amount']
                
                if 'tax' in column_mapping and len(numbers) > 0:
                    item['tax'] = float(numbers[0]) if numbers else 0
                
                # Only add if amount > 0
                if item['amount'] > 0 and description:
                    data['items'].append(item)
                    print(f"  Item: {description[:30]} | Qty: {item['quantity']} | Rate: ₹{item['rate']:.2f} | Amount: ₹{item['amount']:.2f}")
    
    # ========== 7. FALLBACK: Extract items if table detection failed ==========
    if not data['items']:
        print("✓ Using fallback item extraction")
        for line in lines:
            # Skip short lines and lines with common non-item words
            if len(line) < 10:
                continue
            if any(skip in line.lower() for skip in ['total', 'tax', 'invoice', 'date', 'from:', 'to:', 'payment', 'subtotal']):
                continue
            
            # Find numbers in the line
            numbers = re.findall(r'(\d+(?:\.\d+)?)', line)
            
            if numbers:
                # Clean description
                description = re.sub(r'\d+(?:\.\d+)?', '', line)
                description = re.sub(r'[^\w\s\-\.]', '', description).strip()
                
                if description and len(description) > 3:
                    amount = float(numbers[-1]) if numbers else 0
                    if amount > 0:
                        item = {
                            "description": description[:60],
                            "quantity": float(numbers[0]) if len(numbers) >= 2 else 1,
                            "rate": float(numbers[1]) if len(numbers) >= 3 else amount,
                            "amount": amount
                        }
                        
                        # Adjust if rate seems wrong
                        if item['rate'] > item['amount'] and item['quantity'] > 1:
                            item['rate'] = item['amount'] / item['quantity']
                        
                        data['items'].append(item)
                        print(f"  Item: {description[:30]} | Qty: {item['quantity']} | Rate: ₹{item['rate']:.2f} | Amount: ₹{item['amount']:.2f}")
    
    # ========== 8. EXTRACT AMOUNTS (Total, Tax, Subtotal) ==========
    # Find total amount
    total_patterns = [
        r'(?:Total|Grand Total|Amount Due|Net Payable)\s*:?\s*[₹\$]?\s*([0-9,]+\.?\d*)',
        r'TOTAL\s+[A-Z]?\s*[₹\$]?\s*([0-9,]+\.?\d*)',
        r'[₹\$]\s*([0-9,]+\.?\d*)\s*(?:Total|Due)',
    ]
    
    for pattern in total_patterns:
        match = re.search(pattern, extracted_text, re.IGNORECASE)
        if match:
            data['grand_total'] = float(match.group(1).replace(',', ''))
            print(f"✓ Grand Total: ₹{data['grand_total']}")
            break
    
    # Find tax amount
    tax_patterns = [
        r'(?:Tax|GST|VAT|CGST|SGST|IGST)\s*:?\s*[₹\$]?\s*([0-9,]+\.?\d*)',
        r'Total Tax\s*:?\s*[₹\$]?\s*([0-9,]+\.?\d*)',
    ]
    
    for pattern in tax_patterns:
        match = re.search(pattern, extracted_text, re.IGNORECASE)
        if match:
            data['tax_amount'] = float(match.group(1).replace(',', ''))
            print(f"✓ Tax Amount: ₹{data['tax_amount']}")
            break
    
    # Find subtotal
    subtotal_patterns = [
        r'(?:Sub Total|Subtotal|Taxable Value)\s*:?\s*[₹\$]?\s*([0-9,]+\.?\d*)',
    ]
    
    for pattern in subtotal_patterns:
        match = re.search(pattern, extracted_text, re.IGNORECASE)
        if match:
            data['subtotal'] = float(match.group(1).replace(',', ''))
            print(f"✓ Subtotal: ₹{data['subtotal']}")
            break
    
    # Calculate missing values
    if data['grand_total'] > 0 and data['subtotal'] == 0:
        if data['tax_amount'] > 0:
            data['subtotal'] = data['grand_total'] - data['tax_amount']
        else:
            # Assume 18% GST if not specified
            data['subtotal'] = data['grand_total'] / 1.18
            data['tax_amount'] = data['grand_total'] - data['subtotal']
            print(f"✓ Estimated 18% GST: Subtotal ₹{data['subtotal']:.2f}, Tax ₹{data['tax_amount']:.2f}")
    
    # Calculate from items if totals missing
    if data['grand_total'] == 0 and data['items']:
        data['grand_total'] = sum(item['amount'] for item in data['items'])
        print(f"✓ Calculated total from items: ₹{data['grand_total']}")
    
    # ========== 9. FINAL SUMMARY ==========
    print("\n" + "="*50)
    print("PARSING SUMMARY")
    print("="*50)
    print(f"📄 Invoice Number: {data['invoice_number'] or 'Not found'}")
    print(f"📅 Date: {data['date'] or 'Not found'}")
    print(f"📅 Due Date: {data['due_date'] or 'Not found'}")
    print(f"🏢 Supplier: {data['supplier_name'] or 'Not found'}")
    print(f"💳 Payment Terms: {data['payment_terms'] or 'Not found'}")
    print(f"📦 Items Found: {len(data['items'])}")
    print(f"💰 Subtotal: ₹{data['subtotal']:.2f}" if data['subtotal'] else "💰 Subtotal: Not found")
    print(f"🧾 Tax: ₹{data['tax_amount']:.2f}" if data['tax_amount'] else "🧾 Tax: Not found")
    print(f"💵 Total: ₹{data['grand_total']:.2f}" if data['grand_total'] else "💵 Total: Not found")
    print("="*50 + "\n")
    
    return data

# ── Seed Data ─────────────────────────────────────────────────────────────────
SUBSCRIPTION_PLANS_DATA = {
    "basic": {
        "name": "Basic Plan",
        "price": "999",
        "max_companies": "2",
        "max_users": "5",
        "features": "Basic Analytics,Order Management,Client Management,Email Support",
    },
    "premium": {
        "name": "Premium Plan",
        "price": "2499",
        "max_companies": "5",
        "max_users": "15",
        "features": "Advanced Analytics,Inventory Management,Invoice & Estimates,Priority Support,API Access",
    },
    "gold": {
        "name": "Gold Plan",
        "price": "4999",
        "max_companies": "10",
        "max_users": "35",
        "features": "All Premium Features,Custom Reports,Dedicated Account Manager,24/7 Support,White-label Option",
    },
    "custom": {
        "name": "Custom Plan",
        "price": "Contact Sales",
        "max_companies": "Unlimited",
        "max_users": "Unlimited",
        "features": "Fully Customizable,On-premise Deployment,Training Included,Custom Development",
    },
}

def seed_database():
    """Insert initial plans, users and sample data if the DB is empty."""

    # ── Subscription Plans
    if SubscriptionPlan.query.count() == 0:
        for plan_id, data in SUBSCRIPTION_PLANS_DATA.items():
            db.session.add(SubscriptionPlan(
                id=plan_id,
                name=data["name"],
                price=data["price"],
                max_companies=data["max_companies"],
                max_users=data["max_users"],
                features=data["features"],
            ))
        db.session.commit()
        print("✔  Subscription plans seeded.")

    # ── Registered Users
    if RegisteredUser.query.count() == 0:
        admin = RegisteredUser(
            user_id="USR001",
            email="admin@nexa.com",
            password_hash=hash_password("Admin@123"),
            full_name="System Admin",
            phone="9999999999",
            role="super_admin",
            subscription_plan=None,
            created_at=date(2024, 1, 1),
            is_active=True,
        )
        rahul = RegisteredUser(
            user_id="USR002",
            email="rahul@techsolutions.com",
            password_hash=hash_password("Tech@123"),
            full_name="Rahul Sharma",
            phone="9876543210",
            role="owner",
            subscription_plan="premium",
            created_at=date(2024, 1, 1),
            is_active=True,
        )
        priya_reg = RegisteredUser(
            user_id="USR003",
            email="priya@globaltraders.com",
            password_hash=hash_password("Global@123"),
            full_name="Priya Singh",
            phone="9876543211",
            role="owner",
            subscription_plan="basic",
            created_at=date(2024, 1, 15),
            is_active=True,
        )
        db.session.add_all([admin, rahul, priya_reg])
        db.session.commit()
        print("✔  Registered users seeded.")

    # ── Companies
    if Company.query.count() == 0:
        comp1 = Company(
            company_id="COMP001",
            company_name="Tech Solutions India",
            owner_email="rahul@techsolutions.com",
            subscription_plan="premium",
            subscription_start=date(2024, 1, 1),
            subscription_end=date(2025, 1, 1),
            max_companies_allowed="5",
            max_users_per_company="15",
            gst_number="27AAABC1234F1Z",
            address="Mumbai, Maharashtra",
            phone="9876543210",
            created_at=date(2024, 1, 1),
            is_active=True,
        )
        comp2 = Company(
            company_id="COMP002",
            company_name="Global Traders Ltd",
            owner_email="priya@globaltraders.com",
            subscription_plan="basic",
            subscription_start=date(2024, 1, 15),
            subscription_end=date(2024, 7, 15),
            max_companies_allowed="2",
            max_users_per_company="5",
            gst_number="29AABCB5678F1Z",
            address="Delhi, India",
            phone="9876543211",
            created_at=date(2024, 1, 15),
            is_active=True,
        )
        comp3 = Company(
            company_id="COMP003",
            company_name="Rahul Exports Pvt Ltd",
            owner_email="rahul@techsolutions.com",
            subscription_plan="premium",
            subscription_start=date(2024, 3, 1),
            subscription_end=date(2025, 3, 1),
            max_companies_allowed="5",
            max_users_per_company="15",
            gst_number="27AAABC9999F1Z",
            address="Pune, Maharashtra",
            phone="9876543299",
            created_at=date(2024, 3, 1),
            is_active=True,
        )
        db.session.add_all([comp1, comp2, comp3])
        db.session.commit()
        print("✔  Companies seeded.")

    # ── Company Users
    if CompanyUser.query.count() == 0:
        users = [
            CompanyUser(user_id="EMP001", company_id="COMP001", email="rahul@techsolutions.com",
                        password_hash=hash_password("Tech@123"), full_name="Rahul Sharma",
                        role="owner", department="Management", phone="9876543201",
                        is_active=True, created_at=date(2024, 1, 1)),
            CompanyUser(user_id="EMP002", company_id="COMP001", email="priya.mehta@techsolutions.com",
                        password_hash=hash_password("Priya@123"), full_name="Priya Mehta",
                        role="sales_manager", department="Sales", phone="9876543202",
                        is_active=True, created_at=date(2024, 1, 1)),
            CompanyUser(user_id="EMP003", company_id="COMP001", email="arjun.nair@techsolutions.com",
                        password_hash=hash_password("Arjun@123"), full_name="Arjun Nair",
                        role="accountant", department="Accounts", phone="9876543203",
                        is_active=True, created_at=date(2024, 1, 2)),
            CompanyUser(user_id="EMP101", company_id="COMP002", email="priya@globaltraders.com",
                        password_hash=hash_password("Global@123"), full_name="Priya Singh",
                        role="owner", department="Management", phone="9876543211",
                        is_active=True, created_at=date(2024, 1, 15)),
            CompanyUser(user_id="EMP102", company_id="COMP002", email="amit@globaltraders.com",
                        password_hash=hash_password("Amit@123"), full_name="Amit Kumar",
                        role="sales_executive", department="Sales", phone="9876543212",
                        is_active=True, created_at=date(2024, 1, 15)),
            CompanyUser(user_id="EMP201", company_id="COMP003", email="rahul@techsolutions.com",
                        password_hash=hash_password("Tech@123"), full_name="Rahul Sharma",
                        role="owner", department="Management", phone="9876543299",
                        is_active=True, created_at=date(2024, 3, 1)),
        ]
        db.session.add_all(users)
        db.session.commit()
        print("✔  Company users seeded.")

    # ── Sample Clients
    if Client.query.count() == 0:
        clients = [
            Client(company_id="COMP001", name="Reliance Industries", phone="9876543210",
                   pending=0, last_payment=date(2024, 1, 22), status="Paid"),
            Client(company_id="COMP001", name="Tata Consultancy", phone="9876543211",
                   pending=89500, last_payment=date(2024, 1, 5), status="Pending"),
            Client(company_id="COMP001", name="Infosys Ltd", phone="9876543212",
                   pending=86000, last_payment=date(2024, 1, 18), status="Active"),
            Client(company_id="COMP002", name="HDFC Bank", phone="9876543217",
                   pending=156000, last_payment=date(2024, 1, 1), status="Pending"),
            Client(company_id="COMP002", name="ICICI Bank", phone="9876543218",
                   pending=0, last_payment=date(2024, 1, 21), status="Paid"),
        ]
        db.session.add_all(clients)
        db.session.commit()
        print("✔  Clients seeded.")

    # ── Sample Stock Items (COMP001)
    if StockItem.query.count() == 0:
        items = [
            StockItem(company_id="COMP001", code="PROD001", name="LED TV 43 inch",
                      category="Electronics", quantity=25, unit="pcs", unit_price=35000,
                      reorder_level=10, last_updated=date(2024, 1, 20)),
            StockItem(company_id="COMP001", code="PROD002", name="Smartphone X",
                      category="Electronics", quantity=50, unit="pcs", unit_price=25000,
                      reorder_level=20, last_updated=date(2024, 1, 20)),
        ]
        db.session.add_all(items)
        db.session.commit()
        print("✔  Stock items seeded.")

    # ── Sample Orders (COMP001)
    if Order.query.count() == 0:
        c1 = Client.query.filter_by(company_id="COMP001", name="Reliance Industries").first()
        c2 = Client.query.filter_by(company_id="COMP001", name="Tata Consultancy").first()
        c3 = Client.query.filter_by(company_id="COMP001", name="Infosys Ltd").first()
        hd = Client.query.filter_by(company_id="COMP002", name="HDFC Bank").first()
        ic = Client.query.filter_by(company_id="COMP002", name="ICICI Bank").first()

        orders = [
            Order(order_id="ORD-2024-001", company_id="COMP001",
                  client_id=c1.id if c1 else None, employee_id="EMP001",
                  date=date(2024, 1, 15), amount=245000, received=245000, status="Delivered"),
            Order(order_id="ORD-2024-002", company_id="COMP001",
                  client_id=c2.id if c2 else None, employee_id="EMP002",
                  date=date(2024, 1, 17), amount=89500, received=0, status="Pending"),
            Order(order_id="ORD-2024-003", company_id="COMP001",
                  client_id=c3.id if c3 else None, employee_id="EMP001",
                  date=date(2024, 1, 18), amount=172000, received=86000, status="Processing"),
            Order(order_id="ORD-2024-101", company_id="COMP002",
                  client_id=hd.id if hd else None, employee_id="EMP101",
                  date=date(2024, 1, 20), amount=156000, received=0, status="Pending"),
            Order(order_id="ORD-2024-102", company_id="COMP002",
                  client_id=ic.id if ic else None, employee_id="EMP102",
                  date=date(2024, 1, 21), amount=89000, received=89000, status="Delivered"),
        ]
        db.session.add_all(orders)
        db.session.commit()
        print("✔  Orders seeded.")

    print("✅ Database seeding complete.")


# ─────────────────────────────────────────────────────────────────────────────
# ── Plan helper (replaces the old SUBSCRIPTION_PLANS dict) ───────────────────
# ─────────────────────────────────────────────────────────────────────────────
def get_plan(plan_id):
    p = SubscriptionPlan.query.get(plan_id)
    if not p:
        return {}
    return {
        "name": p.name,
        "price": p.price,
        "max_companies": p.max_companies,
        "max_users_per_company": p.max_users,
        "features": p.features.split(",") if p.features else [],
    }

def get_all_plans():
    return {p.id: get_plan(p.id) for p in SubscriptionPlan.query.all()}


# ── Company helpers ───────────────────────────────────────────────────────────
def get_company_by_id(company_id):
    return Company.query.filter_by(company_id=company_id).first()

def get_owner_companies(owner_email):
    return Company.query.filter_by(owner_email=owner_email, is_active=True).all()

def check_company_limit(company_id, user_type="user"):
    company = get_company_by_id(company_id)
    if not company:
        return False, "Company not found"
    plan = get_plan(company.subscription_plan)
    if user_type == "user":
        current = CompanyUser.query.filter_by(company_id=company_id, is_active=True).count()
        max_u = plan.get("max_users_per_company", 5)
        try:
            max_u = int(max_u)
            if current >= max_u:
                return False, f"Maximum {max_u} users allowed in your {plan['name']}. Please upgrade."
        except (ValueError, TypeError):
            pass  # "Unlimited"
    return True, "OK"

def check_new_company_limit(owner_email):
    comps = get_owner_companies(owner_email)
    if not comps:
        return True, "OK"
    plan = get_plan(comps[0].subscription_plan)
    max_c = plan.get("max_companies", 2)
    try:
        max_c = int(max_c)
        if len(comps) >= max_c:
            return False, f"Your {plan['name']} allows up to {max_c} companies. Please upgrade."
    except (ValueError, TypeError):
        pass  # "Unlimited"
    return True, "OK"


# ─────────────────────────────────────────────────────────────────────────────
# ── Auth Routes ───────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # Super-admin / registered-user login
        reg_user = RegisteredUser.query.filter_by(email=email, is_active=True).first()
        if reg_user and verify_password(password, reg_user.password_hash):
            if reg_user.role == "super_admin":
                session["user"] = {
                    "user_id": reg_user.user_id, "email": reg_user.email,
                    "full_name": reg_user.full_name, "role": "super_admin",
                    "company_id": None,
                }
                return redirect(url_for("admin_dashboard"))

            # Owner: may have multiple companies
            companies = get_owner_companies(email)
            if len(companies) == 1:
                c = companies[0]
                session["user"] = {
                    "user_id": reg_user.user_id, "email": reg_user.email,
                    "full_name": reg_user.full_name, "role": reg_user.role,
                    "company_id": c.company_id,
                }
                session["active_company_id"] = c.company_id
                return redirect(url_for("dashboard"))
            elif len(companies) > 1:
                session["pending_login_email"] = email
                return redirect(url_for("select_company"))

        # Company employee login
        emp = CompanyUser.query.filter_by(email=email, is_active=True).first()
        if emp and verify_password(password, emp.password_hash):
            session["user"] = {
                "user_id": emp.user_id, "email": emp.email,
                "full_name": emp.full_name, "role": emp.role,
                "company_id": emp.company_id,
            }
            session["active_company_id"] = emp.company_id
            return redirect(url_for("dashboard"))

        flash("Invalid email or password")
    return render_template("login.html")


@app.route("/select-company", methods=["GET", "POST"])
def select_company():
    owner_email = session.get("pending_login_email") or session.get("user", {}).get("email")
    if not owner_email:
        return redirect(url_for("login"))

    if request.method == "POST":
        company_id = request.form.get("company_id")
        company = get_company_by_id(company_id)
        if company and company.owner_email == owner_email:
            reg_user = RegisteredUser.query.filter_by(email=owner_email).first()
            session["user"] = {
                "email":     reg_user.email,
                "full_name": reg_user.full_name,
                "role":      reg_user.role,
                "user_id":   reg_user.user_id,
            }
            session["active_company_id"] = company_id
            session.pop("pending_login_email", None)
            return redirect(url_for("dashboard"))
        flash("Invalid company selection.")

    companies = get_owner_companies(owner_email)
    user = get_current_user()
    if not user:
        reg_user = RegisteredUser.query.filter_by(email=owner_email).first()
        user = {"full_name": reg_user.full_name, "email": reg_user.email} if reg_user else {"full_name": owner_email, "email": owner_email}
    return render_template("select_company.html", companies=companies, user=user)


@app.route("/switch-company/<company_id>")
@login_required
def switch_company(company_id):
    user = get_current_user()
    company = get_company_by_id(company_id)
    if company and company.owner_email == user.get("email"):
        session["active_company_id"] = company_id
        flash(f"Switched to {company.company_name}")
    return redirect(url_for("dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email             = request.form.get("email", "").strip().lower()
        password          = request.form.get("password", "")
        confirm_password  = request.form.get("confirm_password", "")
        full_name         = request.form.get("full_name", "")
        phone             = request.form.get("phone", "")
        company_name      = request.form.get("company_name", "")
        subscription_plan = request.form.get("subscription_plan", "basic")

        if RegisteredUser.query.filter_by(email=email).first():
            flash("Email already registered"); return redirect(url_for("register"))
        if password != confirm_password:
            flash("Passwords do not match"); return redirect(url_for("register"))
        if len(password) < 6:
            flash("Password must be at least 6 characters"); return redirect(url_for("register"))

        plan_obj = SubscriptionPlan.query.get(subscription_plan) or SubscriptionPlan.query.get("basic")
        reg_count = RegisteredUser.query.count()
        user_id   = f"USR{reg_count + 1:03d}"

        new_user = RegisteredUser(
            user_id=user_id, email=email, password_hash=hash_password(password),
            full_name=full_name, phone=phone, role="owner",
            subscription_plan=plan_obj.id, created_at=date.today(), is_active=True,
        )
        db.session.add(new_user)
        db.session.flush()

        comp_count  = Company.query.count()
        company_id  = f"COMP{comp_count + 1:03d}"
        end_days    = 730 if plan_obj.id == "custom" else 365
        new_company = Company(
            company_id=company_id, company_name=company_name,
            owner_email=email, subscription_plan=plan_obj.id,
            subscription_start=date.today(),
            subscription_end=date.today() + timedelta(days=end_days),
            max_companies_allowed=plan_obj.max_companies,
            max_users_per_company=plan_obj.max_users,
            gst_number=request.form.get("gst_number", ""),
            address=request.form.get("address", ""),
            phone=phone, created_at=date.today(), is_active=True,
        )
        db.session.add(new_company)
        db.session.flush()

        emp_count = CompanyUser.query.count()
        emp_id    = f"EMP{emp_count + 1:03d}"
        new_emp   = CompanyUser(
            user_id=emp_id, company_id=company_id, email=email,
            password_hash=hash_password(password), full_name=full_name,
            role="owner", department="Management", phone=phone,
            is_active=True, created_at=date.today(),
        )
        db.session.add(new_emp)
        db.session.commit()

        flash("Registration successful! Please login.")
        return redirect(url_for("login"))

    return render_template("register.html", plans=get_all_plans())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────────────────────────────────────
# ── Dashboard ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    company_id = get_current_company()
    company    = get_company_by_id(company_id)

    orders    = Order.query.filter_by(company_id=company_id).all()
    clients   = Client.query.filter_by(company_id=company_id).all()
    employees = CompanyUser.query.filter_by(company_id=company_id, is_active=True).all()
    invoices  = Invoice.query.filter_by(company_id=company_id).all()
    purchases = PurchaseInvoice.query.filter_by(company_id=company_id).all()
    stock     = StockItem.query.filter_by(company_id=company_id).all()

    total_revenue   = sum(o.amount    for o in orders)
    total_received  = sum(o.received  for o in orders)
    pending_orders  = [o for o in orders if o.status == "Pending"]

    # Invoice billing totals
    total_billing   = sum(i.grand_total  for i in invoices)
    total_inv_paid  = sum((i.grand_total - getattr(i, "balance", 0)) for i in invoices)
    total_inv_due   = sum(getattr(i, "balance", 0) for i in invoices)

    # Purchase totals
    total_purchases = sum(p.grand_total  for p in purchases)
    total_pur_paid  = sum(p.paid_amount  for p in purchases)
    total_pur_due   = sum(p.balance      for p in purchases)

    # Stock
    low_stock       = [s for s in stock if s.quantity <= s.reorder_level]
    total_stock_val = sum((s.purchase_rate or 0) * s.quantity for s in stock)

    stats = {
        # Orders
        "total_orders":    len(orders),
        "total_revenue":   total_revenue,
        "total_received":  total_received,
        "pending_amount":  total_revenue - total_received,
        "pending_orders":  len(pending_orders),
        # Clients / Employees
        "total_clients":   len(clients),
        "total_employees": len(employees),
        # Invoices / Billing
        "total_billing":   total_billing,
        "total_inv_paid":  total_inv_paid,
        "total_inv_due":   total_inv_due,
        "total_invoices":  len(invoices),
        # Purchases
        "total_purchases": total_purchases,
        "total_pur_paid":  total_pur_paid,
        "total_pur_due":   total_pur_due,
        "total_purchase_count": len(purchases),
        # Stock
        "total_stock_items": len(stock),
        "low_stock_count":   len(low_stock),
        "total_stock_value": total_stock_val,
        # Estimates
        "total_estimates": Estimate.query.filter_by(company_id=company_id).count(),
    }

    recent_orders   = sorted(orders,  key=lambda o: o.date,    reverse=True)[:5]
    recent_invoices = sorted(invoices, key=lambda i: i.date,   reverse=True)[:5]
    recent_purchases= sorted(purchases, key=lambda p: p.date,  reverse=True)[:5]
    top_clients     = sorted(clients,  key=lambda c: c.pending, reverse=True)[:5]

    user_companies = []
    user = get_current_user()
    if user.get("role") == "owner":
        user_companies = get_owner_companies(user.get("email"))

    return render_template("dashboard.html",
                           company=company,
                           stats=stats,
                           recent_orders=recent_orders,
                           recent_invoices=recent_invoices,
                           recent_purchases=recent_purchases,
                           top_clients=top_clients,
                           low_stock=low_stock,
                           user_companies=user_companies,
                           user=user)


# ─────────────────────────────────────────────────────────────────────────────
# ── Orders ────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/orders")
@login_required
def order_list():
    company_id    = get_current_company()
    filter_status = request.args.get("status", "All")
    query         = Order.query.filter_by(company_id=company_id)
    if filter_status != "All":
        query = query.filter_by(status=filter_status)
    orders  = query.order_by(Order.date.desc()).all()
    clients = Client.query.filter_by(company_id=company_id).all()
    return render_template("orders.html", orders=orders, clients=clients,
                           current_status=filter_status)


@app.route("/orders/add", methods=["GET", "POST"])
@login_required
def order_add():
    company_id = get_current_company()
    clients    = Client.query.filter_by(company_id=company_id).all()

    if request.method == "POST":
        client_id   = request.form.get("client_id")
        amount      = float(request.form.get("amount", 0))
        received    = float(request.form.get("received", 0))
        status      = request.form.get("status", "Pending")
        order_date  = request.form.get("order_date") or str(date.today())
        ord_count   = Order.query.count()
        new_order   = Order(
            order_id=f"ORD-{datetime.now().strftime('%Y%m%d')}-{ord_count+1:03d}",
            company_id=company_id,
            client_id=int(client_id) if client_id else None,
            employee_id=get_current_user().get("user_id"),
            date=date.fromisoformat(order_date),
            amount=amount, received=received, status=status,
        )
        db.session.add(new_order)
        db.session.commit()
        flash("Order created successfully!")
        return redirect(url_for("order_list"))

    return render_template("order_form.html", clients=clients)


@app.route("/orders/edit/<int:order_pk>", methods=["GET", "POST"])
@login_required
def order_edit(order_pk):
    company_id = get_current_company()
    order      = Order.query.filter_by(id=order_pk, company_id=company_id).first_or_404()
    clients    = Client.query.filter_by(company_id=company_id).all()

    if request.method == "POST":
        order.client_id = int(request.form.get("client_id")) if request.form.get("client_id") else None
        order.amount    = float(request.form.get("amount", 0))
        order.received  = float(request.form.get("received", 0))
        order.status    = request.form.get("status", "Pending")
        db.session.commit()
        flash("Order updated!")
        return redirect(url_for("order_list"))

    return render_template("order_form.html", order=order, clients=clients)


@app.route("/orders/delete/<int:order_pk>", methods=["POST"])
@login_required
def order_delete(order_pk):
    company_id = get_current_company()
    order      = Order.query.filter_by(id=order_pk, company_id=company_id).first_or_404()
    db.session.delete(order)
    db.session.commit()
    flash("Order deleted.")
    return redirect(url_for("order_list"))


# ─────────────────────────────────────────────────────────────────────────────
# ── Clients ───────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_client(c):
    """Return a dict whose keys match what clients.html / client_form.html expect."""
    return {
        # identity
        "id":              c.id,
        "client_name":     c.name,
        "client_type":     c.client_type     or "Business",
        "contact_person":  c.contact_person  or "",
        # contact
        "phone":           c.phone           or "",
        "alternate_phone": c.alternate_phone or "",
        "email":           c.email           or "",
        "website":         c.website         or "",
        # address
        "address_line1":   c.address_line1   or "",
        "address_line2":   c.address_line2   or "",
        "city":            c.city            or "",
        "state":           c.state           or "",
        "pincode":         c.pincode         or "",
        "country":         c.country         or "India",
        # GST & tax
        "gst_number":      c.gst_number      or "",
        "pan_number":      c.pan_number      or "",
        "gst_type":        c.gst_type        or "Regular",
        # financial
        "credit_limit":    c.credit_limit    or 0.0,
        "credit_days":     c.credit_days     or 30,
        "outstanding":     c.pending         or 0.0,
        "opening_balance": c.opening_balance or 0.0,
        "last_payment":    c.last_payment,
        # status
        "status":          c.status          or "Active",
        "notes":           c.notes           or "",
        "created_at":      c.created_at,
    }


@app.route("/clients")
@login_required
def client_list():
    company_id    = get_current_company()
    filter_status = request.args.get("status", "All")

    query = Client.query.filter_by(company_id=company_id)
    if filter_status != "All":
        query = query.filter_by(status=filter_status)

    clients = [_normalize_client(c) for c in query.all()]
    return render_template("clients.html", clients=clients, current_status=filter_status)


# /clients/new  ── template links here for new client
@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def client_new():
    company_id = get_current_company()
    if request.method == "POST":
        f = request.form

        # GST uniqueness check (per company)
        gst = f.get("gst_number", "").strip().upper()
        if gst:
            existing_gst = Client.query.filter_by(
                company_id=company_id, gst_number=gst
            ).first()
            if existing_gst:
                flash(f"GST number {gst} is already registered to client '{existing_gst.name}'. Please check and try again.", "error")
                return render_template("client_form.html", form_data=f)

        new_client = Client(
            company_id      = company_id,
            name            = f.get("client_name", "").strip(),
            client_type     = f.get("client_type", "Business"),
            contact_person  = f.get("contact_person", "").strip(),
            phone           = f.get("phone", "").strip(),
            alternate_phone = f.get("alternate_phone", "").strip(),
            email           = f.get("email", "").strip().lower(),
            website         = f.get("website", "").strip(),
            address_line1   = f.get("address_line1", "").strip(),
            address_line2   = f.get("address_line2", "").strip(),
            city            = f.get("city", "").strip(),
            state           = f.get("state", "").strip(),
            pincode         = f.get("pincode", "").strip(),
            country         = f.get("country", "India").strip(),
            gst_number      = gst or None,
            pan_number      = f.get("pan_number", "").strip().upper() or None,
            gst_type        = f.get("gst_type", "Regular"),
            credit_limit    = float(f.get("credit_limit", 0) or 0),
            credit_days     = int(f.get("credit_days", 30) or 30),
            pending         = float(f.get("opening_balance", 0) or 0),
            opening_balance = float(f.get("opening_balance", 0) or 0),
            status          = f.get("status", "Active"),
            notes           = f.get("notes", "").strip(),
            created_at      = date.today(),
        )
        db.session.add(new_client)
        db.session.commit()
        flash(f"Client '{new_client.name}' added successfully!")
        return redirect(url_for("client_list"))
    return render_template("client_form.html", form_data={})


# Keep /clients/add as an alias so old links still work
@app.route("/clients/add", methods=["GET", "POST"])
@login_required
def client_add():
    return client_new()


# /clients/<id>  ── view detail (template links here with 👁️)
@app.route("/clients/<int:client_pk>")
@login_required
def client_view(client_pk):
    company_id = get_current_company()
    c = Client.query.filter_by(id=client_pk, company_id=company_id).first_or_404()
    client = _normalize_client(c)
    invoices = Invoice.query.filter_by(company_id=company_id, client_id=c.id).order_by(Invoice.date.desc()).all()
    orders   = Order.query.filter_by(company_id=company_id, client_id=c.id).order_by(Order.date.desc()).all()
    return render_template("client_detail.html", client=client, invoices=invoices, orders=orders)


# /clients/<id>/edit
@app.route("/clients/<int:client_pk>/edit", methods=["GET", "POST"])
@login_required
def client_edit(client_pk):
    company_id = get_current_company()
    c          = Client.query.filter_by(id=client_pk, company_id=company_id).first_or_404()
    if request.method == "POST":
        f   = request.form
        gst = f.get("gst_number", "").strip().upper()

        # GST uniqueness: check no OTHER client has the same GST
        if gst:
            existing_gst = Client.query.filter(
                Client.company_id == company_id,
                Client.gst_number == gst,
                Client.id != c.id
            ).first()
            if existing_gst:
                flash(f"GST number {gst} is already registered to client '{existing_gst.name}'.", "error")
                return render_template("client_form.html", client=_normalize_client(c), form_data=f)

        c.name            = f.get("client_name", c.name).strip()
        c.client_type     = f.get("client_type",     c.client_type)
        c.contact_person  = f.get("contact_person",  c.contact_person or "").strip()
        c.phone           = f.get("phone",            c.phone or "").strip()
        c.alternate_phone = f.get("alternate_phone",  c.alternate_phone or "").strip()
        c.email           = f.get("email",            c.email or "").strip().lower()
        c.website         = f.get("website",          c.website or "").strip()
        c.address_line1   = f.get("address_line1",    c.address_line1 or "").strip()
        c.address_line2   = f.get("address_line2",    c.address_line2 or "").strip()
        c.city            = f.get("city",             c.city or "").strip()
        c.state           = f.get("state",            c.state or "").strip()
        c.pincode         = f.get("pincode",          c.pincode or "").strip()
        c.country         = f.get("country",          c.country or "India").strip()
        c.gst_number      = gst or None
        c.pan_number      = f.get("pan_number",  c.pan_number or "").strip().upper() or None
        c.gst_type        = f.get("gst_type",    c.gst_type)
        c.credit_limit    = float(f.get("credit_limit",    c.credit_limit    or 0) or 0)
        c.credit_days     = int(f.get("credit_days",       c.credit_days     or 30) or 30)
        c.opening_balance = float(f.get("opening_balance", c.opening_balance or 0) or 0)
        c.status          = f.get("status", c.status)
        c.notes           = f.get("notes",   c.notes or "").strip()
        db.session.commit()
        flash(f"Client '{c.name}' updated successfully!")
        return redirect(url_for("client_list"))
    return render_template("client_form.html", client=_normalize_client(c), form_data={})


# /clients/<id>/delete  ── template uses GET link with confirm dialog
@app.route("/clients/<int:client_pk>/delete", methods=["GET", "POST"])
@login_required
def client_delete(client_pk):
    company_id = get_current_company()
    c          = Client.query.filter_by(id=client_pk, company_id=company_id).first_or_404()
    db.session.delete(c)
    db.session.commit()
    flash("Client deleted.")
    return redirect(url_for("client_list"))


# ─────────────────────────────────────────────────────────────────────────────
# ── Stock / Inventory ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/inventory")
@login_required
def inventory_list():
    company_id  = get_current_company()
    stock_items = StockItem.query.filter_by(company_id=company_id).all()

    total_items = len(stock_items)
    in_stock    = sum(1 for i in stock_items if i.quantity > (i.reorder_level or 0))
    low_stock   = sum(1 for i in stock_items if 0 < i.quantity <= (i.reorder_level or 10))
    out_stock   = sum(1 for i in stock_items if i.quantity <= 0)

    stock_summary = {
        "total_items": total_items,
        "in_stock":    in_stock,
        "low_stock":   low_stock,
        "out_stock":   out_stock,
    }

    return render_template("inventory.html",
                           stock_items=stock_items,
                           stock_summary=stock_summary)


# ── Stock JSON API (used by inventory.html JS modals) ────────────────────────
@app.route("/stock/item/<code>")
@login_required
def stock_item_get(code):
    company_id = get_current_company()
    item = StockItem.query.filter_by(company_id=company_id, code=code.upper()).first_or_404()
    return jsonify({
        "code":          item.code,
        "name":          item.name,
        "category":      item.category or "",
        "quantity":      item.quantity,
        "unit":          item.unit or "pcs",
        "unit_price":    item.unit_price,
        "reorder_level": item.reorder_level or 10,
        "hsn":           item.hsn or "",
    })


@app.route("/stock/save", methods=["POST"])
@login_required
def stock_save():
    """Create or update a stock item via JSON (called from the modal form)."""
    company_id = get_current_company()
    data       = request.get_json(force=True)

    code = data.get("code", "").strip().upper()
    item = StockItem.query.filter_by(company_id=company_id, code=code).first() if code else None

    if item:
        # update existing
        item.name          = data.get("name", item.name)
        item.category      = data.get("category", item.category)
        item.quantity      = float(data.get("quantity", item.quantity))
        item.unit          = data.get("unit", item.unit)
        item.unit_price    = float(data.get("unit_price", item.unit_price))
        item.reorder_level = float(data.get("reorder_level", item.reorder_level))
        item.last_updated  = date.today()
    else:
        # auto-generate a code if none provided
        if not code:
            count = StockItem.query.filter_by(company_id=company_id).count()
            code  = f"PROD{count + 1:03d}"
        item = StockItem(
            company_id    = company_id,
            code          = code,
            name          = data.get("name", ""),
            category      = data.get("category", "Other"),
            quantity      = float(data.get("quantity", 0)),
            unit          = data.get("unit", "pcs"),
            unit_price    = float(data.get("unit_price", 0)),
            reorder_level = float(data.get("reorder_level", 10)),
            hsn           = data.get("hsn", ""),
            last_updated  = date.today(),
        )
        db.session.add(item)

    db.session.commit()
    return jsonify({"success": True, "code": item.code})


@app.route("/stock/adjust", methods=["POST"])
@login_required
def stock_adjust():
    """Quick quantity adjustment from the Adj button in the table."""
    company_id = get_current_company()
    data       = request.get_json(force=True)
    code       = data.get("code", "").strip().upper()
    item       = StockItem.query.filter_by(company_id=company_id, code=code).first_or_404()
    item.quantity     = float(data.get("quantity", item.quantity))
    item.last_updated = date.today()
    db.session.commit()
    return jsonify({"success": True})


@app.route("/inventory/add", methods=["GET", "POST"])
@login_required
def inventory_add():
    company_id = get_current_company()
    if request.method == "POST":
        item = StockItem(
            company_id=company_id,
            code=request.form.get("code", "").upper(),
            name=request.form.get("name", ""),
            category=request.form.get("category", ""),
            quantity=float(request.form.get("quantity", 0)),
            unit=request.form.get("unit", "pcs"),
            unit_price=float(request.form.get("unit_price", 0)),
            reorder_level=float(request.form.get("reorder_level", 0)),
            hsn=request.form.get("hsn", ""),
            last_updated=date.today(),
        )
        db.session.add(item)
        db.session.commit()
        flash("Stock item added!")
        return redirect(url_for("inventory_list"))
    return render_template("inventory_form.html")


@app.route("/inventory/edit/<int:item_pk>", methods=["GET", "POST"])
@login_required
def inventory_edit(item_pk):
    company_id = get_current_company()
    item       = StockItem.query.filter_by(id=item_pk, company_id=company_id).first_or_404()
    if request.method == "POST":
        item.name          = request.form.get("name", item.name)
        item.category      = request.form.get("category", item.category)
        item.quantity      = float(request.form.get("quantity", item.quantity))
        item.unit          = request.form.get("unit", item.unit)
        item.unit_price    = float(request.form.get("unit_price", item.unit_price))
        item.reorder_level = float(request.form.get("reorder_level", item.reorder_level))
        item.hsn           = request.form.get("hsn", item.hsn)
        item.last_updated  = date.today()
        db.session.commit()
        flash("Stock item updated!")
        return redirect(url_for("inventory_list"))
    return render_template("inventory_form.html", item=item)


@app.route("/inventory/delete/<int:item_pk>", methods=["POST"])
@login_required
def inventory_delete(item_pk):
    company_id = get_current_company()
    item       = StockItem.query.filter_by(id=item_pk, company_id=company_id).first_or_404()
    db.session.delete(item)
    db.session.commit()
    flash("Stock item deleted.")
    return redirect(url_for("inventory_list"))


# ── Purchase Invoice Routes ───────────────────────────────────────────────────
@app.route("/purchase/list")
@login_required
def purchase_invoice_list():
    company_id = get_current_company()
    invoices = PurchaseInvoice.query.filter_by(company_id=company_id).order_by(PurchaseInvoice.date.desc()).all()
    total_amount = sum(p.grand_total  for p in invoices)
    total_paid   = sum(p.paid_amount  for p in invoices)
    total_due    = sum(p.balance      for p in invoices)

    return render_template("purchases.html",
        purchases    = invoices,
        total_amount = total_amount,
        total_paid   = total_paid,
        total_due    = total_due
    )


@app.route("/purchase/new", methods=["GET", "POST"])
@login_required
def purchase_invoice_new():
    company_id = get_current_company()
    suppliers = Client.query.filter(
        Client.company_id == company_id,
        db.or_(Client.client_type == "Supplier", Client.client_type == "Both")
    ).all()
    
    if request.method == "POST":
        # Get form data
        supplier_id = request.form.get("supplier_id")
        invoice_number = request.form.get("invoice_number", "")
        invoice_date = request.form.get("invoice_date") or str(date.today())
        due_date = request.form.get("due_date")
        payment_terms = request.form.get("payment_terms", "")
        notes = request.form.get("notes", "")
        
        # Get line items
        descriptions = request.form.getlist("item_description[]")
        quantities = request.form.getlist("item_quantity[]")
        units = request.form.getlist("item_unit[]")
        rates = request.form.getlist("item_rate[]")
        gst_percents = request.form.getlist("item_gst[]")
        
        subtotal = 0
        tax_total = 0
        items_data = []
        
        for i in range(len(descriptions)):
            if descriptions[i] and descriptions[i].strip():
                qty = float(quantities[i]) if quantities[i] else 0
                rate = float(rates[i]) if rates[i] else 0
                gst = float(gst_percents[i]) if gst_percents[i] else 0
                
                line_total = qty * rate
                tax_amount = line_total * (gst / 100)
                
                subtotal += line_total
                tax_total += tax_amount
                
                items_data.append({
                    "description": descriptions[i],
                    "quantity": qty,
                    "unit": units[i] if units[i] else "pcs",
                    "rate": rate,
                    "gst": gst,
                    "total": line_total + tax_amount
                })
        
        grand_total = subtotal + tax_total
        
        # Create purchase invoice
        inv_count = PurchaseInvoice.query.count()
        invoice_id = f"PURCHASE-INV-{datetime.now().strftime('%Y%m%d')}-{inv_count+1:03d}"
        
        purchase_inv = PurchaseInvoice(
            invoice_id=invoice_id,
            company_id=company_id,
            supplier_id=int(supplier_id) if supplier_id else None,
            invoice_number=invoice_number,
            date=date.fromisoformat(invoice_date),
            due_date=date.fromisoformat(due_date) if due_date else None,
            subtotal=subtotal,
            tax_amount=tax_total,
            grand_total=grand_total,
            paid_amount=0,
            balance=grand_total,
            status="Pending",
            payment_terms=payment_terms,
            notes=notes,
            created_at=datetime.utcnow()
        )
        db.session.add(purchase_inv)
        db.session.flush()
        
        # Create line items and update stock
        for item in items_data:
            # Find or create stock item
            stock_item = StockItem.query.filter_by(
                company_id=company_id,
                name=item["description"]
            ).first()
            
            if not stock_item:
                # Create new stock item
                stock_count = StockItem.query.filter_by(company_id=company_id).count()
                stock_item = StockItem(
                    company_id=company_id,
                    code=f"AUTO-{stock_count+1:03d}",
                    name=item["description"],
                    category="Purchase",
                    quantity=0,
                    unit=item["unit"],
                    unit_price=0,  # Will be set by selling price later
                    purchase_rate=item["rate"],
                    last_purchase_rate=item["rate"],
                    gst_percent=item["gst"],
                    last_updated=date.today()
                )
                db.session.add(stock_item)
                db.session.flush()
            
            # Update stock quantity
            stock_item.quantity += item["quantity"]
            stock_item.last_purchase_rate = item["rate"]
            stock_item.gst_percent = item["gst"]
            stock_item.last_updated = date.today()
            
            # Add purchase history
            purchase_history = StockPurchaseHistory(
                stock_item_id=stock_item.id,
                purchase_invoice_id=purchase_inv.id,
                quantity=item["quantity"],
                purchase_rate=item["rate"],
                gst_percent=item["gst"],
                purchase_date=date.fromisoformat(invoice_date)
            )
            db.session.add(purchase_history)
            
            # Create invoice item
            inv_item = PurchaseInvoiceItem(
                purchase_invoice_id=purchase_inv.id,
                stock_item_id=stock_item.id,
                description=item["description"],
                quantity=item["quantity"],
                unit=item["unit"],
                purchase_rate=item["rate"],
                gst_percent=item["gst"],
                total_amount=item["total"]
            )
            db.session.add(inv_item)
            
            # Update supplier pending amount
            supplier = Client.query.get(supplier_id)
            if supplier:
                supplier.pending += (item["total"])  # Add to pending
                supplier.last_payment = date.today()
        
        db.session.commit()
        
        # Handle file upload for OCR
        if 'invoice_file' in request.files:
            file = request.files['invoice_file']
            if file and allowed_file(file.filename):
                filename = secure_filename(f"{invoice_id}_{file.filename}")
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                purchase_inv.file_path = filepath
                
                # Perform OCR extraction
                extracted_text = ""
                if filename.lower().endswith('.pdf'):
                    with open(filepath, 'rb') as f:
                        extracted_text = extract_invoice_from_pdf(f.read())
                else:
                    with open(filepath, 'rb') as f:
                        extracted_text = extract_invoice_from_image(f.read())
                
                # Parse extracted data
                parsed_data = parse_invoice_data(extracted_text)
                purchase_inv.ocr_data = json.dumps(parsed_data)
                db.session.commit()
        
        flash(f"Purchase invoice {invoice_id} created successfully!")
        return redirect(url_for("purchase_invoice_list"))
    
    return render_template("purchase_form.html", suppliers=suppliers, today=str(date.today()))


@app.route("/purchase/upload-ocr", methods=["POST"])
@login_required
def purchase_upload_ocr():
    """AJAX endpoint to upload file and get OCR extracted data"""
    company_id = get_current_company()
    
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({"success": False, "error": "Invalid file type. Please upload PDF, PNG, JPG, or JPEG"}), 400
    
    try:
        # Read file content
        file_bytes = file.read()
        
        # Extract text based on file type
        if file.filename.lower().endswith('.pdf'):
            extracted_text = extract_invoice_from_pdf(file_bytes)
        else:
            extracted_text = extract_invoice_from_image(file_bytes)
        
        if not extracted_text or len(extracted_text.strip()) < 10:
            return jsonify({
                "success": False, 
                "error": "Could not read text from file. Please ensure the invoice is clear and try again."
            }), 400
        
        # Parse extracted data
        parsed_data = parse_invoice_data(extracted_text)
        
        # Find matching supplier from extracted name
        if parsed_data.get("supplier_name"):
            supplier = Client.query.filter(
                Client.company_id == company_id,
                db.or_(
                    Client.name.ilike(f"%{parsed_data['supplier_name']}%"),
                    Client.client_type == "Supplier"
                )
            ).first()
            if supplier:
                parsed_data["supplier_id"] = supplier.id
                parsed_data["supplier_name"] = supplier.name
                print(f"✓ Matched supplier: {supplier.name}")
        
        # Add debug info
        parsed_data["extracted_text_preview"] = extracted_text[:200]
        parsed_data["item_count"] = len(parsed_data.get("items", []))
        
        return jsonify({"success": True, "data": parsed_data})
        
    except Exception as e:
        print(f"OCR processing error: {e}")
        return jsonify({"success": False, "error": f"Error processing file: {str(e)}"}), 500


@app.route("/purchase/view/<invoice_id>")
@login_required
def purchase_invoice_view(invoice_id):
    company_id = get_current_company()
    invoice = PurchaseInvoice.query.filter_by(invoice_id=invoice_id, company_id=company_id).first_or_404()
    return render_template("purchase_view.html", invoice=invoice)


@app.route("/purchase/pay/<int:pk>", methods=["POST"])
@login_required
def purchase_make_payment(pk):
    company_id = get_current_company()
    invoice = PurchaseInvoice.query.filter_by(id=pk, company_id=company_id).first_or_404()
    
    amount = float(request.form.get("amount", 0))
    if amount > invoice.balance:
        flash("Payment amount exceeds pending balance!")
        return redirect(url_for("purchase_invoice_view", invoice_id=invoice.invoice_id))
    
    invoice.paid_amount += amount
    invoice.balance -= amount
    
    if invoice.balance == 0:
        invoice.status = "Paid"
    elif invoice.paid_amount > 0:
        invoice.status = "Partial"
    
    # Update supplier's pending amount (reduce by payment)
    if invoice.supplier:
        invoice.supplier.pending -= amount
    
    db.session.commit()
    flash(f"Payment of ₹{amount:,.2f} recorded!")
    return redirect(url_for("purchase_invoice_view", invoice_id=invoice.invoice_id))

# ─────────────────────────────────────────────────────────────────────────────
# ── Invoices ──────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/invoice/list")
@login_required
def invoice_list():
    company_id    = get_current_company()
    filter_status = request.args.get("status", "All")

    # Map template tab names -> DB status values
    status_map = {
        "paid":    "Paid",
        "partial": "Partial",
        "pending": "Draft",
    }

    query = Invoice.query.filter_by(company_id=company_id)
    if filter_status != "All":
        db_status = status_map.get(filter_status)
        if db_status:
            query = query.filter_by(status=db_status)

    raw_invoices = query.order_by(Invoice.date.desc()).all()

    # Normalize into dicts that match invoice_list.html field names:
    # inv.id, inv.customer_name, inv.date, inv.bill_type,
    # inv.total, inv.paid, inv.balance, inv.status
    invoices = []
    for inv in raw_invoices:
        if inv.client_obj:
            customer_name = inv.client_obj.name
        elif inv.contact_person:
            customer_name = inv.contact_person
        else:
            customer_name = "—"

        total = inv.grand_total or 0.0

        if inv.status == "Paid":
            paid      = total
            balance   = 0.0
            tab_status = "paid"
        elif inv.status == "Partial":
            paid      = inv.subtotal or 0.0
            balance   = total - paid
            tab_status = "partial"
        else:
            paid      = 0.0
            balance   = total
            tab_status = "pending"

        invoices.append({
            "id":            inv.invoice_id,
            "customer_name": customer_name,
            "date":          inv.date,
            "bill_type":     "credit",
            "total":         total,
            "paid":          paid,
            "balance":       balance,
            "status":        tab_status,
        })

    return render_template("invoice_list.html",
                           invoices=invoices,
                           current_status=filter_status)


@app.route("/invoice/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    company_id = get_current_company()
    clients    = Client.query.filter_by(company_id=company_id).all()

    edit_id  = request.args.get("edit")
    existing = Invoice.query.filter_by(invoice_id=edit_id, company_id=company_id).first() if edit_id else None

    if request.method == "POST":
        item_codes   = request.form.getlist("item_code[]")
        descriptions = request.form.getlist("description[]")
        qtys         = request.form.getlist("qty[]")
        rates        = request.form.getlist("rate[]")
        discounts    = request.form.getlist("discount[]")

        subtotal = 0
        line_items = []
        for i in range(len(descriptions)):
            if descriptions[i] and descriptions[i].strip():
                qty  = float(qtys[i])  if qtys[i]  else 0
                rate = float(rates[i]) if rates[i] else 0
                disc = float(discounts[i]) if discounts[i] else 0
                total_line = qty * rate * (1 - disc / 100)
                subtotal  += total_line
                line_items.append((item_codes[i], descriptions[i], qty, rate, disc))

        tax         = subtotal * 0.18
        grand_total = subtotal + tax

        client_id_raw = request.form.get("client_id")
        client_id     = int(client_id_raw) if client_id_raw else None

        if existing:
            existing.client_id      = client_id
            existing.date           = date.fromisoformat(request.form.get("invoice_date") or str(date.today()))
            existing.status         = request.form.get("status", "Draft")
            existing.contact_person = request.form.get("contact_person", "")
            existing.email          = request.form.get("email", "")
            existing.phone          = request.form.get("phone", "")
            existing.subtotal       = subtotal
            existing.tax_amount     = tax
            existing.grand_total    = grand_total
            existing.terms          = request.form.get("terms", "")
            # rebuild line items
            InvoiceItem.query.filter_by(invoice_id=existing.id).delete()
            for code, desc, qty, rate, disc in line_items:
                si = StockItem.query.filter_by(company_id=company_id, code=code.upper()).first()
                db.session.add(InvoiceItem(
                    invoice_id=existing.id,
                    stock_item_id=si.id if si else None,
                    code=code, description=desc, qty=qty, rate=rate, discount=disc,
                ))
            db.session.commit()
            flash(f"Invoice {existing.invoice_id} updated!")
        else:
            inv_count  = Invoice.query.count()
            invoice_id = f"INV-{datetime.now().strftime('%Y%m%d')}-{inv_count+1:03d}"
            inv        = Invoice(
                invoice_id=invoice_id, company_id=company_id,
                client_id=client_id,
                date=date.fromisoformat(request.form.get("invoice_date") or str(date.today())),
                due_date=date.fromisoformat(request.form.get("due_date")) if request.form.get("due_date") else None,
                status=request.form.get("status", "Draft"),
                contact_person=request.form.get("contact_person", ""),
                email=request.form.get("email", ""),
                phone=request.form.get("phone", ""),
                subtotal=subtotal, tax_amount=tax, grand_total=grand_total,
                terms=request.form.get("terms", ""),
            )
            db.session.add(inv)
            db.session.flush()
            for code, desc, qty, rate, disc in line_items:
                si = StockItem.query.filter_by(company_id=company_id, code=code.upper()).first()
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    stock_item_id=si.id if si else None,
                    code=code, description=desc, qty=qty, rate=rate, discount=disc,
                ))
            db.session.commit()
            flash(f"Invoice {invoice_id} created!")

        return redirect(url_for("invoice_list"))

    return render_template("invoice.html",
                           clients=clients, invoice=existing,
                           today=str(date.today()),
                           due_date=str(date.today() + timedelta(days=30)),
                           form_data={})


@app.route("/invoice/view/<invoice_id>")
@login_required
def invoice_view(invoice_id):
    company_id = get_current_company()
    inv        = Invoice.query.filter_by(invoice_id=invoice_id, company_id=company_id).first_or_404()

    # Resolve customer name & phone
    if inv.client_obj:
        customer_name  = inv.client_obj.name
        customer_phone = inv.client_obj.phone or inv.phone or ""
    else:
        customer_name  = inv.contact_person or "—"
        customer_phone = inv.phone or ""

    total    = inv.grand_total or 0.0
    subtotal = inv.subtotal    or 0.0
    tax      = inv.tax_amount  or 0.0

    # Derive paid / balance / tab-status from DB status
    db_status = (inv.status or "").lower()
    if db_status == "paid":
        paid       = total
        balance    = 0.0
        tab_status = "paid"
    elif db_status == "partial":
        paid       = subtotal
        balance    = total - paid
        tab_status = "partial"
    else:
        paid       = 0.0
        balance    = total
        tab_status = "pending"

    # Normalize line items — template uses item.desc, item.code, item.qty,
    # item.rate, item.discount
    items = []
    for li in inv.items:
        qty      = li.qty      or 0.0
        rate     = li.rate     or 0.0
        discount = li.discount or 0.0
        items.append({
            "code":     li.code        or "",
            "desc":     li.description or "",
            "qty":      qty,
            "rate":     rate,
            "discount": discount,
            "amount":   qty * rate * (1 - discount / 100),
        })

    invoice = {
        "id":             inv.invoice_id,
        "date":           inv.date,
        "due_date":       inv.due_date,
        "status":         tab_status,
        "customer_name":  customer_name,
        "customer_phone": customer_phone,
        "subtotal":       subtotal,
        "tax":            tax,
        "total":          total,
        "paid":           paid,
        "balance":        balance,
        "bill_type":      "credit",
        "payment_mode":   "credit",
        "cheque_no":      "",
        "transaction_id": "",
        "items":          items,
        "related_orders": [],
        "terms":          inv.terms or "",
    }

    return render_template("invoice_view.html", invoice=invoice)


# ─────────────────────────────────────────────────────────────────────────────
# ── Estimates ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/estimate/list")
@login_required
def estimate_list():
    company_id    = get_current_company()
    filter_status = request.args.get("status", "All")
    query         = Estimate.query.filter_by(company_id=company_id)
    if filter_status != "All":
        query = query.filter_by(status=filter_status)
    estimates = query.order_by(Estimate.date.desc()).all()
    return render_template("estimate_list.html", estimates=estimates, current_status=filter_status)


@app.route("/estimate/new", methods=["GET", "POST"])
@login_required
def estimate_new():
    company_id = get_current_company()
    clients    = Client.query.filter_by(company_id=company_id).all()

    edit_id  = request.args.get("edit")
    existing = Estimate.query.filter_by(estimate_id=edit_id, company_id=company_id).first() if edit_id else None

    if request.method == "POST":
        item_codes   = request.form.getlist("item_code[]")
        descriptions = request.form.getlist("description[]")
        qtys         = request.form.getlist("qty[]")
        rates        = request.form.getlist("rate[]")
        discounts    = request.form.getlist("discount[]")

        subtotal   = 0
        line_items = []
        for i in range(len(descriptions)):
            if descriptions[i] and descriptions[i].strip():
                qty  = float(qtys[i])  if qtys[i]  else 0
                rate = float(rates[i]) if rates[i] else 0
                disc = float(discounts[i]) if discounts[i] else 0
                subtotal += qty * rate * (1 - disc / 100)
                line_items.append((item_codes[i], descriptions[i], qty, rate, disc))

        tax         = subtotal * 0.18
        grand_total = subtotal + tax

        client_id_raw = request.form.get("client_id")
        client_id     = int(client_id_raw) if client_id_raw else None

        if existing:
            existing.client_id      = client_id
            existing.date           = date.fromisoformat(request.form.get("estimate_date") or str(date.today()))
            existing.valid_until    = date.fromisoformat(request.form.get("valid_until")) if request.form.get("valid_until") else None
            existing.status         = request.form.get("status", "Draft")
            existing.contact_person = request.form.get("contact_person", "")
            existing.email          = request.form.get("email", "")
            existing.phone          = request.form.get("phone", "")
            existing.subtotal       = subtotal
            existing.tax_amount     = tax
            existing.grand_total    = grand_total
            existing.terms          = request.form.get("terms", "")
            EstimateItem.query.filter_by(estimate_id=existing.id).delete()
            for code, desc, qty, rate, disc in line_items:
                si = StockItem.query.filter_by(company_id=company_id, code=code.upper()).first()
                db.session.add(EstimateItem(
                    estimate_id=existing.id,
                    stock_item_id=si.id if si else None,
                    code=code, description=desc, qty=qty, rate=rate, discount=disc,
                ))
            db.session.commit()
            flash(f"Estimate {existing.estimate_id} updated!")
        else:
            est_count   = Estimate.query.count()
            estimate_id = f"EST-{datetime.now().strftime('%Y%m%d')}-{est_count+1:03d}"
            est         = Estimate(
                estimate_id=estimate_id, company_id=company_id,
                client_id=client_id,
                date=date.fromisoformat(request.form.get("estimate_date") or str(date.today())),
                valid_until=date.fromisoformat(request.form.get("valid_until")) if request.form.get("valid_until") else None,
                status=request.form.get("status", "Draft"),
                contact_person=request.form.get("contact_person", ""),
                email=request.form.get("email", ""),
                phone=request.form.get("phone", ""),
                subtotal=subtotal, tax_amount=tax, grand_total=grand_total,
                terms=request.form.get("terms", ""),
            )
            db.session.add(est)
            db.session.flush()
            for code, desc, qty, rate, disc in line_items:
                si = StockItem.query.filter_by(company_id=company_id, code=code.upper()).first()
                db.session.add(EstimateItem(
                    estimate_id=est.id,
                    stock_item_id=si.id if si else None,
                    code=code, description=desc, qty=qty, rate=rate, discount=disc,
                ))
            db.session.commit()
            flash(f"Estimate {estimate_id} created!")

        return redirect(url_for("estimate_list"))

    valid_until = str(date.today() + timedelta(days=30))
    return render_template("estimate.html",
                       clients=clients, estimate=existing,
                       today=str(date.today()), valid_until=valid_until,
                       form_data={})

@app.route("/estimate/edit/<estimate_id>")
@login_required
def estimate_edit(estimate_id):
    return redirect(url_for("estimate_new", edit=estimate_id))


@app.route("/estimate/view/<estimate_id>")
@login_required
def estimate_view(estimate_id):
    company_id = get_current_company()
    estimate   = Estimate.query.filter_by(estimate_id=estimate_id, company_id=company_id).first_or_404()
    return render_template("estimate_view_new.html", estimate=estimate)


# ─────────────────────────────────────────────────────────────────────────────
# ── Super Admin ───────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/admin/dashboard")
@login_required
@super_admin_required
def admin_dashboard():
    stats = {
        "total_companies":  Company.query.count(),
        "total_users":      CompanyUser.query.count(),
        "active_companies": Company.query.filter_by(is_active=True).count(),
        "monthly_revenue":  0,
    }
    plan_distribution = {}
    for c in Company.query.all():
        plan_distribution[c.subscription_plan] = plan_distribution.get(c.subscription_plan, 0) + 1

    return render_template("super_admin.html",
                           stats=stats,
                           companies=Company.query.all(),
                           plans=get_all_plans(),
                           plan_distribution=plan_distribution)


@app.route("/admin/companies")
@login_required
@super_admin_required
def admin_companies():
    return render_template("admin_companies.html", companies=Company.query.all())


@app.route("/admin/company/<company_id>")
@login_required
@super_admin_required
def admin_company_detail(company_id):
    company = get_company_by_id(company_id)
    users   = CompanyUser.query.filter_by(company_id=company_id).all()
    return render_template("admin_company_detail.html",
                           company=company, users=users, plans=get_all_plans())


@app.route("/admin/company/<company_id>/update-plan", methods=["POST"])
@login_required
@super_admin_required
def admin_update_company_plan(company_id):
    plan_id = request.form.get("plan")
    company = get_company_by_id(company_id)
    plan    = SubscriptionPlan.query.get(plan_id)
    if company and plan:
        company.subscription_plan     = plan.id
        company.max_companies_allowed = plan.max_companies
        company.max_users_per_company = plan.max_users
        db.session.commit()
        flash(f"Company plan updated to {plan.name}")
    return redirect(url_for("admin_company_detail", company_id=company_id))


@app.route("/admin/company/<company_id>/toggle-status", methods=["POST"])
@login_required
@super_admin_required
def admin_toggle_company_status(company_id):
    company = get_company_by_id(company_id)
    if company:
        company.is_active = not company.is_active
        db.session.commit()
        status = "activated" if company.is_active else "suspended"
        flash(f"Company {status}")
    return redirect(url_for("admin_company_detail", company_id=company_id))


# ─────────────────────────────────────────────────────────────────────────────
# ── Employee Management ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/employees")
@login_required
@owner_required
def employee_list():
    company_id = get_current_company()
    employees  = CompanyUser.query.filter_by(company_id=company_id).all()
    return render_template("employees.html", employees=employees)


@app.route("/employees/add", methods=["GET", "POST"])
@login_required
@owner_required
def employee_add():
    company_id = get_current_company()
    can_add, msg = check_company_limit(company_id, "user")
    if not can_add:
        flash(msg)
        return redirect(url_for("employee_list"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        emp_count = CompanyUser.query.count()
        emp_id    = f"EMP{emp_count + 1:03d}"
        new_emp   = CompanyUser(
            user_id=emp_id, company_id=company_id, email=email,
            password_hash=hash_password(password),
            full_name=request.form.get("full_name", ""),
            role=request.form.get("role", "employee"),
            department=request.form.get("department", ""),
            phone=request.form.get("phone", ""),
            is_active=True, created_at=date.today(),
        )
        db.session.add(new_emp)
        db.session.commit()
        flash("Employee added!")
        return redirect(url_for("employee_list"))
    return render_template("employee_form.html")


@app.route("/employees/toggle/<user_id>", methods=["POST"])
@login_required
@owner_required
def employee_toggle(user_id):
    company_id = get_current_company()
    emp        = CompanyUser.query.filter_by(user_id=user_id, company_id=company_id).first_or_404()
    emp.is_active = not emp.is_active
    db.session.commit()
    flash(f"Employee {'activated' if emp.is_active else 'deactivated'}.")
    return redirect(url_for("employee_list"))


# ─────────────────────────────────────────────────────────────────────────────
# ── Product Lookup API ────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/product/<code>")
@login_required
def api_product_lookup(code):
    company_id = get_current_company()
    code_clean = code.strip().upper()
    item = StockItem.query.filter_by(company_id=company_id, code=code_clean).first()
    if not item:
        item = StockItem.query.filter(
            StockItem.company_id == company_id,
            StockItem.name.ilike(f"%{code_clean}%")
        ).first()
    if not item:
        return jsonify({"found": False, "message": f"No product found for '{code}'"}), 404
    return jsonify({
        "found": True, "code": item.code, "name": item.name,
        "rate": item.unit_price, "unit": item.unit or "pcs",
        "category": item.category or "", "stock": item.quantity,
        "hsn": item.hsn or "",
        "low_stock": item.quantity <= item.reorder_level,
    }), 200


@app.route("/api/products/search")
@login_required
def api_products_search():
    company_id = get_current_company()
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify({"results": []})
    items = StockItem.query.filter(
        StockItem.company_id == company_id,
        db.or_(StockItem.code.ilike(f"%{q}%"), StockItem.name.ilike(f"%{q}%"))
    ).limit(8).all()
    return jsonify({"results": [{
        "code": s.code, "name": s.name, "rate": s.unit_price,
        "unit": s.unit or "pcs", "stock": s.quantity, "hsn": s.hsn or "",
    } for s in items]})


# ─────────────────────────────────────────────────────────────────────────────
# ── Profile ───────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/profile")
@login_required
def profile():
    user = get_current_user()
    return render_template("profile.html", user=user)



# ─────────────────────────────────────────────────────────────────────────────
# ── Company Settings ──────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/company/settings")
@login_required
@owner_required
def company_settings():
    company_id = get_current_company()
    company    = get_company_by_id(company_id)
    users      = CompanyUser.query.filter_by(company_id=company_id).all()
    plans = {
        p.id: {
            "name":          p.name,
            "price":         p.price,
            "max_companies": p.max_companies,
            "max_users":     p.max_users,
            "features":      p.features.split(",") if p.features else [],
        }
        for p in SubscriptionPlan.query.all()
    }
    current_plan = plans.get(company.subscription_plan) if company else None
    return render_template("company_settings.html",
                           company=company,
                           users=users,
                           plans=plans,
                           current_plan=current_plan)


@app.route("/company/update-info", methods=["POST"])
@login_required
@owner_required
def update_company_info():
    company_id = get_current_company()
    company    = get_company_by_id(company_id)
    if company:
        company.company_name = request.form.get("company_name", company.company_name).strip()
        company.address      = request.form.get("address",      company.address)
        company.phone        = request.form.get("phone",        company.phone)
        company.gst_number   = request.form.get("gst_number",   company.gst_number)
        db.session.commit()
        # Keep session in sync
        if "user" in session:
            session["user"]["company_name"] = company.company_name
            session.modified = True
        flash("Company information updated successfully.")
    else:
        flash("Company not found.")
    return redirect(url_for("company_settings"))


@app.route("/company/add-user", methods=["POST"])
@login_required
@owner_required
def add_company_user():
    company_id = get_current_company()

    can_add, message = check_company_limit(company_id, "user")
    if not can_add:
        flash(message)
        return redirect(url_for("company_settings"))

    email     = request.form.get("email",     "").strip().lower()
    password  = request.form.get("password",  "")
    full_name = request.form.get("full_name", "").strip()
    role      = request.form.get("role",      "employee")
    department= request.form.get("department","")
    phone     = request.form.get("phone",     "")

    if CompanyUser.query.filter_by(company_id=company_id, email=email).first():
        flash("A user with this email already exists in your company.")
        return redirect(url_for("company_settings"))

    emp_count = CompanyUser.query.count()
    emp_id    = f"EMP{emp_count + 1:03d}"
    new_user  = CompanyUser(
        user_id=emp_id, company_id=company_id,
        email=email, password_hash=hash_password(password),
        full_name=full_name, role=role,
        department=department, phone=phone,
        is_active=True, created_at=date.today()
    )
    db.session.add(new_user)
    db.session.commit()
    flash(f"User '{full_name}' added successfully.")
    return redirect(url_for("company_settings"))


@app.route("/company/remove-user/<user_id>")
@login_required
@owner_required
def remove_company_user(user_id):
    company_id = get_current_company()
    user = CompanyUser.query.filter_by(user_id=user_id, company_id=company_id).first()
    if user and user.role != "owner":
        user.is_active = False
        db.session.commit()
        flash("User removed successfully.")
    else:
        flash("Cannot remove this user.")
    return redirect(url_for("company_settings"))


@app.route("/company/upgrade-plan", methods=["POST"])
@login_required
@owner_required
def upgrade_plan():
    company_id = get_current_company()
    company    = get_company_by_id(company_id)
    new_plan   = request.form.get("plan")
    plan       = SubscriptionPlan.query.get(new_plan)
    if company and plan:
        company.subscription_plan     = new_plan
        company.max_users_per_company = plan.max_users
        company.max_companies_allowed = plan.max_companies
        db.session.commit()
        flash(f"Plan upgraded to {plan.name} successfully!")
    else:
        flash("Invalid plan selected.")
    return redirect(url_for("company_settings"))

# ─────────────────────────────────────────────────────────────────────────────
# ── DEBTORS & CREDITORS ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#
#  Debtors  = Clients who OWE you money  (Sales Invoices with balance > 0)
#  Creditors= Suppliers you OWE money to (Purchase Invoices with balance > 0)
#
# ─────────────────────────────────────────────────────────────────────────────

def _debtor_summary(company_id):
    """
    For every client that has at least one outstanding sales invoice,
    return a summary dict with the key financial fields.
    """
    clients = Client.query.filter_by(company_id=company_id).all()
    today   = date.today()
    rows    = []

    for c in clients:
        invoices = (Invoice.query
                    .filter_by(company_id=company_id, client_id=c.id)
                    .order_by(Invoice.date.desc())
                    .all())
        if not invoices:
            continue

        total_pending = sum(getattr(i, "balance", 0) or 0 for i in invoices)
        if total_pending <= 0:
            continue  # fully settled – skip

        last_invoice_date = invoices[0].date  # already desc sorted

        # nearest due invoice (unpaid, due_date set)
        unpaid       = [i for i in invoices if (getattr(i, "balance", 0) or 0) > 0]
        due_invoices = [i for i in unpaid if getattr(i, "due_date", None)]
        if due_invoices:
            future  = [i for i in due_invoices if i.due_date >= today]
            nearest = min(future, key=lambda i: i.due_date) if future else \
                      max(due_invoices, key=lambda i: i.due_date)
            nearest_due_date = nearest.due_date
            nearest_due_amt  = getattr(nearest, "balance", 0) or 0
        else:
            nearest_due_date = None
            nearest_due_amt  = None

        # last payment: invoice with highest amount paid
        paid_invoices = [i for i in invoices
                         if (i.grand_total - (getattr(i, "balance", 0) or 0)) > 0]
        if paid_invoices:
            last_paid_inv     = max(paid_invoices, key=lambda i: i.date)
            last_payment_date = last_paid_inv.date
            last_payment_amt  = last_paid_inv.grand_total - (getattr(last_paid_inv, "balance", 0) or 0)
        else:
            last_payment_date = None
            last_payment_amt  = None

        rows.append({
            "id":                c.id,
            "name":              c.name,
            "phone":             c.phone or "",
            "city":              c.city or "",
            "total_pending":     total_pending,
            "last_invoice_date": last_invoice_date,
            "nearest_due_date":  nearest_due_date,
            "nearest_due_amt":   nearest_due_amt,
            "last_payment_date": last_payment_date,
            "last_payment_amt":  last_payment_amt,
            "invoice_count":     len(invoices),
            "overdue":           nearest_due_date is not None and nearest_due_date < today,
        })

    rows.sort(key=lambda r: r["total_pending"], reverse=True)
    return rows


def _creditor_summary(company_id):
    """
    For every supplier that has at least one outstanding purchase invoice.
    """
    suppliers = Client.query.filter(
        Client.company_id == company_id,
        db.or_(Client.client_type == "Supplier", Client.client_type == "Both")
    ).all()

    today = date.today()
    rows  = []

    for s in suppliers:
        invoices = (PurchaseInvoice.query
                    .filter_by(company_id=company_id, supplier_id=s.id)
                    .order_by(PurchaseInvoice.date.desc())
                    .all())
        if not invoices:
            continue

        total_pending = sum(i.balance or 0 for i in invoices)
        if total_pending <= 0:
            continue

        last_bill_date = invoices[0].date

        unpaid       = [i for i in invoices if (i.balance or 0) > 0]
        due_invoices = [i for i in unpaid if i.due_date]
        if due_invoices:
            future  = [i for i in due_invoices if i.due_date >= today]
            nearest = min(future, key=lambda i: i.due_date) if future else \
                      max(due_invoices, key=lambda i: i.due_date)
            nearest_due_date = nearest.due_date
            nearest_due_amt  = nearest.balance or 0
        else:
            nearest_due_date = None
            nearest_due_amt  = None

        paid_invs = [i for i in invoices if (i.paid_amount or 0) > 0]
        if paid_invs:
            last_paid_inv     = max(paid_invs, key=lambda i: i.date)
            last_payment_date = last_paid_inv.date
            last_payment_amt  = last_paid_inv.paid_amount or 0
        else:
            last_payment_date = None
            last_payment_amt  = None

        rows.append({
            "id":                s.id,
            "name":              s.name,
            "phone":             s.phone or "",
            "city":              s.city or "",
            "total_pending":     total_pending,
            "last_bill_date":    last_bill_date,
            "nearest_due_date":  nearest_due_date,
            "nearest_due_amt":   nearest_due_amt,
            "last_payment_date": last_payment_date,
            "last_payment_amt":  last_payment_amt,
            "invoice_count":     len(invoices),
            "overdue":           nearest_due_date is not None and nearest_due_date < today,
        })

    rows.sort(key=lambda r: r["total_pending"], reverse=True)
    return rows


@app.route("/debtors")
@login_required
def debtors_list():
    company_id        = get_current_company()
    debtors           = _debtor_summary(company_id)
    total_outstanding = sum(d["total_pending"] for d in debtors)
    overdue_count     = sum(1 for d in debtors if d["overdue"])
    return render_template("debtors.html",
                           debtors=debtors,
                           total_outstanding=total_outstanding,
                           overdue_count=overdue_count)


@app.route("/creditors")
@login_required
def creditors_list():
    company_id    = get_current_company()
    creditors     = _creditor_summary(company_id)
    total_payable = sum(c["total_pending"] for c in creditors)
    overdue_count = sum(1 for c in creditors if c["overdue"])
    return render_template("creditors.html",
                           creditors=creditors,
                           total_payable=total_payable,
                           overdue_count=overdue_count)


@app.route("/debtors/<int:client_pk>/statement")
@login_required
def debtor_statement(client_pk):
    company_id = get_current_company()
    c          = Client.query.filter_by(id=client_pk, company_id=company_id).first_or_404()

    invoices = (Invoice.query
                .filter_by(company_id=company_id, client_id=c.id)
                .order_by(Invoice.date.asc())
                .all())

    ledger          = []
    running_balance = c.opening_balance or 0.0

    if running_balance:
        ledger.append({
            "date":    c.created_at or date.today(),
            "type":    "Opening Balance",
            "ref":     "—",
            "debit":   running_balance,
            "credit":  0,
            "balance": running_balance,
            "status":  "",
            "id":      None,
        })

    for inv in invoices:
        running_balance += inv.grand_total
        ledger.append({
            "date":    inv.date,
            "type":    "Invoice",
            "ref":     inv.invoice_id,
            "debit":   inv.grand_total,
            "credit":  0,
            "balance": running_balance,
            "status":  inv.status,
            "id":      inv.invoice_id,
        })
        paid = inv.grand_total - (getattr(inv, "balance", 0) or 0)
        if paid > 0:
            running_balance -= paid
            ledger.append({
                "date":    inv.date,
                "type":    "Payment Received",
                "ref":     inv.invoice_id,
                "debit":   0,
                "credit":  paid,
                "balance": running_balance,
                "status":  "",
                "id":      inv.invoice_id,
            })

    total_debit  = sum(r["debit"]  for r in ledger)
    total_credit = sum(r["credit"] for r in ledger)

    return render_template("ledger_statement.html",
                           entity=_normalize_client(c),
                           ledger=ledger,
                           total_debit=total_debit,
                           total_credit=total_credit,
                           closing_balance=running_balance,
                           mode="debtor",
                           back_url="/debtors",
                           today=date.today().strftime("%d %b %Y"))


@app.route("/creditors/<int:supplier_pk>/statement")
@login_required
def creditor_statement(supplier_pk):
    company_id = get_current_company()
    s          = Client.query.filter_by(id=supplier_pk, company_id=company_id).first_or_404()

    invoices = (PurchaseInvoice.query
                .filter_by(company_id=company_id, supplier_id=s.id)
                .order_by(PurchaseInvoice.date.asc())
                .all())

    ledger          = []
    running_balance = s.opening_balance or 0.0

    if running_balance:
        ledger.append({
            "date":    s.created_at or date.today(),
            "type":    "Opening Balance",
            "ref":     "—",
            "debit":   0,
            "credit":  running_balance,
            "balance": running_balance,
            "status":  "",
            "id":      None,
            "inv_id":  None,
        })

    for inv in invoices:
        running_balance += inv.grand_total
        ledger.append({
            "date":    inv.date,
            "type":    "Purchase Invoice",
            "ref":     inv.invoice_number or inv.invoice_id,
            "debit":   0,
            "credit":  inv.grand_total,
            "balance": running_balance,
            "status":  inv.status,
            "id":      inv.id,
            "inv_id":  inv.invoice_id,
        })
        if inv.paid_amount and inv.paid_amount > 0:
            running_balance -= inv.paid_amount
            ledger.append({
                "date":    inv.date,
                "type":    "Payment Made",
                "ref":     inv.invoice_number or inv.invoice_id,
                "debit":   inv.paid_amount,
                "credit":  0,
                "balance": running_balance,
                "status":  "",
                "id":      inv.id,
                "inv_id":  inv.invoice_id,
            })

    total_debit  = sum(r["debit"]  for r in ledger)
    total_credit = sum(r["credit"] for r in ledger)

    return render_template("ledger_statement.html",
                           entity=_normalize_client(s),
                           ledger=ledger,
                           total_debit=total_debit,
                           total_credit=total_credit,
                           closing_balance=running_balance,
                           mode="creditor",
                           back_url="/creditors",
                           today=date.today().strftime("%d %b %Y"))


# ─────────────────────────────────────────────────────────────────────────────
# ── Receipts & Payments ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _outstanding_invoices_for_client(company_id, client_id):
    """Return list of dicts for invoices with a remaining balance for a client."""
    invs = (Invoice.query
            .filter_by(company_id=company_id, client_id=client_id)
            .filter(Invoice.status.in_(["Draft", "Partial"]))
            .order_by(Invoice.date.asc())
            .all())
    result = []
    for inv in invs:
        total   = inv.grand_total or 0
        balance = getattr(inv, "balance", None)
        if balance is None:
            balance = total if inv.status != "Paid" else 0
        if balance > 0:
            result.append({
                "id":      inv.id,
                "ref":     inv.invoice_id,
                "date":    inv.date.strftime("%d %b %Y") if inv.date else "",
                "total":   total,
                "balance": balance,
            })
    return result


def _outstanding_invoices_for_supplier(company_id, supplier_id):
    """Return list of dicts for purchase invoices with a remaining balance."""
    invs = (PurchaseInvoice.query
            .filter_by(company_id=company_id, supplier_id=supplier_id)
            .filter(PurchaseInvoice.status.in_(["Pending", "Partial"]))
            .order_by(PurchaseInvoice.date.asc())
            .all())
    result = []
    for inv in invs:
        total   = inv.grand_total or 0
        balance = inv.balance or total
        if balance > 0:
            result.append({
                "id":      inv.id,
                "ref":     inv.invoice_number or inv.invoice_id,
                "date":    inv.date.strftime("%d %b %Y") if inv.date else "",
                "total":   total,
                "balance": balance,
            })
    return result


def _build_invoices_json(company_id, entities, fetch_fn):
    """Build {entity_id: [invoice list]} dict for JS."""
    data = {}
    for e in entities:
        data[str(e.id)] = fetch_fn(company_id, e.id)
    return json.dumps(data)


@app.route("/receipts/new")
@login_required
def receipt_new():
    company_id    = get_current_company()
    all_clients   = Client.query.filter_by(company_id=company_id).order_by(Client.name).all()
    selected_id   = request.args.get("client_id", type=int)
    invoices_json = _build_invoices_json(company_id, all_clients,
                                         _outstanding_invoices_for_client)
    return render_template(
        "receipt_payment.html",
        mode="receipt",
        entities=all_clients,
        invoices_json=invoices_json,
        selected_id=selected_id,
        today=str(date.today()),
    )


@app.route("/receipts/save", methods=["POST"])
@login_required
def receipt_save():
    company_id  = get_current_company()
    entity_id   = request.form.get("entity_id", type=int)
    amount      = request.form.get("amount", type=float, default=0)
    invoice_ids = [int(x) for x in request.form.get("invoice_ids", "").split(",") if x.strip()]
    narration   = request.form.get("narration", "")
    pay_mode    = request.form.get("pay_mode", "Cash")
    txn_date_str = request.form.get("txn_date")
    txn_date    = date.fromisoformat(txn_date_str) if txn_date_str else date.today()

    if not entity_id or amount <= 0:
        flash("Please select a client and enter a valid amount.")
        return redirect(url_for("receipt_new"))

    if not invoice_ids:
        rows = _outstanding_invoices_for_client(company_id, entity_id)
        invoice_ids = [r["id"] for r in rows]

    remaining = amount
    settled   = 0

    for inv_id in invoice_ids:
        if remaining <= 0:
            break
        inv = Invoice.query.filter_by(id=inv_id, company_id=company_id).first()
        if not inv:
            continue

        inv_balance = getattr(inv, "balance", None)
        if inv_balance is None:
            inv_balance = inv.grand_total or 0

        apply        = min(remaining, inv_balance)
        remaining   -= apply
        inv_balance -= apply
        settled     += apply

        if hasattr(inv, "balance"):
            inv.balance = inv_balance
        if hasattr(inv, "paid_amount"):
            inv.paid_amount = (inv.paid_amount or 0) + apply

        if inv_balance <= 0:
            inv.status = "Paid"
        elif apply > 0:
            inv.status = "Partial"

    client = Client.query.filter_by(id=entity_id, company_id=company_id).first()
    if client and hasattr(client, "pending") and client.pending:
        client.pending = max(0, (client.pending or 0) - settled)

    db.session.commit()
    flash(f"Receipt of ₹{settled:,.2f} recorded via {pay_mode}. {narration}")
    return redirect(url_for("debtors_list"))


@app.route("/payments/new")
@login_required
def payment_new():
    company_id    = get_current_company()
    all_suppliers = Client.query.filter_by(company_id=company_id).order_by(Client.name).all()
    selected_id   = request.args.get("supplier_id", type=int)
    invoices_json = _build_invoices_json(company_id, all_suppliers,
                                         _outstanding_invoices_for_supplier)
    return render_template(
        "receipt_payment.html",
        mode="payment",
        entities=all_suppliers,
        invoices_json=invoices_json,
        selected_id=selected_id,
        today=str(date.today()),
    )


@app.route("/payments/save", methods=["POST"])
@login_required
def payment_save():
    company_id  = get_current_company()
    entity_id   = request.form.get("entity_id", type=int)
    amount      = request.form.get("amount", type=float, default=0)
    invoice_ids = [int(x) for x in request.form.get("invoice_ids", "").split(",") if x.strip()]
    narration   = request.form.get("narration", "")
    pay_mode    = request.form.get("pay_mode", "Cash")
    txn_date_str = request.form.get("txn_date")
    txn_date    = date.fromisoformat(txn_date_str) if txn_date_str else date.today()

    if not entity_id or amount <= 0:
        flash("Please select a supplier and enter a valid amount.")
        return redirect(url_for("payment_new"))

    if not invoice_ids:
        rows = _outstanding_invoices_for_supplier(company_id, entity_id)
        invoice_ids = [r["id"] for r in rows]

    remaining = amount
    settled   = 0

    for inv_id in invoice_ids:
        if remaining <= 0:
            break
        inv = PurchaseInvoice.query.filter_by(id=inv_id, company_id=company_id).first()
        if not inv:
            continue

        inv_balance  = inv.balance or (inv.grand_total or 0)
        apply        = min(remaining, inv_balance)
        remaining   -= apply
        settled     += apply

        inv.balance     = inv_balance - apply
        inv.paid_amount = (inv.paid_amount or 0) + apply

        if inv.balance <= 0:
            inv.status = "Paid"
        elif apply > 0:
            inv.status = "Partial"

        if inv.supplier and hasattr(inv.supplier, "pending"):
            inv.supplier.pending = max(0, (inv.supplier.pending or 0) - apply)

    db.session.commit()
    flash(f"Payment of ₹{settled:,.2f} recorded via {pay_mode}. {narration}")
    return redirect(url_for("creditors_list"))


# ─────────────────────────────────────────────────────────────────────────────
# ── App entry point ───────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_database()
    app.run(debug=True, port=5003)
else:
    # When run by Gunicorn / Render, seed after the app is fully loaded
    with app.app_context():
        seed_database()
