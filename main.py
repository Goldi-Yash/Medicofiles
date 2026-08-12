from flask import Flask, render_template , request, redirect, url_for , jsonify , session , send_file , flash , abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user , AnonymousUserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime , date, timedelta , timezone
import pdfplumber
import re
import io
import os
import pandas as pd
from sqlalchemy import func , inspect , text
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from authlib.integrations.flask_client import OAuth
import secrets
import random
from functools import wraps
import json
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
import wikipediaapi
import urllib.parse
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
import traceback
import base64

load_dotenv()


# Initialize Wikipedia API with a custom user-agent
wiki = wikipediaapi.Wikipedia(
    user_agent='MedicofilesApp/1.0 (contact@medicofiles.com)',
    language='en'
)

# Initialize Gemini Client
client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('🔒 Admin access required for this section!', 'access_denied_popup')
            return redirect(request.referrer or url_for('billing'))
        return f(*args, **kwargs)
    return decorated_function

# IST Timezone (+5:30) Helper
def get_ist_time():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).replace(tzinfo=None)

app = Flask(__name__)
# csrf = CSRFProtect(app)
app.secret_key = os.getenv('app_secret_key')  # Replace with a secure key in production

# Google OAuth Setup (Pure OAuth2 - No ID Token Validation Bug)
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id= os.getenv('client_id'), #CLIENT ID
    client_secret= os.getenv('client_secret'), #CLIENT SECRET
    access_token_url='https://oauth2.googleapis.com/token',
    access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/2/',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'email profile'
    }
)

# Flask-Mail Configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')  # Google App Password (16-digit code)

mail = Mail(app)
serializer = URLSafeTimedSerializer(app.secret_key)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'landing'

@login_manager.user_loader
def load_user(user_id):
    if user_id is not None and str(user_id).isdigit():
        return User.query.get(int(user_id))
    return None

class AnonymousUser(AnonymousUserMixin):
    role = 'guest'
    def has_permission(self, category, key):
        return False

# login_manager ko custom anonymous user assign kar do
login_manager.anonymous_user = AnonymousUser

# SQLite Database Setup
# basedir = os.path.abspath(os.path.dirname(__file__))
# app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'instance', 'medico.db')
# app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# db = SQLAlchemy(app)

# Database Setup (Supabase PostgreSQL with Local SQLite Fallback)
db_url = os.getenv('DATABASE_URL', 'sqlite:///' + os.path.join(os.path.abspath(os.path.dirname(__file__)), 'instance', 'medico.db'))

# Fix Supabase/Heroku pooler prefix (postgres:// -> postgresql://)
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=get_ist_time)
    role = db.Column(db.String(20), default='admin')
    plain_password = db.Column(db.String(100))
    permissions = db.Column(db.Text, nullable=True) # Granular JSON permissions
    is_deactivated = db.Column(db.Boolean, default=False)
    deactivated_at = db.Column(db.DateTime, nullable=True)

    def get_permissions(self):
        if self.role == 'admin':
            return {"all": True}
        if not self.permissions:
            return {
                "modules": {"dashboard": True, "symptom_tags": True, "assistant": True, "add_medicine": True, "transactions": True, "billing": True, "inventory": True, "alerts": True, "ledger": True, "settings": False, "distributors": True},
                "actions": {"inventory_edit": False, "inventory_delete": False, "delete_bill": False , "map_medicine": False , "delete_tag": False , "delete_mapping": False, "ledger_delete": False, "ledger_edit": False, "delete_distributor": False, "edit_distributor": False},
                "settings": {"store": False, "billing": False, "stock": False, "tax": False, "security": False, "backup": False, "account": False, "staff": False}
            }
        try:
            data = json.loads(self.permissions)
            if isinstance(data, str): # Safety check for double encoding
                data = json.loads(data)
            return data
        except Exception as e:
            return {}

    def has_permission(self, category, key):
        if self.role == 'admin':
            return True
        perms = self.get_permissions()
        return perms.get(category, {}).get(key, False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.plain_password = password  # Store plain password for reference

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

class Distributor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    contact_person = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    supplies_category = db.Column(db.String(200), nullable=True) # e.g., Generic, Pharma, Surgical, Injections
    notes = db.Column(db.String(255), nullable=True)

# Medicine / Inventory Model
class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    company = db.Column(db.String(100), nullable=True)
    category = db.Column(db.String(50), nullable=False)
    composition = db.Column(db.String(150))
    batch_no = db.Column(db.String(50), nullable=False)
    expiry_date = db.Column(db.String(20), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    pack_size = db.Column(db.Integer, default=10) # e.g. 10 tablets per strip
    mrp = db.Column(db.Float, nullable=False)
    purchase_price = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=get_ist_time)
    rx_required = db.Column(db.Boolean, default=False)
    medicine_type = db.Column(db.String(10), default='None', nullable=True)

    def __repr__(self):
        return f'<Medicine {self.name}>'

# Sale / Bill Summary Model
class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=True)
    customer_phone = db.Column(db.String(15), nullable=True)
    total_amount = db.Column(db.Float, nullable=False)
    payment_mode = db.Column(db.String(20), default='Cash')  # Cash, UPI, Card
    doctor_name = db.Column(db.String(100), nullable=True)
    discount_percent = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=get_ist_time)
    
    # Relationship with individual items sold in this bill
    items = db.relationship('SaleItem', backref='sale', lazy=True)

# Sale Item Details Model
class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    medicine_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    discount_percent = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, nullable=False)

class StoreSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # 1. Store Profile
    shop_name = db.Column(db.String(150), default="Medicofiles Pharmacy")
    address = db.Column(db.String(255), default="Near Main Market, Gurgaon")
    phone = db.Column(db.String(20), default="+91 9876543210")
    gstin = db.Column(db.String(50), default="06AAAAA0000A1Z5")
    dl_number = db.Column(db.String(50), default="HR-GUG-123456")
    footer_note = db.Column(db.String(255), default="Goods once sold will not be taken back without original bill.")

    # --- NEW ADDED STORE & FILE FIELDS ---
    logo_path = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    pincode = db.Column(db.String(20), nullable=True)
    operating_hours = db.Column(db.String(100), nullable=True)
    off_days = db.Column(db.String(100), nullable=True)

    # --- NEW OWNER DETAILS ---
    owner_name = db.Column(db.String(150), nullable=True)
    owner_contact = db.Column(db.String(20), nullable=True)
    owner_email = db.Column(db.String(120), nullable=True)
    owner_pan = db.Column(db.String(20), nullable=True)
    owner_aadhaar = db.Column(db.String(20), nullable=True)
    owner_doc_path = db.Column(db.String(255), nullable=True)

    # --- NEW LEGAL & PHARMA DETAILS ---
    fssai_no = db.Column(db.String(50), nullable=True)
    pharmacist_reg_no = db.Column(db.String(50), nullable=True)
    legal_doc_path = db.Column(db.String(255), nullable=True)
    
    # 2. Billing & Invoice
    receipt_format = db.Column(db.String(50), default="thermal_80mm") # thermal_80mm / a4
    invoice_prefix = db.Column(db.String(20), default="INV/2026/")
    default_discount = db.Column(db.Float, default=0.0)
    enable_negative_stock = db.Column(db.Boolean, default=False)
    require_doctor_name = db.Column(db.Boolean, default=False)
    show_salt_on_print = db.Column(db.Boolean, default=True)
    
    # 3. Inventory & Rules
    expiry_alert_days = db.Column(db.Integer, default=60)
    profit_margin = db.Column(db.Float, default=20.0)

    # --- THRESHOLDS ---
    thresh_tablet = db.Column(db.Integer, default=5)
    thresh_syrup = db.Column(db.Integer, default=2)
    thresh_injection = db.Column(db.Integer, default=2)
    thresh_ointment = db.Column(db.Integer, default=3)
    thresh_capsule = db.Column(db.Integer, default=5)
    thresh_other = db.Column(db.Integer, default=2)
    
    # 4. Tax & Security
    round_off_bills = db.Column(db.Boolean, default=True)
    admin_pin = db.Column(db.String(20), default="1234")

# Disease / Symptom Tag Master
class DiseaseTag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False) # e.g. "Fever", "Acidity"
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=get_ist_time)
    target_age = db.Column(db.String(50), nullable=True, default='all')
    
    # Relationship with Mapping Table
    mappings = db.relationship('TagMedicineMap', backref='tag', cascade="all, delete-orphan")

# Tag to Medicine Mapping Table
class TagMedicineMap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag_id = db.Column(db.Integer, db.ForeignKey('disease_tag.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    dosage_note = db.Column(db.String(100)) # e.g. "1 Tablet BD (Subah-Shaam)"
    target_age = db.Column(db.String(50), nullable=True, default='all')
    
    medicine = db.relationship('Medicine')

class Customer(db.Model):
    __tablename__ = 'customer'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(15), unique=True, nullable=False)
    address = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=get_ist_time)
    
    # Relationships
    ledger_entries = db.relationship('CustomerLedger', backref='customer', lazy=True, cascade="all, delete-orphan")

class CustomerLedger(db.Model):
    __tablename__ = 'customer_ledger'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    
    # 'credit' = udhar medicine di, 'debit' = customer ne paise jama kiye
    txn_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    balance_after = db.Column(db.Float, default=0.0)
    note = db.Column(db.String(255), nullable=True) # Bill/Invoice ID ya Receipt Info
    date = db.Column(db.DateTime, default=get_ist_time)

def get_settings():
    settings = StoreSettings.query.first()
    if not settings:
        settings = StoreSettings()
        db.session.add(settings)
        db.session.commit()
    return settings

# Database create karne ke liye wrapper
with app.app_context():
    db.create_all()

@app.route('/welcome')
def landing():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

# Master Cache Store (In-Memory Database for fast lookup)
CACHE_FILE = 'medicine_cache.json'

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

@app.route('/get_medicine_info/<path:med_name>')
def get_medicine_info(med_name):
    try:
        med_key = med_name.strip().lower()

        # STEP 1: Agar medicine cache me pehle se hai, to seedha vahan se return karo!
        cache_data = load_cache()
        if med_key in cache_data:
            print(f"[FILE CACHE HIT] Serving '{med_name}' instantly from persistent file!")
            return jsonify({
                'status': 'success',
                'uses': cache_data[med_key]['uses'],
                'side_effects': cache_data[med_key]['side_effects']
            })

        # STEP 2: Agar pehli baar search ho raha hai, tabhi Gemini API call hogi
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("API Key missing")

        client = genai.Client(api_key=api_key)

        prompt = f"""
        You are a clinical pharmacy expert.
        Provide accurate medical information for the medicine: '{med_name}'.
        
        Respond STRICTLY in JSON format with two keys:
        1. "uses": A detailed 3-4 line paragraph detailing primary medical uses, indications, and clinical benefits in India.
        2. "side_effects": A detailed 2-3 line paragraph detailing common side effects and safety precautions.

        Keep language simple and professional. Do NOT wrap output in markdown formatting like ```json.
        """

        model_names = ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite' , 'gemini-flash']
        response = None
        
        for m_name in model_names:
            try:
                response = client.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                if response and response.text:
                    break
            except Exception:
                continue

        if not response or not response.text:
            raise RuntimeError("All endpoints failed")

        data = json.loads(response.text)
        uses_data = data.get('uses')
        side_effects_data = data.get('side_effects')

        #  STEP 3: Naya fetched data Cache me save kar lo
        cache_data = load_cache()
        cache_data[med_key] = {'uses': uses_data, 'side_effects': side_effects_data}
        try:
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Cache write error: {e}")

        return jsonify({
            'status': 'success',
            'uses': uses_data,
            'side_effects': side_effects_data
        })

    except Exception as e:
        return jsonify({
            'status': 'success',
            'uses': f"{med_name} is an active therapeutic medication used for targeted symptom relief, managing physiological discomfort, and supporting recovery under prescription.",
            'side_effects': "Safety Note: Read label carefully. Common side effects may include mild nausea, stomach upset, or drowsiness in sensitive individuals."
        })

@app.route('/')
@login_required
def dashboard():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'dashboard'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(url_for('billing'))
    
    store_config = get_settings()
    # 1. Basic Stats
    total_medicines = Medicine.query.count()

    # 2. Near Expiry Count
    all_meds = Medicine.query.all()
    near_expiry_count = 0
    today = get_ist_time()
    alert_days = getattr(store_config, 'expiry_alert_days', 60) or 60

    # 1. Saved Thresholds Load Karo
    THRESHOLDS = {
        'Tablet': getattr(store_config, 'thresh_tablet', 5) or 5,
        'Syrup': getattr(store_config, 'thresh_syrup', 2) or 2,
        'Injection': getattr(store_config, 'thresh_injection', 2) or 2,
        'Ointment': getattr(store_config, 'thresh_ointment', 3) or 3,
        'Capsule': getattr(store_config, 'thresh_capsule', 5) or 5,
        'Other': getattr(store_config, 'thresh_other', 2) or 2
    }

    low_stock_items = []

    # Loop through medicines and filter based on its category limit
    for med in all_meds:
        # Category match (Safe title conversion)
        cat = str(getattr(med, 'category', 'Other') or 'Other').strip().title()
        cat_limit = THRESHOLDS.get(cat, THRESHOLDS.get('Other', 2))

        if med.quantity is not None and float(med.quantity) <= float(cat_limit):
            low_stock_items.append(med)

    low_stock_count = len(low_stock_items)
    preview_low_stock = low_stock_items[:5]
    
    # Safe Near Expiry Calculation
    for med in all_meds:
        if getattr(med, 'expiry_date', None):
            try:
                exp_str = str(med.expiry_date).strip()
                if '/' in exp_str:
                    parts = exp_str.split('/')
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        exp_month, exp_year = int(parts[0]), int(parts[1])
                        if exp_year < 100:
                            exp_year += 2000
                    
                        # Month range validation (1-12)
                        if 1 <= exp_month <= 12:
                            exp_date = datetime(exp_year, exp_month, 1)
                            if exp_date <= today + timedelta(days=alert_days):
                                near_expiry_count += 1
            except Exception:
                pass

    # 3. Today's Sales & Profit
    today_start = get_ist_time().replace(hour=0, minute=0, second=0, microsecond=0)
    today_bills = Sale.query.filter(Sale.created_at >= today_start).all() if 'Sale' in globals() else []
    todays_sales = sum(bill.total_amount for bill in today_bills) if today_bills else 0.0

    # 3. Today's Real Profit & Dynamic Margin Calculation
    actual_today_profit = 0.0

    if today_bills:
        for bill in today_bills:
            for item in bill.items:
                # Match medicine from database to get purchase_price
                med = Medicine.query.get(item.medicine_id)
                if med:
                    # Actual Cost = purchase_price * quantity
                    # Selling Total = item.total
                    cost_price = (med.purchase_price or 0.0) * item.quantity
                    actual_today_profit += (item.total - cost_price)
                else:
                    # Fallback if medicine was deleted: assume 20% margin
                    actual_today_profit += item.total * 0.20

    est_net_profit = max(0.0, actual_today_profit)

    # Dynamic Real Margin Percentage Calculation
    if todays_sales > 0:
        STORE_MARGIN_PERCENT = round((est_net_profit / todays_sales) * 100, 1)
    else:
        STORE_MARGIN_PERCENT = getattr(store_config, 'profit_margin', 20.0) or 20.0

    # 4. Weekly Sales Calculation (Last 7 Days)
    weekly_labels = []
    weekly_sales = []
    
    for i in range(6, -1, -1):
        day_date = datetime.now() - timedelta(days=i)
        day_start = day_date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        day_bills = Sale.query.filter(Sale.created_at >= day_start, Sale.created_at <= day_end).all() if 'Sale' in globals() else []
        day_total = sum(s.total_amount for s in day_bills) if day_bills else 0.0
        
        weekly_labels.append(day_date.strftime('%a'))
        weekly_sales.append(day_total)

    # 5. Stock Categories & Category-wise Margin Analytics
    all_medicines = Medicine.query.all()
    
    categories = ['Tablet', 'Syrup', 'Injection', 'Capsule', 'Other']
    category_data = {c + 's' if not c.endswith('s') else c: 0 for c in categories}
    category_analytics = {}

    for cat in categories:
        cat_key = cat + 's' if not cat.endswith('s') else cat
        cat_meds = [m for m in all_medicines if (m.category or 'Other').strip().title() == cat]
        
        # Stock Count
        category_data[cat_key] = len(cat_meds)
        
        # Margin Calculations
        total_purchase_val = sum((m.purchase_price or 0.0) * (m.quantity or 0) for m in cat_meds)
        total_mrp_val = sum((m.mrp or 0.0) * (m.quantity or 0) for m in cat_meds)
        cat_profit = max(0.0, total_mrp_val - total_purchase_val)
        cat_margin_pct = round((cat_profit / total_mrp_val * 100), 1) if total_mrp_val > 0 else 0.0
        
        category_analytics[cat_key] = {
            'count': len(cat_meds),
            'stock_value': round(total_mrp_val, 2),
            'profit_value': round(cat_profit, 2),
            'margin_pct': cat_margin_pct
        }

    # 6. Recent Billing Transactions
    recent_bills = Sale.query.order_by(Sale.id.desc()).limit(5).all() if 'Sale' in globals() else []

    return render_template(
        'dashboard.html',
        total_medicines=total_medicines,
        low_stock_count=low_stock_count,
        near_expiry_count=near_expiry_count,
        todays_sales=round(todays_sales, 2),
        net_profit=round(est_net_profit, 2),
        margin_percent=STORE_MARGIN_PERCENT,
        low_stock_items=preview_low_stock,
        category_data=category_data,
        category_analytics=category_analytics,
        weekly_labels=weekly_labels,
        weekly_sales=weekly_sales,
        recent_bills=recent_bills
    )

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'settings'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    store_config = get_settings()
    
    if request.method == 'POST':

        store_config.round_off_bills = 'round_off_bills' in request.form
        store_config.enable_negative_stock = 'enable_negative_stock' in request.form
        store_config.require_doctor_name = True if request.form.get('require_doctor_name') else False
        store_config.show_salt_on_print = ('show_salt_on_print' in request.form)
        db.session.commit()

        # 1. Basic Store Details
        store_config.shop_name = request.form.get('shop_name', '').strip()
        store_config.phone = request.form.get('phone', '').strip()
        store_config.email = request.form.get('store_email', '').strip()
        store_config.operating_hours = request.form.get('operating_hours', '').strip()
        store_config.address = request.form.get('address', '').strip()
        store_config.city = request.form.get('city', '').strip()
        store_config.state = request.form.get('state', '').strip()
        store_config.pincode = request.form.get('pincode', '').strip()
        store_config.off_days = request.form.get('off_days', '').strip()
    
        # 2. Owner Details
        store_config.owner_name = request.form.get('owner_name', '').strip()
        store_config.owner_contact = request.form.get('owner_contact', '').strip()
        store_config.owner_email = request.form.get('owner_email', '').strip()
        store_config.owner_pan = request.form.get('owner_pan', '').strip()
        store_config.owner_aadhaar = request.form.get('owner_aadhaar', '').strip()
    
        # 3. Legal & Pharma Details
        store_config.dl_number = request.form.get('dl_number', '').strip()
        store_config.gstin = request.form.get('gstin', '').strip()
        store_config.fssai_no = request.form.get('fssai_no', '').strip()
        store_config.pharmacist_reg_no = request.form.get('pharmacist_reg_no', '').strip()
        store_config.footer_note = request.form.get('footer_note', '').strip()
    
        # 4. FILE UPLOADS HANDLING
        upload_folder = os.path.join('static', 'uploads', 'store_docs')
        os.makedirs(upload_folder, exist_ok=True)

        # Logo Image Upload
        if 'store_logo' in request.files and request.files['store_logo'].filename != '':
            file = request.files['store_logo']
            fname = secure_filename(f"logo_{file.filename}")
            file.save(os.path.join(upload_folder, fname))
            store_config.logo_path = f"uploads/store_docs/{fname}"

        # Owner KYC Doc Upload
        if 'owner_doc' in request.files and request.files['owner_doc'].filename != '':
            file = request.files['owner_doc']
            fname = secure_filename(f"owner_{file.filename}")
            file.save(os.path.join(upload_folder, fname))
            store_config.owner_doc_path = f"uploads/store_docs/{fname}"

        # Legal / License Doc Upload
        if 'legal_doc' in request.files and request.files['legal_doc'].filename != '':
            file = request.files['legal_doc']
            fname = secure_filename(f"legal_{file.filename}")
            file.save(os.path.join(upload_folder, fname))
            store_config.legal_doc_path = f"uploads/store_docs/{fname}"
        
        # Billing
        store_config.receipt_format = request.form.get('receipt_format', 'thermal_80mm')
        store_config.invoice_prefix = request.form.get('invoice_prefix', 'INV/2026/')
        store_config.default_discount = float(request.form.get('default_discount', 0.0))
        
        # Inventory & Taxes
        store_config.expiry_alert_days = int(request.form.get('expiry_alert_days', 60))
        # store_config.profit_margin = float(request.form.get('profit_margin', 20.0))

        # DYNAMIC CATEGORY THRESHOLDS SAVE ---
        store_config.thresh_tablet = int(request.form.get('thresh_tablet', 5))
        store_config.thresh_syrup = int(request.form.get('thresh_syrup', 2))
        store_config.thresh_injection = int(request.form.get('thresh_injection', 2))
        store_config.thresh_ointment = int(request.form.get('thresh_ointment', 3))
        store_config.thresh_capsule = int(request.form.get('thresh_capsule', 5))
        store_config.thresh_other = int(request.form.get('thresh_other', 2))

        # 1. Check if user modified email without OTP verification
        form_email = request.form.get('email', '').strip().lower()
        
        if form_email and form_email != current_user.email.lower():
            flash('Email change requires OTP verification! Please click "Verify & Update" button.', 'warning')
            return redirect(url_for('settings') + '#tab-account')

        # 1. Update Username & Email
        new_username = request.form.get('username')

        if new_username:
            current_user.username = new_username.strip()

        # 2. Admin Security PIN Update (Option 5)
        new_pin = request.form.get('admin_pin', '').strip()
        current_auth = request.form.get('current_admin_auth', '').strip()

        if new_pin:
            is_authorized = False
            if current_auth and current_auth == store_config.admin_pin:
                is_authorized = True
            elif current_auth and hasattr(current_user, 'check_password') and current_user.check_password(current_auth):
                is_authorized = True

            if not is_authorized:
                flash('Authorization failed! Enter correct current PIN or Account Password.', 'danger')
                return redirect(url_for('settings') + '#tab-security')

            store_config.admin_pin = new_pin
            db.session.commit()
            flash('Admin Security PIN updated successfully!', 'success')
            return redirect(url_for('settings') + '#tab-security')

        # 3. Account Password Change Logic (Option 7)
        current_password = request.form.get('current_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if current_password:
            db_user = User.query.get(current_user.id)

            # Check Old Password
            is_valid_pass = False
            if hasattr(db_user, 'check_password'):
                is_valid_pass = db_user.check_password(current_password)
            else:
                is_valid_pass = check_password_hash(db_user.password_hash, current_password)

            if not is_valid_pass:
                flash('Current password is incorrect! Authorization failed.', 'danger')
                return redirect(url_for('settings') + '#tab-account')

            if not new_password or new_password != confirm_password:
                flash('New passwords do not match!', 'danger')
                return redirect(url_for('settings') + '#tab-account')

            if len(new_password) < 6:
                flash('New password must be at least 6 characters long!', 'warning')
                return redirect(url_for('settings') + '#tab-account')

            # Update password
            if hasattr(db_user, 'set_password'):
                db_user.set_password(new_password)
            else:
                db_user.password_hash = generate_password_hash(new_password)

            db.session.commit()
            flash('Password updated successfully!', 'success')
            return redirect(url_for('settings') + '#tab-account')

        # Store Details Save
        db.session.commit()
        flash('Configurations saved successfully!', 'success')
        return redirect(url_for('settings'))

    # Fetch all cashiers directly for template display
    staff_members = User.query.filter_by(role='cashier').all()

    return render_template('settings.html', config=store_config ,current_user=current_user , staff_members=staff_members)

@app.route('/upload-pdf-bill', methods=['POST'])
def upload_pdf_bill():
    if 'bill_pdf' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file uploaded'}), 400

    file = request.files['bill_pdf']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400

    filename = file.filename.lower()

    # ========================================================
    # OPTION A: EXCEL / CSV FILE HANDLING (Pandas Fast Parse)
    # ========================================================
    if filename.endswith('.xlsx') or filename.endswith('.xls') or filename.endswith('.csv'):
        try:
            if filename.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)

            df.columns = [str(c).strip().lower() for c in df.columns]
            
            # Ultra-Comprehensive Keyword Matching for Indian Pharma Invoices / Bills
            name_col = next((c for c in df.columns if any(k in c for k in ['item', 'med', 'product', 'particular', 'description', 'trade', 'drug', 'title', 'name'])), df.columns[0])
            company_col = next((c for c in df.columns if any(k in c for k in ['company', 'mfg', 'brand', 'maker', 'manufacturer', 'comp_name', 'mfr', 'lab', 'pharma'])), None)
            
            # Salt/Composition (Strict check: excludes plain 'c' to prevent mapping company column)
            comp_col = next((c for c in df.columns if any(k in c for k in ['salt', 'composition', 'formula', 'generic', 'molecule', 'active', 'ingred']) and c != company_col), None)
            
            cat_col = next((c for c in df.columns if any(k in c for k in ['category', 'group', 'form', 'dosage', 'cat', 'type'])), None)
            batch_col = next((c for c in df.columns if any(k in c for k in ['batch', 'b.no', 'b_no', 'lot', 'bno', 'b. n', 'b.no.'])), None)
            exp_col = next((c for c in df.columns if any(k in c for k in ['exp', 'expiry', 'exp_date', 'exp.date', 'validity', 'mfg_exp'])), None)
            qty_col = next((c for c in df.columns if any(k in c for k in ['qty', 'pack', 'quantity', 'count', 'units', 'nos', 'strip', 'box', 'free_qty'])), None)
    
            # Indian Pharmacy Pricing Resolution (PTS, P.Rate, PTR, MRP)
            prate_col = next((c for c in df.columns if any(k in c for k in ['pts', 'cost', 'p.rate', 'p_rate', 'pur', 'purchase', 'p.price', 'cost_rate', 'buy_price', 'net_rate', 'net_cost', 'p.rate(₹)', 'p_rate(₹)', 'rate'])), None)
            mrp_col = next((c for c in df.columns if any(k in c for k in ['mrp', 'ptr', 'sale', 'retail', 'price', 's.rate', 's_rate', 'mrp(₹)'] ) and c != prate_col), None)
    
            extracted_items = []
            for _, row in df.iterrows():
                raw_name = str(row.get(name_col, '')).strip() if name_col else ''
                if not raw_name or raw_name.lower() in ['nan', 'none', '', 'total', 'subtotal', 'grand total']:
                    continue
    
                clean_name = re.sub(r'^\d+[\s\.\)]*', '', raw_name).strip()
                
                # Safe Numeric Converter
                def parse_float(val, fallback=0.0):
                    if pd.isna(val): return fallback
                    s = re.sub(r'[^\d\.]', '', str(val))
                    try: return float(s) if s else fallback
                    except: return fallback
    
                qty_val = parse_float(row.get(qty_col), 1.0) if qty_col else 1.0
                prate_val = parse_float(row.get(prate_col), 0.0) if prate_col else 0.0
                mrp_val = parse_float(row.get(mrp_col), prate_val) if mrp_col else prate_val
    
                cat = str(row.get(cat_col, 'Tablet')).strip() if cat_col and pd.notna(row.get(cat_col)) else 'Tablet'
                if 'syrup' in clean_name.lower(): cat = 'Syrup'
                elif 'capsule' in clean_name.lower(): cat = 'Capsule'
                elif 'injection' in clean_name.lower(): cat = 'Injection'
                elif 'ointment' in clean_name.lower() or 'cream' in clean_name.lower(): cat = 'Ointment'
    
                extracted_items.append({
                    'name': clean_name,
                    'company': str(row.get(company_col, '')).strip() if company_col and pd.notna(row.get(company_col)) else '',
                    'composition': str(row.get(comp_col, 'N/A')).strip() if comp_col and pd.notna(row.get(comp_col)) else 'N/A',
                    'category': cat,
                    'batch_no': str(row.get(batch_col, 'BATCH-01')).strip() if batch_col and pd.notna(row.get(batch_col)) else 'BATCH-01',
                    'expiry_date': str(row.get(exp_col, '12/28')).strip() if exp_col and pd.notna(row.get(exp_col)) else '12/28',
                    'quantity': qty_val,
                    'purchase_price': prate_val,
                    'mrp': mrp_val
                })
    
            return jsonify({'status': 'success', 'items': extracted_items})
        except Exception as e:
            return jsonify({'status': 'error', 'message': f"Excel Parse Error: {str(e)}"}), 500

    # ========================================================
    # OPTION B: PDF / IMAGE FILE HANDLING (Gemini AI Vision)
    # ========================================================
    try:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            return jsonify({'status': 'error', 'message': 'GEMINI_API_KEY missing'}), 500

        client = genai.Client(api_key=api_key)

        file_bytes = file.read()
        mime_type = "application/pdf" if filename.endswith('.pdf') else "image/jpeg"

        prompt = """
        You are an expert Indian Pharmacy Invoice / Bill Parser.
        Extract all medicine items listed in the invoice table and respond STRICTLY with a valid JSON array.

        Each object in the array must contain these exact keys:
        1. "name": Exact trade/brand medicine name (e.g. "Dolo 650", "Cipcal 500").
        2. "company": Manufacturer or Brand/Company name if present (e.g. "Cipla", "Alembic"). Otherwise "".
        3. "composition": Chemical composition / Salt / Generic name (e.g. "Paracetamol 650mg"). If missing, return "N/A".
        4. "category": Auto-detect among "Tablet", "Capsule", "Syrup", "Injection", "Ointment", or "Other".
        5. "batch_no": Batch Number (e.g. "CP6012"). If missing, return "BATCH-01".
        6. "expiry_date": Expiry date formatted as "MM/YY" or "MM/YYYY" (e.g. "10/27"). If missing, return "12/28".
        7. "quantity": Total billed quantity as a number (e.g. 50, 30).
        8. "purchase_price": Exact Cost Price / PTS (Price to Stockist / Cost Rate to dukan) as a number (e.g., 62.00, 95.00). If PTS is missing, use PTR.
        9. "mrp": Exact PTR (Price to Retailer / Base Selling Rate) or MRP as a number (e.g., 70.00, 108.00). Must be equal to or greater than purchase_price.

        CRITICAL INSTRUCTIONS:
        - Exclude invoice header details (Billed To, Shipped To, Invoice No, GSTIN).
        - Exclude invoice footer details (GST Summary, Bank Details, Total Amount).
        - Extract ONLY actual medicine rows from the table.
        - Do NOT wrap output in markdown formatting like ```json.
        """

        # model_names = ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite' , 'gemini-flash']
        # EXACT SAME FALLBACK LOOP AS GET_MEDICINE_INFO
        model_names = ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-flash']
        response = None

        for m_name in model_names:
            try:
                response = client.models.generate_content(
                    model=m_name,
                    contents=[
                        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                if response and response.text:
                    break
            except Exception:
                continue

        if not response or not response.text:
            return jsonify({'status': 'error', 'message': 'API endpoints failed. Please try again.'}), 500

        raw_text = response.text
        
        raw_text = raw_text.strip()
        
        raw_text = raw_text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()

        # Fix missing commas, trailing commas, and truncated JSON arrays
        cleaned_json_text = re.sub(r',\s*([\]}])', r'\1', raw_text)
        
        # Repair unclosed array if AI response was truncated
        if cleaned_json_text.startswith('[') and not cleaned_json_text.rstrip().endswith(']'):
            cleaned_json_text = cleaned_json_text.rstrip().rstrip(',') + ']'

        try:
            items = json.loads(cleaned_json_text)
        except Exception:
            # Fallback repair using regex extraction
            json_match = re.search(r'\[.*\]', cleaned_json_text, re.DOTALL)
            if json_match:
                try:
                    items = json.loads(json_match.group(0))
                except Exception:
                    # Partial extract objects if valid syntax fails
                    items = []
                    for obj in re.finditer(r'\{[^{}]*\}', cleaned_json_text):
                        try:
                            items.append(json.loads(obj.group(0)))
                        except Exception:
                            continue
            else:
                return jsonify({'status': 'error', 'message': 'Bill response formatting error. Please try uploading again.'}), 500

        # Cleanup & Safety Types
        for item in items:
            item['quantity'] = float(item.get('quantity', 1) or 1)
            item['purchase_price'] = float(item.get('purchase_price', 0) or 0)
            item['mrp'] = float(item.get('mrp', item['purchase_price']) or item['purchase_price'])

        return jsonify({'status': 'success', 'items': items})

    except Exception as e:
        print(f"AI Bill Extraction Error: {e}")
        return jsonify({'status': 'error', 'message': 'API limit/network delay. Please wait 10 seconds and try again.'}), 500

# @app.route('/upload-pdf-bill', methods=['POST'])
# def upload_pdf_bill():
#     if 'bill_pdf' not in request.files:
#         return jsonify({'status': 'error', 'message': 'No file uploaded'}), 400

#     file = request.files['bill_pdf']
#     if file.filename == '':
#         return jsonify({'status': 'error', 'message': 'No selected file'}), 400

#     extracted_items = []
#     filename = file.filename.lower()

#     # Broad Synonyms Mapping
#     SYNONYMS = {
#         'name': ['item description', 'item name', 'product', 'particular', 'medicine', 'drug', 'description', 'item'],
#         'company': ['company', 'brand', 'mfg', 'manufacturer', 'marketed by', 'lab'],
#         'batch': ['batch', 'batch no', 'batch.no', 'b.no', 'lot', 'batch/lot'],
#         'expiry': ['exp', 'expiry', 'exp.date', 'exp date', 'expiry date', 'exp.'],
#         'qty': ['packs', 'qty', 'quantity', 'billed qty', 'packs qty'],
#         'p_rate': ['pts', 'ptr', 'p.rate', 'purchase rate', 'cost', 'p.price', 'rate', 'pur price', 'pur.rate'],
#         'mrp': ['mrp', 'm.r.p.', 'mrp (₹)', 'max retail price', 'm.r.p']
#     }

#     def match_header(cell_text):
#         if not cell_text:
#             return None
#         clean_text = str(cell_text).upper().replace('\n', ' ').strip()
#         for key, aliases in SYNONYMS.items():
#             for alias in aliases:
#                 if alias.upper() in clean_text:
#                     return key
#         return None

#     try:
#         # ==========================================
#         # 1. EXCEL / CSV FILE PROCESSING
#         # ==========================================
#         if filename.endswith('.xlsx') or filename.endswith('.xls') or filename.endswith('.csv'):
#             if filename.endswith('.csv'):
#                 df = pd.read_csv(file)
#             else:
#                 df = pd.read_excel(file)

#             df.columns = [str(c).strip() for c in df.columns]
#             col_map = {}
#             for col in df.columns:
#                 matched_key = match_header(col)
#                 if matched_key and matched_key not in col_map:
#                     col_map[matched_key] = col

#             for _, row in df.iterrows():
#                 raw_name = str(row.get(col_map.get('name'), '')).strip() if 'name' in col_map else ''
#                 if not raw_name or raw_name.lower() in ['nan', 'none', 'total', 'subtotal', 'item name', 'gst summary']:
#                     continue

#                 # Ignore non-medicine junk lines
#                 if any(junk in raw_name.upper() for junk in ['SHIPPED TO', 'BILLED TO', 'TAXABLE', 'CGST', 'SGST', 'TOTAL']):
#                     continue

#                 lines = [l.strip() for l in raw_name.split('\n') if l.strip()]
#                 clean_name = re.sub(r'^\d+[\s\.\)]*', '', lines[0]).strip()
#                 company_val = lines[1] if len(lines) > 1 else str(row.get(col_map.get('company'), '')).strip()

#                 batch_val = str(row.get(col_map.get('batch'), 'BATCH-01')).strip()
#                 expiry_val = str(row.get(col_map.get('expiry'), '12/28')).strip()

#                 # Clean numeric Qty, P.Rate, MRP
#                 qty_clean = re.sub(r'[^0-9\.]', '', str(row.get(col_map.get('qty'), '1')))
#                 qty_val = float(qty_clean) if qty_clean else 1.0

#                 p_rate_clean = re.sub(r'[^0-9\.]', '', str(row.get(col_map.get('p_rate'), '0')))
#                 p_rate_val = float(p_rate_clean) if p_rate_clean else 0.0

#                 mrp_clean = re.sub(r'[^0-9\.]', '', str(row.get(col_map.get('mrp'), '0')))
#                 mrp_val = float(mrp_clean) if mrp_clean else p_rate_val

#                 cat = 'Tablet'
#                 if 'syrup' in clean_name.lower() or 'suspension' in clean_name.lower():
#                     cat = 'Syrup'
#                 elif 'capsule' in clean_name.lower():
#                     cat = 'Capsule'
#                 elif 'injection' in clean_name.lower() or 'inj' in clean_name.lower():
#                     cat = 'Injection'
#                 elif 'ointment' in clean_name.lower() or 'cream' in clean_name.lower() or 'gel' in clean_name.lower():
#                     cat = 'Ointment'

#                 extracted_items.append({
#                     'name': clean_name,
#                     'company': company_val if company_val != 'nan' else '',
#                     'composition': 'N/A',
#                     'category': cat,
#                     'batch_no': batch_val if batch_val != 'nan' else 'BATCH-01',
#                     'expiry_date': expiry_val if expiry_val != 'nan' else '12/28',
#                     'quantity': qty_val,
#                     'purchase_price': p_rate_val,
#                     'mrp': mrp_val
#                 })

#         # ==========================================
#         # 2. PDF FILE PROCESSING (pdfplumber)
#         # ==========================================
#         else:
#             with pdfplumber.open(file) as pdf:
#                 for page in pdf.pages:
#                     tables = page.extract_tables()
#                     for table in tables:
#                         if not table or len(table) < 2:
#                             continue

#                         header_idx = -1
#                         col_map = {}

#                         # Find Table Header Row dynamically
#                         for row_i, row in enumerate(table):
#                             row_str = " ".join([str(c).upper() for c in row if c])
#                             if any(k in row_str for k in ['ITEM', 'DESCRIPTION', 'PARTICULAR', 'PRODUCT']):
#                                 header_idx = row_i
#                                 for col_i, cell in enumerate(row):
#                                     m_key = match_header(cell)
#                                     if m_key and m_key not in col_map:
#                                         col_map[m_key] = col_i
#                                 break

#                         if header_idx == -1 or 'name' not in col_map:
#                             continue

#                         # Read Data Rows
#                         for row in table[header_idx + 1:]:
#                             if not row or len(row) < 3:
#                                 continue

#                             raw_name_cell = str(row[col_map['name']]).strip() if col_map['name'] < len(row) else ''
#                             if not raw_name_cell or raw_name_cell.upper() in ['NONE', 'NAN', '']:
#                                 continue

#                             # Stop / Skip Junk Rows outside item table
#                             upper_cell = raw_name_cell.upper()
#                             if any(junk in upper_cell for junk in [
#                                 'GST SUMMARY', 'TAXABLE', 'TOTAL', 'BANK ACCOUNT', 'AMOUNT IN WORDS', 
#                                 'SHIPPED TO', 'BILLED TO', 'CGST', 'SGST', 'FREIGHT', 'ROUND OFF'
#                             ]):
#                                 continue

#                             # Split Cell: Line 1 -> Medicine Name, Line 2 -> Company Name
#                             lines = [l.strip() for l in raw_name_cell.split('\n') if l.strip()]
#                             clean_name = re.sub(r'^\d+[\s\.\)]*', '', lines[0]).strip()
                            
#                             company_from_cell = ''
#                             if len(lines) > 1:
#                                 company_from_cell = lines[1]
#                             elif 'company' in col_map and col_map['company'] < len(row):
#                                 company_from_cell = str(row[col_map['company']]).strip()

#                             # Batch & Expiry
#                             batch_val = str(row[col_map['batch']]).strip().split('\n')[0] if 'batch' in col_map and col_map['batch'] < len(row) else 'BATCH-01'
#                             expiry_val = str(row[col_map['expiry']]).strip().split('\n')[0] if 'expiry' in col_map and col_map['expiry'] < len(row) else '12/28'

#                             # Quantity
#                             qty_str = str(row[col_map['qty']]).strip().split('\n')[0] if 'qty' in col_map and col_map['qty'] < len(row) else '1'
#                             qty_clean = re.sub(r'[^0-9\.]', '', qty_str)
#                             qty_val = float(qty_clean) if qty_clean else 1.0

#                             # Purchase Rate (PTR / PTS / P.Rate / Cost)
#                             p_rate_val = 0.0
#                             if 'p_rate' in col_map and col_map['p_rate'] < len(row):
#                                 pr_str = str(row[col_map['p_rate']]).strip().split('\n')[0]
#                                 pr_clean = re.sub(r'[^0-9\.]', '', pr_str)
#                                 p_rate_val = float(pr_clean) if pr_clean else 0.0

#                             # MRP
#                             mrp_val = 0.0
#                             if 'mrp' in col_map and col_map['mrp'] < len(row):
#                                 mrp_str = str(row[col_map['mrp']]).strip().split('\n')[0]
#                                 mrp_clean = re.sub(r'[^0-9\.]', '', mrp_str)
#                                 mrp_val = float(mrp_clean) if mrp_clean else 0.0

#                             # Fallback if MRP missing in invoice, set MRP equal to PTR
#                             if mrp_val == 0.0 and p_rate_val > 0.0:
#                                 mrp_val = p_rate_val

#                             # Auto Category
#                             cat = 'Tablet'
#                             name_lower = clean_name.lower()
#                             if 'syrup' in name_lower or 'suspension' in name_lower or 'ml' in name_lower:
#                                 cat = 'Syrup'
#                             elif 'capsule' in name_lower or 'cap' in name_lower:
#                                 cat = 'Capsule'
#                             elif 'injection' in name_lower or 'inj' in name_lower:
#                                 cat = 'Injection'
#                             elif 'ointment' in name_lower or 'cream' in name_lower or 'gel' in name_lower:
#                                 cat = 'Ointment'

#                             extracted_items.append({
#                                 'name': clean_name,
#                                 'company': company_from_cell,
#                                 'composition': 'N/A',
#                                 'category': cat,
#                                 'batch_no': batch_val if batch_val not in ['None', ''] else 'BATCH-01',
#                                 'expiry_date': expiry_val if expiry_val not in ['None', ''] else '12/28',
#                                 'quantity': qty_val,
#                                 'purchase_price': p_rate_val,
#                                 'mrp': mrp_val
#                             })

#         return jsonify({'status': 'success', 'items': extracted_items})

#     except Exception as e:
#         return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/transactions')
def transactions():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'transactions'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    filter_type = request.args.get('filter', 'all')  # today, week, month, all
    payment_filter = request.args.get('payment', 'all')  # Cash, UPI, Card, all
    selected_date = request.args.get('date', '') # Format: YYYY-MM-DD

    query = Sale.query

    # 1. Custom Calendar Date Filter (Takes precedence if user picks a date)
    if selected_date:
        try:
            target_date = datetime.strptime(selected_date, '%Y-%m-%d')
            start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Sale.created_at >= start_of_day, Sale.created_at <= end_of_day)
            filter_type = 'custom'  # Mark time period as custom date
        except ValueError:
            pass

    # 2. Dropdown Time Filter Logic (If no custom date is selected)
    elif filter_type != 'all':
        now = datetime.now()
        if filter_type == 'today':
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(Sale.created_at >= start_date)
        elif filter_type == 'week':
            start_date = now - timedelta(days=7)
            query = query.filter(Sale.created_at >= start_date)
        elif filter_type == 'month':
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(Sale.created_at >= start_date)

    # 3. Payment Filter Logic (Flexible String Check)
    if payment_filter != 'all':
        if 'UPI' in payment_filter:
            query = query.filter(Sale.payment_mode.like('%UPI%'))
        elif 'Cash' in payment_filter:
            query = query.filter(Sale.payment_mode.like('%Cash%'))
        elif 'Card' in payment_filter:
            query = query.filter(Sale.payment_mode.like('%Card%'))
        else:
            query = query.filter(Sale.payment_mode == payment_filter)

    all_bills = query.order_by(Sale.id.desc()).all()

    # Total Analytics for the selected filter
    total_revenue = sum(bill.total_amount for bill in all_bills)
    total_count = len(all_bills)

    return render_template(
        'transactions.html',
        bills=all_bills,
        filter_type=filter_type,
        payment_filter=payment_filter,
        selected_date=selected_date,
        total_revenue=total_revenue,
        total_count=total_count
    )

@app.route('/inventory')
def inventory():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'inventory'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    store_config = get_settings()
    # Database se saare stocks retrieve kar rahe hain
    # all_medicines = Medicine.query.order_by(Medicine.created_at.desc()).all()

    search_query = request.args.get('search', '').strip()
    category_filter = request.args.get('category', 'all')
    stock_status = request.args.get('status', 'all')  # all, low, near_expiry

    # Settings se Dynamic Category Thresholds Load
    THRESHOLDS = {
        'Tablet': getattr(store_config, 'thresh_tablet', 5) or 5,
        'Syrup': getattr(store_config, 'thresh_syrup', 2) or 2,
        'Injection': getattr(store_config, 'thresh_injection', 2) or 2,
        'Ointment': getattr(store_config, 'thresh_ointment', 3) or 3,
        'Capsule': getattr(store_config, 'thresh_capsule', 5) or 5,
        'Other': getattr(store_config, 'thresh_other', 2) or 2
    }

    # Fetch all records first to apply custom threshold logic
    # all_medicines = Medicine.query.order_by(Medicine.created_at.desc()).all()
    all_medicines = Medicine.query.filter_by(user_id=current_user.id).order_by(Medicine.created_at.desc()).all()

    # Pre-calculate category-wise low stock using live thresholds
    total_low_stock_count = 0
    for med in all_medicines:
        cat = str(getattr(med, 'category', 'Other') or 'Other').strip().title()
        limit = THRESHOLDS.get(cat, THRESHOLDS.get('Other', 2))
        
        # Quantity <= Limit condition matching
        med.is_low_stock = (med.quantity is not None and float(med.quantity) <= float(limit))
        med.threshold_limit = limit

        # Expiry Check (Is Expired / Near Expiry)
        today = datetime.now().date()
        exp_date = parse_expiry(med.expiry_date)
        med.is_expired = True if (exp_date and exp_date < today) else False
        
        if med.is_low_stock:
            total_low_stock_count += 1

    # Now apply search & dropdown filters for display
    displayed_medicines = []
    for med in all_medicines:
        # Search Filter Check
        match_search = True
        if search_query:
            query_lower = search_query.lower()
            match_search = (
                query_lower in (med.name or '').lower() or
                query_lower in (med.company or '').lower() or
                query_lower in (med.batch_no or '').lower()
            )

        # Category Filter Check
        match_category = True
        if category_filter != 'all':
            match_category = (str(getattr(med, 'category', '') or '').strip().lower() == category_filter.strip().lower())

        # Status Filter Check (Low Stock / All)
        match_status = True
        if stock_status == 'low':
            match_status = med.is_low_stock

        if match_search and match_category and match_status:
            displayed_medicines.append(med)

    return render_template(
        'inventory.html',
        medicines=displayed_medicines,
        total_count=len(all_medicines),
        low_stock_count=total_low_stock_count,  # Perfect Dynamic Count Pass
        search_query=search_query,
        category_filter=category_filter,
        stock_status=stock_status
    )

@app.route('/delete-stock/<int:id>')
def delete_stock(id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'inventory_delete'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('inventory'))
    
    # ID ke basis pe stock delete karna
    medicine = Medicine.query.get_or_404(id)
    db.session.delete(medicine)
    db.session.commit()
    return redirect(url_for('inventory') + f'#med-{id}')

@app.route('/edit-stock/<int:id>', methods=['GET', 'POST'])
def edit_stock(id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'inventory_edit'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('inventory'))
    
    medicine = Medicine.query.get_or_404(id)
    
    if request.method == 'POST':
        medicine.name = request.form.get('name')
        medicine.company = request.form.get('company')
        medicine.category = request.form.get('category')
        medicine.composition = request.form.get('composition')
        medicine.batch_no = request.form.get('batch_no')
        medicine.expiry_date = request.form.get('expiry_date')
        medicine.quantity = float(request.form.get('quantity', 0))
        medicine.mrp = float(request.form.get('mrp'))
        medicine.purchase_price = float(request.form.get('purchase_price', 0) or 0)
        medicine.rx_required = True if request.form.get('rx_required') in ['true', 'on', 'True'] or 'rx_required' in request.form else False
        medicine.medicine_type = request.form.get('medicine_type', '').strip()
        medicine.pack_size = int(request.form.get('pack_size', 10) or 10)

        db.session.commit()
        return redirect(url_for('inventory') + f'#med-{id}')
    return render_template('add_stock.html', medicine=medicine)

@app.route('/add-stock', methods=['GET', 'POST'])
def add_stock():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'add_medicine'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    if request.method == 'POST':
        # Form inputs se data extract karna
        name = request.form.get('name')
        company = request.form.get('company')
        category = request.form.get('category')
        composition = request.form.get('composition')
        batch_no = request.form.get('batch_no')
        expiry_date = request.form.get('expiry_date')
        quantity = float(request.form.get('quantity', 0))
        mrp = float(request.form.get('mrp'))
        purchase_price = float(request.form.get('purchase_price', 0) or 0)
        is_rx = ('rx_required' in request.form) or (request.form.get('rx_required') == 'true')
        medicine_type = request.form.get('medicine_type', '')
        pack_size = int(request.form.get('pack_size', 10) or 10)

        # -------------------------------------------------------------
        # DUPLICATE BATCH CHECK LOGIC
        # -------------------------------------------------------------
        # Fetch data safely from JSON payload or HTML Form
        data = request.get_json(silent=True) or request.form or {}
    
        req_name = str(data.get('name') or '').strip()
        req_batch = str(data.get('batch_no') or '').strip()
        req_company = str(data.get('company') or '').strip()
        req_composition = str(data.get('composition') or data.get('salt') or '').strip()
        req_mrp = float(data.get('mrp') or 0.0)
        req_purchase_price = float(data.get('purchase_price', 0) or 0)
        req_qty = float(data.get('quantity') or 0.0)
    
        # Duplicate match query using exact DB Model field names
        existing_med = Medicine.query.filter(
                Medicine.user_id == current_user.id,
                func.lower(Medicine.name) == req_name.lower(),
                func.lower(Medicine.batch_no) == req_batch.lower(),
                Medicine.mrp == req_mrp,
                Medicine.expiry_date == request.form.get('expiry_date', '').strip(),
                Medicine.purchase_price == float(request.form.get('purchase_price', 0) or 0)
        ).first()
    
        if existing_med:
                same_company = (getattr(existing_med, 'company', '') or '').strip().lower() == req_company.lower()
                same_comp = (getattr(existing_med, 'composition', '') or '').strip().lower() == req_composition.lower()
                same_pack = getattr(existing_med, 'pack_size', 10) == int(request.form.get('pack_size', 10) or 10)
    
                if same_company and same_comp and same_pack:
                    # EXACT MATCH FOUND: Quantity increment kar do existing row me!
                    existing_med.quantity = round(float(existing_med.quantity or 0) + req_qty, 2)
                    db.session.commit()
                    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return jsonify({'status': 'success', 'message': f'Stock updated! Added {req_qty} units to existing medicine.'})
                    else:
                        flash(f'Stock updated! Added {req_qty} units to existing medicine.', 'success')
                        return redirect(url_for('inventory')) # Ya jo bhi inventory page ka route function name hai (e.g. url_for('stocks'))

        # If no exact match, create a NEW row as usual below:
        # new_medicine = Medicine(...)

        # DB Model object banana
        new_med = Medicine(
            user_id=current_user.id,
            name=name,
            company=company,
            category=category,
            composition=composition,
            batch_no=batch_no,
            expiry_date=expiry_date,
            quantity=quantity,
            mrp=mrp,
            purchase_price=purchase_price,
            rx_required=is_rx,
            medicine_type=medicine_type,
            pack_size=pack_size
        )

        # Database me Save karna
        db.session.add(new_med)
        db.session.commit()

        return redirect(url_for('inventory'))

    return render_template('add_stock.html')

@app.route('/bulk-save-stock', methods=['POST'])
def bulk_save_stock():
    
    try:
        data = request.get_json()
        items = data.get('items', [])

        for item in items:
            # Clean item field values for strict check
            req_name = str(item.get('name', '')).strip()
            req_batch = str(item.get('batch_no', 'BATCH-01')).strip()
            req_company = str(item.get('company', '')).strip()
            req_comp = str(item.get('composition', 'N/A')).strip()
            req_expiry = str(item.get('expiry_date', '12/28')).strip()
            req_mrp = float(item.get('mrp', 0.0) or 0.0)
            req_pprice = float(item.get('purchase_price', 0.0) or 0.0)
            req_qty = float(item.get('quantity', 1) or 1)
    
            # STRICT DB QUERY: Match Name, Batch, MRP, Expiry & Purchase Price
            existing_med = Medicine.query.filter(
                Medicine.user_id == current_user.id,
                func.lower(Medicine.name) == req_name.lower(),
                func.lower(Medicine.batch_no) == req_batch.lower(),
                Medicine.mrp == req_mrp,
                Medicine.expiry_date == req_expiry,
                Medicine.purchase_price == req_pprice
            ).first()
    
            if existing_med:
                # Check Company and Composition too
                same_company = (getattr(existing_med, 'company', '') or '').strip().lower() == req_company.lower()
                same_comp = (getattr(existing_med, 'composition', '') or '').strip().lower() == req_comp.lower()
                same_pack = getattr(existing_med, 'pack_size', 10) == int(request.form.get('pack_size', 10) or 10)
    
                if same_company and same_comp and same_pack:
                    # 100% MATCH FOUND: Quantity Increment Karo!
                    existing_med.quantity = round(float(existing_med.quantity or 0) + req_qty, 2)
                else:
                    # Discrepancy in Company/Salt: Create NEW Row
                    new_med = Medicine(
                        user_id=current_user.id,
                        name=req_name, company=req_company, category=item.get('category', 'Tablet'),
                        composition=req_comp, batch_no=req_batch, expiry_date=req_expiry,
                        quantity=req_qty, purchase_price=req_pprice, mrp=req_mrp,
                        pack_size=10, rx_required=item.get('rx_required', False)
                    )
                    db.session.add(new_med)
            else:
                # NO MATCH: Create NEW Row
                new_med = Medicine(
                    user_id=current_user.id,
                    name=req_name, company=req_company, category=item.get('category', 'Tablet'),
                    composition=req_comp, batch_no=req_batch, expiry_date=req_expiry,
                    quantity=req_qty, purchase_price=req_pprice, mrp=req_mrp,
                    pack_size=10, rx_required=item.get('rx_required', False)
                )
                db.session.add(new_med)

        db.session.commit()
        return {'status': 'success', 'message': 'Bulk stock imported successfully!'}

    except Exception as e:
        db.session.rollback()
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/api/delete-transaction/<int:sale_id>', methods=['POST'])
def delete_transaction(sale_id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'delete_bill'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('transactions'))
    
    try:
        data = request.get_json() or {}
        passcode = data.get('passcode', '')
        store_config = get_settings()

        if passcode != store_config.admin_pin:
            return {'status': 'error', 'message': 'Incorrect Security PIN! Authorization denied.'}, 403

        sale = Sale.query.get_or_404(sale_id)
        # Restore stock logic...
        items = getattr(sale, 'items', None) or getattr(sale, 'sale_items', [])
        for item in items:
            if hasattr(item, 'medicine_id') and item.medicine_id:
                medicine = Medicine.query.get(item.medicine_id)
                if medicine:
                    pack_size = int(getattr(medicine, 'pack_size', 10) or getattr(medicine, 'strip_size', 10) or 10)
                    med_display = str(getattr(item, 'medicine_name', '') or '')
    
                    # Current stock in terms of total tablets
                    current_total_tabs = round(float(medicine.quantity or 0.0) * pack_size)
    
                    # Identify if item was sold as loose tablet
                    if '[LOOSE-' in med_display:
                        # Restore exact number of sold loose tabs
                        restore_tabs = int(float(item.quantity))
                    else:
                        # Full strip restore
                        restore_tabs = int(float(item.quantity) * pack_size)

                    # Add back exact tabs and update DB strip value
                    new_total_tabs = current_total_tabs + restore_tabs
                    medicine.quantity = round(new_total_tabs / pack_size, 4)
                    
        for item in items:
            db.session.delete(item)
        db.session.delete(sale)
        db.session.commit()

        return {'status': 'success', 'message': f'Transaction deleted successfully!'}
    except Exception as e:
        db.session.rollback()
        return {'status': 'error', 'message': str(e)}, 500

# 1. BILLING PAGE VIEW
@app.route('/billing')
def billing():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'billing'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('dashboard'))
    
    store_config = get_settings()
    # Only show medicines that are in stock (>0)
    medicines = Medicine.query.filter(Medicine.quantity > 0).all()
    customers = Customer.query.all() #For Ledger

    # Session se pending cart items fetch karke clear karo
    pending_items = session.pop('pending_cart', [])

    return render_template('billing.html', medicines=medicines , store_config=store_config, customers=customers , pending_items=pending_items)

# 2. CHECKOUT & STOCK DEDUCTION LOGIC
@app.route('/process-sale', methods=['POST'])
def process_sale():
    store_config = get_settings()
    try:
        data = request.get_json() or {}
        items = data.get('items', [])
        customer_name = data.get('customer_name', 'Walk-in Customer')
        customer_phone = data.get('customer_phone', '').strip()
        payment_mode = data.get('payment_mode', 'Cash')
        overall_discount = float(data.get('overall_discount', 0.0) or 0.0)
        customer_id = data.get('customer_id')
        doctor_name = data.get('doctor_name', '').strip()

        if not items:
            return {'status': 'error', 'message': 'Cart is empty!'}, 400

        grand_total = 0.0

        # Create main Sale instance
        new_sale = Sale(
            customer_name=customer_name or 'Walk-in Customer',
            customer_phone=customer_phone,
            doctor_name=doctor_name,
            payment_mode=payment_mode,
            total_amount=0.0,
            discount_percent=overall_discount,
            created_at=get_ist_time()
        )
        db.session.add(new_sale)
        db.session.flush()  # Gets generated new_sale.id

        for cart_item in items:
            med_id = cart_item.get('id') or cart_item.get('medicine_id') or cart_item.get('med_id')
            qty = float(cart_item.get('qty', cart_item.get('quantity', 1)))
            unit = cart_item.get('unit', 'Strip') # Get selected unit ('Tab' or 'Strip')
    
            medicine = Medicine.query.get(med_id) if med_id else None
            med_name = cart_item.get('name') or cart_item.get('medicine_name') or (medicine.name if medicine else 'Medicine Item')
    
            # Base MRP per Strip
            base_mrp = float(cart_item.get('mrp', cart_item.get('price', medicine.mrp if medicine else 0.0)))
            # Fetch dynamic pack_size from Medicine Model (Default 10)
            strip_size = float(getattr(medicine, 'pack_size', 10) or getattr(medicine, 'strip_size', 10) or 10)
    
            # 1. Dynamic Unit Price (Per Tab vs Per Strip)
            unit_raw = str(cart_item.get('unit', 'Strip')).strip().lower()
            is_loose = unit_raw in ['tab', 'loose', 'tablet', 'piece']

            if is_loose:
                unit_mrp = round(base_mrp / strip_size, 2)
                display_name = f"{med_name} [LOOSE-{qty}Tab]"
            else:
                unit_mrp = base_mrp
                display_name = med_name

            item_disc = float(cart_item.get('discount', cart_item.get('disc', 0.0)) or 0.0)
            effective_disc = item_disc if item_disc > 0 else overall_discount

            # 2. Calculate Effective Unit Price and Item Total
            discount_amount = unit_mrp * (effective_disc / 100.0)
            final_unit_price = round(unit_mrp - discount_amount, 2)
            item_total = round(final_unit_price * qty, 2)

            grand_total += item_total

            # 3. Create SaleItem with Exact Tablet/Strip Effective Unit Price
            sale_item = SaleItem(
                sale_id=new_sale.id,
                medicine_id=med_id if med_id else (medicine.id if medicine else None),
                medicine_name=display_name,
                quantity=qty,
                price=final_unit_price, # Actual Unit Price (Loose or Strip)
                total=item_total,
                discount_percent=float(cart_item.get('discount', 0) or 0)
            )
    
            if hasattr(sale_item, 'mrp'):
                sale_item.mrp = unit_mrp
            if hasattr(sale_item, 'discount_percent'):
                sale_item.discount_percent = effective_disc
        
            db.session.add(sale_item)

            # Stock Deduction Logic (Exact Tablet Modulo System)
            if medicine:
                pack_size = int(getattr(medicine, 'pack_size', 10) or getattr(medicine, 'strip_size', 10) or 10)
                selected_unit = str(cart_item.get('unit', 'Strip')).strip().lower()
                is_loose_unit = selected_unit in ['tab', 'tablet', 'loose', 'piece']

                # Calculate total stock in terms of individual tablets/units
                current_total_tabs = round(float(medicine.quantity or 0.0) * pack_size)

                if is_loose_unit:
                    # Deduct exact number of loose tabs requested (e.g. 2 tabs)
                    deduct_tabs = int(qty)
                else:
                    # Full strip sale: deduct full strip worth of tabs (e.g. 1 strip = pack_size tabs)
                    deduct_tabs = int(qty * pack_size)

                # Subtract exact tablets and convert back to fractional strip float
                remaining_tabs = max(0, current_total_tabs - deduct_tabs)
                medicine.quantity = round(remaining_tabs / pack_size, 4)

        # Final bill amount
        if store_config.round_off_bills:
            final_total = float(round(grand_total))
        else:
            final_total = float(round(grand_total, 2))

        new_sale.total_amount = final_total

        # Check if Payment Mode is Credit / Udhar
        if payment_mode.lower() == 'credit' or payment_mode == 'udhar':
            customer = None
            
            # Step A: Find Customer by ID or Phone
            if customer_id:
                customer = Customer.query.get(customer_id)
            elif customer_phone:
                customer = Customer.query.filter_by(phone=customer_phone).first()
                if not customer and customer_name and customer_name != 'Walk-in Customer':
                    customer = Customer(name=customer_name, phone=customer_phone)
                    db.session.add(customer)
                    db.session.flush()

            # Step B: Auto Add to Customer Ledger
            if customer:
                credits = sum(l.amount for l in customer.ledger_entries if l.txn_type == 'credit')
                debits = sum(l.amount for l in customer.ledger_entries if l.txn_type == 'debit')
                curr_bal = credits - debits
                
                ledger_entry = CustomerLedger(
                    customer_id=customer.id,
                    txn_type='credit',
                    amount=new_sale.total_amount,
                    balance_after=curr_bal + new_sale.total_amount,
                    note=f"Bill #{new_sale.id} (Udhar Purchase)"
                )
                db.session.add(ledger_entry)
        db.session.commit()

        return {
            'status': 'success',
            'bill_id': f"INV-{new_sale.id:04d}",
            'sale_id': new_sale.id,
            'total_amount': new_sale.total_amount
        }

    except Exception as e:
        db.session.rollback()
        print(f"Checkout Error Exception: {str(e)}")
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/api/bill-details/<int:bill_id>')
def bill_details_api(bill_id):
    # Fetch sale using Sale model
    sale = Sale.query.get_or_404(bill_id)
    items_data = []

    # Iterate over linked SaleItem records
    items = getattr(sale, 'items', None) or getattr(sale, 'sale_items', [])
    
    for item in items:
        # Fetch linked Medicine to get original MRP
        medicine = Medicine.query.get(item.medicine_id) if getattr(item, 'medicine_id', None) else None
        med_name = getattr(item, 'medicine_name', None) or (medicine.name if medicine else 'Medicine Item')

        # Original MRP (Inventory Full Strip MRP)
        base_mrp = float(getattr(item, 'mrp', 0.0) or (medicine.mrp if medicine else item.price))
        selling_price = float(item.price)
        
        # MRP direct inventory/full strip wali dikhani hai
        effective_mrp = base_mrp
        
        # Quantity & Total Amount
        qty = float(item.quantity)
        net_amount = float(getattr(item, 'total', qty * selling_price))
        
        # Direct Saved Discount Fetch
        disc_percent = float(getattr(item, 'discount_percent', getattr(item, 'discount', 0.0)) or 0.0)

        items_data.append({
            'med_name': med_name,
            'batch_no': getattr(item, 'batch_no', None) or (getattr(medicine, 'batch_no', 'N/A') if medicine else 'N/A'),
            'quantity': qty,
            'mrp': effective_mrp, # Effective Per-Tab/Per-Strip MRP
            'discount': disc_percent, # Actual User Applied Discount %
            'rate': selling_price,
            'total': net_amount,
            'medicine_type': getattr(medicine, 'medicine_type', '') if medicine else '',
            'rx_required': getattr(medicine, 'rx_required', False) if medicine else False
        })

    return {
        'status': 'success',
        'bill_id': f"INV-{sale.id:04d}",
        'customer': sale.customer_name or 'Walk-in Customer',
        'customer_phone': sale.customer_phone or 'N/A',
        'doctor_name': sale.doctor_name or 'No Prescription Required',
        'date': sale.created_at.strftime('%d %b %Y, %I:%M %p') if hasattr(sale.created_at, 'strftime') else str(sale.created_at),
        'payment_mode': sale.payment_mode or 'Cash',
        'total_amount': sale.total_amount,
        'items': items_data
    }

@app.route('/print-bill/<int:sale_id>')
def print_bill(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    store_config = get_settings() # Auto-fetches Shop Name, Address, DL No, Footer
    
    return render_template(
        'print_receipt.html',
        sale=sale,
        config=store_config,
        Medicine=Medicine
    )

def parse_expiry(expiry_str):
    if not expiry_str:
        return None
    
    if hasattr(expiry_str, 'date'):
        return expiry_str.date()
    elif isinstance(expiry_str, datetime):
        return expiry_str.date()
        
    expiry_str = str(expiry_str).strip()
    
    # Supported Formats Parse
    for fmt in ('%m/%Y', '%m/%y', '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            dt = datetime.strptime(expiry_str, fmt).date()
            if fmt in ('%m/%Y', '%m/%y'):
                if dt.month == 12:
                    dt = dt.replace(day=31)
                else:
                    dt = (dt.replace(month=dt.month + 1, day=1) - timedelta(days=1))
            return dt
        except ValueError:
            pass
    return None

@app.route('/inventory/alerts')
def inventory_alerts():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'alerts'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    # Store settings fetch (aapka model store_setting / StoreSetting object)
    store_config = get_settings()
    alert_days = getattr(store_config, 'expiry_alert_days', 60) or 60
    
    # Direct database se saved editable thresholds load karo
    THRESHOLDS = {
        'Tablet': getattr(store_config, 'thresh_tablet', 5) or 5,
        'Syrup': getattr(store_config, 'thresh_syrup', 2) or 2,
        'Injection': getattr(store_config, 'thresh_injection', 2) or 2,
        'Ointment': getattr(store_config, 'thresh_ointment', 3) or 3,
        'Capsule': getattr(store_config, 'thresh_capsule', 5) or 5,
        'Other': getattr(store_config, 'thresh_other', 2) or 2
    }
    
    today = datetime.now().date()
    threshold_date = today + timedelta(days=alert_days)
    
    all_medicines = Medicine.query.all()
    
    expired_list = []
    near_expiry_list = []
    low_stock_list = []
    out_of_stock_list = []  # New Out of Stock list

    for med in all_medicines:
        # Category matching (Case-insensitive & Safe)
        med_category = str(getattr(med, 'category', 'Other') or 'Other').strip().title()

        # Matching threshold limit fetch karo (Default 2 if category unknown)
        limit = THRESHOLDS.get(med_category, THRESHOLDS.get('Other', 2))

        # Stock Filters Logic (Safe Check for 0.0, None, or < 0)
        if med.quantity is None or float(med.quantity or 0) <= 0.001:
            # Explicitly 0, None, ya negative stock -> OUT OF STOCK
            out_of_stock_list.append(med)
        elif float(med.quantity) <= float(limit):
            # Stock 0 se bada hai lekin threshold se kam -> LOW STOCK
            low_stock_list.append(med)

        # Expiry Filter
        exp_date = parse_expiry(med.expiry_date)
        if exp_date:
            if exp_date < today:
                expired_list.append(med)
            elif today <= exp_date <= threshold_date:
                near_expiry_list.append(med)

    return render_template(
        'alerts.html',
        expired=expired_list,
        near_expiry=near_expiry_list,
        low_stock=low_stock_list,
        out_of_stock=out_of_stock_list,
        out_of_stock_items=out_of_stock_list,
        alert_days=alert_days,
        today=today
    )

@app.route('/ledger')
def customer_ledger():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'ledger'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    customers = Customer.query.all()
    customer_data = []
    
    total_udhar_market = 0.0
    
    for c in customers:
        # Calculate total pending balance
        # Credit (+) minus Debit (-)
        credits = sum(l.amount for l in c.ledger_entries if l.txn_type == 'credit')
        debits = sum(l.amount for l in c.ledger_entries if l.txn_type == 'debit')
        pending_balance = credits - debits
        
        if pending_balance > 0:
            total_udhar_market += pending_balance
            
        customer_data.append({
            'customer': c,
            'pending_balance': pending_balance,
            'last_txn': c.ledger_entries[-1].date if c.ledger_entries else None
        })
        
    return render_template('ledger.html',
        customers=customer_data,
        total_market_due=total_udhar_market)

@app.route('/ledger/settle', methods=['POST'])
def settle_customer_payment():
    customer_id = request.form.get('customer_id')
    amount_paid = float(request.form.get('amount_paid', 0.0))
    note = request.form.get('note', 'Cash Settlement')
    
    if customer_id and amount_paid > 0:
        c = Customer.query.get(customer_id)
        if c:
            # Add Debit entry (Payment received)
            credits = sum(l.amount for l in c.ledger_entries if l.txn_type == 'credit')
            debits = sum(l.amount for l in c.ledger_entries if l.txn_type == 'debit')
            curr_bal = credits - debits
            
            new_entry = CustomerLedger(
                customer_id=c.id,
                txn_type='debit',
                amount=amount_paid,
                balance_after=curr_bal - amount_paid,
                note=note
            )
            db.session.add(new_entry)
            db.session.commit()
            
    return redirect('/ledger')

@app.route('/ledger/customer/add', methods=['POST'])
def add_customer():
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    address = request.form.get('address', '').strip()
    opening_balance = float(request.form.get('opening_balance', 0.0) or 0.0)
    
    if name and phone:
        existing = Customer.query.filter_by(phone=phone).first()
        if not existing:
            new_cust = Customer(name=name, phone=phone, address=address)
            db.session.add(new_cust)
            db.session.flush() # ID generate karne ke liye
            
            # Agar initial udhar daala hai toh automatically credit entry create karo
            if opening_balance > 0:
                initial_entry = CustomerLedger(
                    customer_id=new_cust.id,
                    txn_type='credit',
                    amount=opening_balance,
                    balance_after=opening_balance,
                    note='Opening Udhar Balance'
                )
                db.session.add(initial_entry)
                
            db.session.commit()
            
    return redirect('/ledger')

@app.route('/ledger/customer/edit', methods=['POST'])
def edit_customer():
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'ledger_edit'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('ledger'))
    
    customer_id = request.form.get('customer_id')
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    address = request.form.get('address', '').strip()
    opening_balance_str = request.form.get('opening_balance', '')
    
    if customer_id:
        c = Customer.query.get(customer_id)
        if c:
            c.name = name
            c.phone = phone
            c.address = address
            
            # Target Outstanding Balance jo user chahta hai
            if opening_balance_str != '':
                target_balance = float(opening_balance_str or 0.0)
                
                # Current Existing Balance calculate karo
                credits = sum(l.amount for l in c.ledger_entries if l.txn_type == 'credit')
                debits = sum(l.amount for l in c.ledger_entries if l.txn_type == 'debit')
                current_balance = credits - debits
                
                # Farak (Difference) calculate karo
                diff = target_balance - current_balance
                
                if diff > 0:
                    # Udhar badhana hai
                    adj_entry = CustomerLedger(
                        customer_id=c.id,
                        txn_type='credit',
                        amount=diff,
                        balance_after=target_balance,
                        note='Balance Adjusted (Manual Edit)'
                    )
                    db.session.add(adj_entry)
                elif diff < 0:
                    # Udhar kam karna hai
                    adj_entry = CustomerLedger(
                        customer_id=c.id,
                        txn_type='debit',
                        amount=abs(diff),
                        balance_after=target_balance,
                        note='Balance Adjusted (Manual Edit)'
                    )
                    db.session.add(adj_entry)
                    
            db.session.commit()
            
    return redirect('/ledger')

# Delete Customer Ledger Route
@app.route('/ledger/customer/delete/<int:customer_id>', methods=['POST'])
def delete_customer(customer_id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'ledger_delete'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('ledger'))
    
    customer = Customer.query.get_or_404(customer_id)
    
    # Customer model me cascade="all, delete-orphan" ki wajah se
    # iski saari ledger entries bhi auto-delete ho jayengi
    db.session.delete(customer)
    db.session.commit()
    
    return redirect('/ledger')

# 1. Disease Tags Management Page
@app.route('/symptom-tags', methods=['GET', 'POST'])
def symptom_tags():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'symptom_tags'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))

    if request.method == 'POST':
        tag_name = request.form.get('tag_name', '').strip()
        description = request.form.get('description', '').strip()
        
        if tag_name:
            existing = DiseaseTag.query.filter_by(name=tag_name).first()
            if not existing:
                new_tag = DiseaseTag(name=tag_name, description=description)
                db.session.add(new_tag)
                db.session.commit()
        return redirect('/symptom-tags')

    tags = DiseaseTag.query.all()
    medicines = Medicine.query.all()
    return render_template('symptom_tags.html', tags=tags, medicines=medicines)

@app.route('/edit_symptom_tag/<int:tag_id>', methods=['POST'])
@login_required
def edit_symptom_tag(tag_id):
    tag = DiseaseTag.query.get_or_404(tag_id)
    tag.name = request.form.get('name')
    tag.description = request.form.get('description')
    
    db.session.commit()
    flash('Tag updated successfully!', 'success')
    return redirect(url_for('symptom_tags'))

# 2. Add Medicine to Disease Tag
@app.route('/symptom-tags/add-medicine', methods=['POST'])
def add_medicine_to_tag():
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'map_medicine'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('symptom_tags'))
    
    tag_id = request.form.get('tag_id')
    medicine_id = request.form.get('medicine_id')
    dosage_note = request.form.get('dosage_note', '').strip()
    target_age = request.form.get('target_age', 'all')

    if tag_id and medicine_id:
        # Check if already mapped
        existing = TagMedicineMap.query.filter_by(tag_id=tag_id, medicine_id=medicine_id).first()
        if not existing:
            mapping = TagMedicineMap(tag_id=tag_id, medicine_id=medicine_id, dosage_note=dosage_note, target_age=target_age)
            db.session.add(mapping)
        else:
            # Agar pehle se mapped hai to update kar do
            existing.dosage_note = dosage_note
            existing.target_age = target_age

        db.session.commit()

    return redirect('/symptom-tags')

@app.route('/symptom-tags/update-tag-age/<int:tag_id>', methods=['POST'])
@login_required
def update_tag_age(tag_id):
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'settings'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(url_for('symptom_tags'))

    tag = DiseaseTag.query.get_or_404(tag_id)
    tag.target_age = request.form.get('target_age', 'all')
    db.session.commit()
    flash(f'Target Age for "{tag.name}" updated successfully!', 'success')
    return redirect(url_for('symptom_tags'))

@app.route('/symptom-tags/update-mapping/<int:map_id>', methods=['POST'])
@login_required
def update_tag_mapping(map_id):
    mapping = TagMedicineMap.query.get_or_404(map_id)
    mapping.dosage_note = request.form.get('dosage_note', '').strip()
    db.session.commit()
    flash('Dosage updated successfully!', 'success')
    return redirect(url_for('symptom_tags'))

# 3. Delete Mapping / Delete Tag
@app.route('/symptom-tags/delete-mapping/<int:map_id>')
def delete_tag_mapping(map_id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'delete_mapping'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('symptom_tags'))
    
    mapping = TagMedicineMap.query.get_or_404(map_id)
    db.session.delete(mapping)
    db.session.commit()
    return redirect('/symptom-tags')

@app.route('/symptom-tags/delete-tag/<int:tag_id>')
def delete_disease_tag(tag_id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'delete_tag'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('symptom_tags'))
    
    tag = DiseaseTag.query.get_or_404(tag_id)
    db.session.delete(tag)
    db.session.commit()
    return redirect('/symptom-tags')

# 1. Smart Symptom Assistant Counter Page
@app.route('/symptom-assistant')
def symptom_assistant():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'assistant'):
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('billing'))
    
    tags = DiseaseTag.query.all()
    return render_template('symptom_assistant.html', tags=tags)

# 2. API to get medicines based on selected Tag IDs
@app.route('/api/get-symptom-medicines', methods=['POST'])
def get_symptom_medicines():
    data = request.get_json() or {}
    tag_ids = data.get('tag_ids', [])
    
    if not tag_ids:
        return {'status': 'success', 'medicines': []}

    # Fetch mappings for all selected tags
    mappings = TagMedicineMap.query.filter(TagMedicineMap.tag_id.in_(tag_ids)).all()
    
    med_dict = {}
    for m in mappings:
        med = m.medicine
        if med and med.id not in med_dict:
            med_dict[med.id] = {
                'id': med.id,
                'name': med.name,
                'batch': getattr(med, 'batch_no', None) or getattr(med, 'batch_number', None) or getattr(med, 'batch', 'N/A'),
                'batch_no': getattr(med, 'batch_no', None) or getattr(med, 'batch_number', None) or getattr(med, 'batch', 'N/A'),
                'stock': med.quantity,
                'mrp': med.mrp,
                'strip_size': float(getattr(med, 'strip_size', 10) or 10),
                'dosage': m.dosage_note or 'As prescribed',
                'tags': [m.tag.name],
                'rx_required': getattr(med, 'rx_required', False),
                'medicine_type': getattr(med, 'medicine_type', '')
            }
        elif med and med.id in med_dict:
            med_dict[med.id]['tags'].append(m.tag.name)

    return {'status': 'success', 'medicines': list(med_dict.values())}

# API route jab user Assistant page se "Add to POS" dabayega
@app.route('/api/add-symptom-to-cart', methods=['POST'])
def add_symptom_to_cart():
    data = request.get_json() or {}
    raw_id = data.get('id')
    
    if raw_id is not None:
        try:
            med_id = int(raw_id)
        except (ValueError, TypeError):
            med_id = None

        if med_id:
            medicine = Medicine.query.get(med_id)
            if medicine:
                # Session cart fetch karo
                pending_cart = session.get('pending_cart', [])
                
                # Check if already added in session queue
                found = False
                for item in pending_cart:
                    if int(item['id']) == medicine.id:
                        item['quantity'] += 1
                        found = True
                        break
                
                if not found:
                    pending_cart.append({
                        'id': medicine.id,
                        'name': medicine.name,
                        'company': getattr(medicine, 'company_name', '') or getattr(medicine, 'manufacturer', '') or '',
                        'price': float(medicine.mrp),
                        'mrp': float(medicine.mrp),
                        'quantity': 1,
                        'discount': 0,
                        'maxStock': medicine.quantity,
                        'unit': 'Strip',
                        'packSize': int(getattr(medicine, 'strip_size', 10) or 10),
                        'category': getattr(medicine, 'category', 'Tablet') or 'Tablet'
                    })
                
                session['pending_cart'] = pending_cart
                session.modified = True
                return {'status': 'success', 'message': f'{medicine.name} added to Billing Queue!'}

    return {'status': 'error', 'message': 'Medicine not found or invalid ID'}, 400

# Excel Sales & Profit Export API Route
@app.route('/api/export/sales-excel', methods=['GET'])
@admin_required
def export_sales_excel():
    # Filter type: 'today', 'week', 'month', ya custom range
    range_type = request.args.get('range', 'month')
    now = datetime.now()
    
    if range_type == 'today':
        start_date = now.replace(hour=0, minute=0, second=0)
    elif range_type == 'week':
        start_date = now - timedelta(days=7)
    elif range_type == 'month':
        start_date = now - timedelta(days=30)
    else:
        start_date = datetime(2000, 1, 1)

    # Database query for transactions/sales
    sales = Sale.query.filter(Sale.created_at >= start_date).all()
    
    data = []
    for s in sales:
        items = getattr(s, 'items', [])
        
        # Check overall discount with fallbacks
        overall_disc = float(
            getattr(s, 'discount_percent', 0) or
            getattr(s, 'discount', 0) or
            getattr(s, 'overall_discount', 0) or 0
        )
        
        if overall_disc > 0:
            final_disc_pct = overall_disc
        else:
            final_disc_pct = sum([float(getattr(item, 'discount_percent', 0) or getattr(item, 'discount', 0) or 0) for item in items])

        cost_price_sum = 0.0
        for item in items:
            med_id = getattr(item, 'medicine_id', None)
            med = Medicine.query.get(med_id) if med_id else None
            qty = float(getattr(item, 'quantity', 1) or 1)
            billed_total = float(getattr(item, 'total', 0) or 0)
            
            if med:
                p_price = float(getattr(med, 'purchase_price', 0) or 0)
                cost_price_sum += (p_price * qty) if p_price > 0 else (billed_total * 0.70)
            else:
                cost_price_sum += (billed_total * 0.70)

        total_bill_amount = float(getattr(s, 'total_amount', 0) or 0)

        # Net Profit Calculation
        if cost_price_sum > 0 and total_bill_amount > 0:
            net_profit = total_bill_amount - cost_price_sum
            if net_profit <= 0:
                net_profit = total_bill_amount * 0.18
        else:
            net_profit = total_bill_amount * 0.18

        data.append({
            'Invoice No': getattr(s, 'id', 'N/A'),
            'Date': getattr(s, 'created_at', datetime.now()).strftime('%Y-%m-%d %H:%M') if isinstance(getattr(s, 'created_at', None), datetime) else str(getattr(s, 'created_at', 'N/A')),
            'Customer Name': getattr(s, 'customer_name', 'Walk-in Customer'),
            'Phone': getattr(s, 'customer_phone', 'N/A'),
            'Payment Mode': getattr(s, 'payment_mode', 'Cash'),
            'Total Amount (₹)': round(total_bill_amount, 2),
            'Total Discount (%)': f"{round(final_disc_pct, 2)}%",
            'Estimated Profit (₹)': round(net_profit, 2)
        })

    # Create Pandas DataFrame
    df = pd.DataFrame(data)
    
    # Save to memory buffer
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Sales Report')
    
    output.seek(0)
    
    filename = f"Medicofiles_Sales_Report_{range_type}_{now.strftime('%d%b%Y')}.xlsx"
    return send_file(output, download_name=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/reactivate-account', methods=['POST'])
def reactivate_account():
    user_id = session.get('deactivated_user_id')
    if not user_id:
        flash('Session expired. Please log in again.', 'danger')
        return redirect(url_for('login'))
        
    password = request.form.get('password')
    user = User.query.get_or_404(user_id)
    
    if check_password_hash(user.password_hash, password):
        user.is_deactivated = False
        user.deactivated_at = None
        db.session.commit()
        
        session.pop('deactivated_user_id', None)
        login_user(user)
        flash('Welcome back! Your account and store data have been fully reactivated.', 'success')
        return redirect(url_for('dashboard'))
    else:
        flash('Incorrect password! Reactivation failed.', 'danger')
        return render_template('reactivate_account.html', username=user.username, deactivated_at=user.deactivated_at)

@app.route('/deactivate-account', methods=['POST'])
@login_required
def deactivate_account():
    if current_user.role != 'admin':
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(request.referrer or url_for('settings'))
    
    password = request.form.get('password')
    if not password or not check_password_hash(current_user.password_hash, password):
        flash('Incorrect password! Account deactivation cancelled.', 'danger')
        return redirect(url_for('settings'))

    user = User.query.get(current_user.id)
    user.is_deactivated = True
    user.deactivated_at = datetime.now()
    db.session.commit()

    logout_user()
    session.clear()

    flash('Your account has been deactivated. You can reactivate it anytime within 30 days by simply logging in.', 'warning')
    return redirect(url_for('login'))

# 1. LOGIN ROUTE (With Case-Insensitive Email Check)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        # Accept either email or username from the form field
        login_input = request.form.get('username') or request.form.get('email')
        password = request.form.get('password')

        # Check against both username OR email
        user = User.query.filter(
            (User.username == login_input) | (User.email == login_input)
        ).first()

        # 1. Password Verification Check
        if user and check_password_hash(user.password_hash, password):
            
            # 2. Deactivation Check (EXACT STEP 2 UPDATE)
            if getattr(user, 'is_deactivated', False):
                session['deactivated_user_id'] = user.id
                return render_template('reactivate_account.html', username=user.username, deactivated_at=user.deactivated_at)

            # Normal Login
            login_user(user)
            # session['first_time_login'] = True
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password!', 'danger')

    return render_template('login.html')

# 2. SIGNUP ROUTE (With Try-Except Safety)
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            flash('Please fill in all fields!', 'warning')
            return redirect(url_for('signup'))

        # MINIMUM 6 CHARACTERS VALIDATION
        if len(password) < 6:
            flash('Password must be at least 6 characters long!', 'danger')
            return redirect(url_for('signup'))

        # Check existing user
        existing_user = User.query.filter(func.lower(User.email) == email).first()
        if existing_user:
            flash('Email already registered! Please login directly.', 'info')
            return redirect(url_for('login'))

        try:
            # Generate unique username from email
            base_username = email.split('@')[0]
            extracted_username = base_username
            while User.query.filter(func.lower(User.username) == extracted_username.lower()).first():
                extracted_username = f"{base_username}_{secrets.token_hex(2)}"

            new_user = User(username=extracted_username, email=email)
            new_user.password_hash = generate_password_hash(password)
            
            db.session.add(new_user)
            db.session.commit()

            flash('Account created successfully! Please login.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback() # 👈 Database lock/error se bachata hai!
            flash('Something went wrong during signup. Please try again.', 'danger')
            return redirect(url_for('signup'))

    return render_template('signup.html')

# 1. FORGOT PASSWORD ROUTE (Sends Link to Gmail)
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter(func.lower(User.email) == email).first()

        if user:
            # Secure token generate karo (30 min expiry)
            token = serializer.dumps(email, salt='reset-password-token')
            reset_url = url_for('reset_password', token=token, _external=True)

            # Gmail Send Logic
            msg = Message('Password Reset Link - Medico',
                        sender=app.config['MAIL_USERNAME'],
                        recipients=[email])
            
            msg.body = f'''Hello,

Click on the following link to reset your password for Medico:
{reset_url}

If you did not request this, simply ignore this email.
Link expires in 30 minutes.
'''
            mail.send(msg)
            flash('Reset link sent! Please check your Gmail inbox.', 'info')
        else:
            flash('Email address not found in system!', 'danger')

        return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html')


# 2. RESET PASSWORD ROUTE (Opens only from Email Link)3
@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        # Token verify karo (Expires in 1800 sec = 30 min)
        email = serializer.loads(token, salt='reset-password-token', max_age=1800)
    except:
        flash('The reset link is invalid or has expired!', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('password')

        # MINIMUM 6 CHARACTERS VALIDATION
        if len(new_password) < 6:
            flash('Password must be at least 6 characters long!', 'danger')
            return redirect(url_for('reset_password', token=token))
        
        user = User.query.filter(func.lower(User.email) == email).first()

        if user:
            user.password_hash = generate_password_hash(new_password)
            # user.set_password(new_password)
            db.session.commit()
            flash('Password updated successfully! Please login.', 'success')
            return redirect(url_for('login'))

    return render_template('reset_password.html', email=email)

# 1. GOOGLE LOGIN INITIATE
@app.route('/login/google')
def google_login():
    redirect_uri = url_for('google_authorize', _external=True)
    return google.authorize_redirect(redirect_uri)


# 2. GOOGLE CALLBACK / AUTHORIZE (Auto-Signup & Auto-Login)
@app.route('/authorize/google')
def google_authorize():
    # 1. Access Token fetch karo
    token = google.authorize_access_token()
    
    # 2. OpenID UserInfo fetch karo (using token)
    user_info = google.userinfo(token=token)

    email = user_info['email'].strip().lower()

    # 3. Check karo agar user DB me exist karta hai
    user = User.query.filter(func.lower(User.email) == email).first()

    if not user:
        # User exist nahi karta to Auto-Create account
        username = email.split('@')[0]
        if User.query.filter_by(username=username).first():
            username = f"{username}_{secrets.token_hex(2)}"

        random_password = secrets.token_hex(16)
        
        user = User(
            username=username,
            email=email
        )
        # user.set_password(random_password)
        # Replace user.set_password line with:
        user.password_hash = generate_password_hash(random_password)

        db.session.add(user)
        db.session.commit()
        flash('Account created successfully via Google!', 'success')

    # 4. Instant Login & Dashboard Redirect
    login_user(user)
    flash(f'Welcome, {user.username}!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))

@app.route('/api/send-email-otp', methods=['POST'])
@login_required
def send_email_otp():
    data = request.get_json() or {}
    new_email = data.get('new_email', '').strip().lower()

    if not new_email or new_email == current_user.email.lower():
        return {'status': 'error', 'message': 'Please enter a different new email address.'}, 400

    # Generate 6-Digit OTP
    otp = str(random.randint(100000, 999999))
    session['email_change_otp'] = otp
    session['pending_new_email'] = new_email

    try:
        msg = Message(
            subject="Medico Account - Email Verification OTP",
            sender=app.config['MAIL_USERNAME'],
            recipients=[new_email]
        )
        msg.body = f"Hello {current_user.username},\n\nYour OTP to verify and update your new email address on Medico is: {otp}\n\nIf you did not request this, please ignore."
        mail.send(msg)
        return {'status': 'success', 'message': f'OTP sent successfully to {new_email}!'}
    except Exception as e:
        return {'status': 'error', 'message': f'Failed to send OTP email: {str(e)}'}, 500


@app.route('/api/verify-email-otp', methods=['POST'])
@login_required
def verify_email_otp():
    data = request.get_json() or {}
    entered_otp = data.get('otp', '').strip()

    saved_otp = session.get('email_change_otp')
    pending_email = session.get('pending_new_email')

    if not saved_otp or not pending_email:
        return {'status': 'error', 'message': 'No pending OTP verification found. Please request OTP again.'}, 400

    if entered_otp != saved_otp:
        return {'status': 'error', 'message': 'Invalid OTP! Please enter the correct code.'}, 400

    # OTP Sahi hone par hi Database Update hoga
    db_user = User.query.get(current_user.id)
    db_user.email = pending_email
    db.session.commit()

    # Clear session variables
    session.pop('email_change_otp', None)
    session.pop('pending_new_email', None)

    return {'status': 'success', 'message': 'Email address verified & updated successfully in database!'}

@app.route('/download-db-backup')
@login_required
@admin_required
def download_db_backup():
    try:
        # SQLite DB path in instance folder
        db_path = os.path.join(app.instance_path, 'medical_store.db')
        
        # Fallback if named medico.db
        if not os.path.exists(db_path):
            db_path = os.path.join(app.instance_path, 'medico.db')

        if os.path.exists(db_path):
            return send_file(
                db_path,
                as_attachment=True,
                download_name=f"medico_backup_{get_ist_time().strftime('%Y%m%d_%H%M%S')}.db"
            )
        else:
            flash('Database file not found on server!', 'danger')
            return redirect(url_for('settings'))
    except Exception as e:
        flash(f'Backup Error: {str(e)}', 'danger')
        return redirect(url_for('settings'))

# Final Staff Account Creation
@app.route('/create-verified-staff', methods=['POST'])
@login_required
@admin_required
def create_verified_staff():
    name = request.form.get('staff_name', '').strip()
    email = request.form.get('staff_email', '').strip().lower()
    password = request.form.get('staff_password', '').strip()

    if not session.get('staff_email_verified') or session.get('staff_creation_email') != email:
        return jsonify({'status': 'error', 'message': 'Mandatory OTP verification incomplete!'}), 400

    if not password or len(password) < 6:
        return jsonify({'status': 'error', 'message': 'Password must be at least 6 characters!'}), 400

    new_staff = User(
        username=name or email.split('@')[0],
        email=email,
        role='cashier',
        plain_password=password
    )
    new_staff.set_password(password)
    
    db.session.add(new_staff)
    db.session.commit()

    # Clear Session
    session.pop('staff_creation_otp', None)
    session.pop('staff_creation_email', None)
    session.pop('staff_email_verified', None)

    flash(f'Staff account created successfully for {email}!', 'success')
    return jsonify({'status': 'success'})

@app.route('/delete-staff/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_staff(user_id):
    staff_user = User.query.get_or_404(user_id)
    if staff_user.role != 'admin':
        db.session.delete(staff_user)
        db.session.commit()
        flash('Staff account deleted successfully.', 'success')
    else:
        flash('Cannot delete Admin account!', 'danger')
    return redirect(url_for('settings') + '#staff-management')

@app.route('/edit-staff/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def edit_staff(user_id):
    staff_user = User.query.get_or_404(user_id)
    
    if staff_user.role == 'admin':
        return jsonify({'status': 'error', 'message': 'Cannot edit admin from here'}), 400

    name = request.form.get('edit_name', '').strip()
    email = request.form.get('edit_email', '').strip().lower()
    old_password = request.form.get('old_password', '').strip()
    new_password = request.form.get('new_password', '').strip()

    # Email uniqueness check
    if email != staff_user.email:
        existing_email = User.query.filter(User.email == email, User.id != user_id).first()
        if existing_email:
            return jsonify({'status': 'error', 'message': 'Email already in use by another user'}), 400
        staff_user.email = email

    if name:
        staff_user.username = name

    # Password Change Logic (Requires Current/Old Password Verification)
    if new_password:
        if not old_password:
            return jsonify({'status': 'error', 'message': 'Please enter current password to update password'}), 400

        if len(new_password) < 6:
            return jsonify({'status': 'error', 'message': 'New password must be at least 6 characters long!'}), 400
        
        if not staff_user.check_password(old_password):
            return jsonify({'status': 'error', 'message': 'Incorrect current password'}), 400
        
        staff_user.set_password(new_password)
        staff_user.plain_password = new_password # Update plain password

    db.session.commit()
    flash(f'Staff details for "{staff_user.username}" updated successfully!', 'success')
    return jsonify({'status': 'success'})

# Send Staff OTP (Dynamic Sender from Config/Current User)
@app.route('/send-staff-otp', methods=['POST'])
@login_required
@admin_required
def send_staff_otp():
    email = request.form.get('staff_email', '').strip().lower()
    
    if not email:
        return jsonify({'status': 'error', 'message': 'Email address is required!'}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        return jsonify({'status': 'error', 'message': 'User with this email already exists!'}), 400

    otp = str(random.randint(100000, 999999))
    session['staff_creation_otp'] = str(otp)
    session['staff_creation_email'] = email

    try:
        from flask_mail import Message
        
        # Dynamic sender: Pick logged-in user email or Flask-Mail App Config username
        sender_email = app.config.get('MAIL_USERNAME') or current_user.email
        
        msg = Message(
            subject="Staff Registration OTP Verification",
            sender=sender_email,
            recipients=[email],
            body=f"Your OTP for staff account registration is: {otp}. Valid for 10 minutes."
        )
        mail.send(msg)

        return jsonify({'status': 'success', 'message': f'OTP sent successfully to {email}'})
    except Exception as e:
        print("Mail Sending Exception:", str(e))
        return jsonify({'status': 'error', 'message': f'Failed to send OTP: {str(e)}'}), 500

# 2. Strict Server-Side OTP Verification Endpoint
@app.route('/verify-staff-otp', methods=['POST'])
@login_required
@admin_required
def verify_staff_otp():
    email = request.form.get('staff_email', '').strip().lower()
    user_otp = request.form.get('staff_otp', '').strip()

    session_otp = session.get('staff_creation_otp')
    session_email = session.get('staff_creation_email')

    if not session_otp or not session_email or session_email != email:
        return jsonify({'status': 'error', 'message': 'OTP session expired. Please request a new OTP.'}), 400

    if str(session_otp) != str(user_otp):
        return jsonify({'status': 'error', 'message': 'Invalid OTP! Verification failed.'}), 400

    # Mark as verified in session
    session['staff_email_verified'] = True
    return jsonify({'status': 'success', 'message': 'OTP Verified Successfully!'})

@app.route('/get-staff-permissions/<int:user_id>', methods=['GET'])
@login_required
@admin_required
def get_staff_permissions(user_id):
    staff = User.query.get_or_404(user_id)
    return jsonify({'status': 'success', 'permissions': staff.get_permissions()})

@app.route('/update-staff-permissions/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def update_staff_permissions(user_id):
    staff = User.query.get_or_404(user_id)
    if staff.role == 'admin':
        return jsonify({'status': 'error', 'message': 'Cannot modify Admin permissions'}), 400

    perms_data = request.json
    staff.permissions = json.dumps(perms_data)
    
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(staff, "permissions")

    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Permissions updated successfully!'})

@app.route('/delete-store-doc', methods=['POST'])
@login_required
def delete_store_doc():
    if current_user.role != 'admin':
        flash('ACCESS_RESTRICTED', 'access_denied_popup')
        return redirect(url_for('settings'))

    doc_type = request.form.get('doc_type')
    store_config = get_settings()

    target_attr = None
    if doc_type == 'store_logo':
        target_attr = 'logo_path'
    elif doc_type == 'owner_doc':
        target_attr = 'owner_doc_path'
    elif doc_type == 'legal_doc':
        target_attr = 'legal_doc_path'

    if target_attr and getattr(store_config, target_attr, None):
        relative_path = getattr(store_config, target_attr)
        full_path = os.path.join(app.root_path, 'static', relative_path)
        
        # Remove physical file if exists
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except Exception as e:
                pass

        # Clear path in Database
        setattr(store_config, target_attr, None)
        db.session.commit()
        flash('Document deleted successfully!', 'success')
    
    return redirect(url_for('settings'))

@app.route('/distributors')
def distributors():
    if current_user.role != 'admin' and not current_user.has_permission('modules', 'distributors'):
            flash('ACCESS_RESTRICTED', 'access_denied_popup')
            return redirect(request.referrer or url_for('billing'))
    
    all_distributors = Distributor.query.order_by(Distributor.id.desc()).all()
    return render_template('distributors.html', distributors=all_distributors)

@app.route('/distributors/add', methods=['POST'])
def add_distributor():
    name = request.form.get('name')
    phone = request.form.get('phone')
    email = request.form.get('email')
    contact_person = request.form.get('contact_person')
    supplies_category = request.form.get('supplies_category')

    new_dist = Distributor(
        name=name,
        phone=phone,
        email=email,
        contact_person=contact_person,
        supplies_category=supplies_category
    )
    db.session.add(new_dist)
    db.session.commit()
    flash('Distributor added successfully!', 'success')
    return redirect(url_for('distributors'))

@app.route('/distributors/edit/<int:id>', methods=['POST'])
def edit_distributor(id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'edit_distributor'):
                flash('ACCESS_RESTRICTED', 'access_denied_popup')
                return redirect(request.referrer or url_for('distributors'))
    
    dist = Distributor.query.get_or_404(id)
    dist.name = request.form.get('name')
    dist.phone = request.form.get('phone')
    dist.email = request.form.get('email')
    dist.contact_person = request.form.get('contact_person')
    dist.supplies_category = request.form.get('supplies_category')
    
    db.session.commit()
    flash('Distributor details updated!', 'success')
    return redirect(url_for('distributors'))

@app.route('/distributors/delete/<int:id>')
def delete_distributor(id):
    if current_user.role != 'admin' and not current_user.has_permission('actions', 'delete_distributor'):
                flash('ACCESS_RESTRICTED', 'access_denied_popup')
                return redirect(request.referrer or url_for('distributors'))
    
    dist = Distributor.query.get_or_404(id)
    db.session.delete(dist)
    db.session.commit()
    flash('Distributor removed!', 'danger')
    return redirect(url_for('distributors'))

if __name__ == '__main__':
    app.run(debug=True)
