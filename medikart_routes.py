from flask import Blueprint, render_template, request, jsonify , session ,current_app, send_from_directory
from flask_login import login_required, current_user
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import io
import uuid
import json
import base64
import boto3
from botocore.config import Config
from datetime import datetime, timezone, timedelta
from PIL import Image
import json
import random
import resend
import re
import requests

medikart_bp = Blueprint('medikart_bp', __name__)

CACHE_FILE = os.path.join(os.path.dirname(__file__), 'medikart_product_cache.json')

def load_med_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def get_google_maps_distance_km(origin_address, destination_address):
    """
    Calculates exact real-world driving distance in KM using modern Google Routes API (v2).
    """
    api_key = os.getenv('GOOGLE_MAPS_API_KEY')
    print(f"\n[GEOFENCE DEBUG] API_KEY Found: {bool(api_key)}")
    print(f"[GEOFENCE DEBUG] Origin (Store): '{origin_address}'")
    print(f"[GEOFENCE DEBUG] Destination (Customer): '{destination_address}'")

    if not api_key:
        return False, 0.0, "GOOGLE_MAPS_API_KEY is missing."

    # Modern Google Routes API endpoint
    endpoint = "https://routes.googleapis.com/directions/v2:computeRoutes"
    
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration"
    }

    payload = {
        "origin": {
            "address": origin_address
        },
        "destination": {
            "address": destination_address
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE"
    }

    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=7)
        res_json = resp.json()
        print(f"[GEOFENCE DEBUG] Routes API v2 Response: {res_json}")

        if 'error' in res_json:
            err_msg = res_json['error'].get('message', 'Routes calculation failed')
            return False, 0.0, f"Google Maps error: {err_msg}"

        routes = res_json.get('routes', [])
        if not routes:
            return False, 0.0, "No driving route found between store and this address."

        meters = routes[0].get('distanceMeters', 0)
        distance_km = round(meters / 1000.0, 2)
        print(f"[GEOFENCE DEBUG] Exact Driving Distance: {distance_km} KM")

        # 5.0 KM Geofence Rule
        if distance_km <= 5.0:
            return True, distance_km, f"Within zone ({distance_km} km away)"
        else:
            return False, distance_km, f"Location is {distance_km} km away. We only deliver within 5 km."

    except Exception as e:
        print(f"[GEOFENCE DEBUG] Exception: {str(e)}")
        return False, 0.0, f"Distance service unavailable: {str(e)}"

def save_med_cache(cache):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Cache save error:", e)

# IST Timezone (+5:30) Helper (Same as main.py)
def get_ist_time():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).replace(tzinfo=None)

def get_db():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

# R2 Client Configuration
def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f"https://{os.environ.get('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )

R2_BUCKET = os.getenv('R2_BUCKET_NAME', 'medicofiles-bills')
R2_PUBLIC_DOMAIN = os.getenv('R2_PUBLIC_URL', '').rstrip('/') # e.g. https://pub-xxx.r2.dev

def upload_rx_to_r2(base64_data):
    if not base64_data:
        return None
    try:
        # Separate base64 header from raw binary
        if ',' in base64_data:
            header, encoded = base64_data.split(',', 1)
        else:
            encoded = base64_data
            
        file_bytes = base64.b64decode(encoded)
        filename = f"prescriptions/rx_{uuid.uuid4().hex[:10]}.jpg"
        
        r2 = get_r2_client()
        bucket_name = os.environ.get('R2_BUCKET_NAME', 'medicofiles-bills')
        
        r2.put_object(
            Bucket=bucket_name,
            Key=filename,
            Body=file_bytes,
            ContentType='image/jpeg'
        )
        
        public_url_base = os.environ.get('R2_PUBLIC_URL', '').rstrip('/')
        if public_url_base:
            return f"{public_url_base}/{filename}"
        return filename
    except Exception as e:
        print("R2 Upload Error:", e)
        return None

def delete_rx_from_r2(prescription_url):
    """Cloudflare R2 se prescription image permanently delete karta hai"""
    if not prescription_url:
        return
    try:
        # URL ya path se clean key extract karein
        # Example: 'https://pub-xxx.r2.dev/prescriptions/rx_123.jpg' -> 'prescriptions/rx_123.jpg'
        if 'prescriptions/' in prescription_url:
            object_key = 'prescriptions/' + prescription_url.split('prescriptions/')[-1]
        else:
            filename = prescription_url.split('/')[-1]
            object_key = f"prescriptions/{filename}"
            
        r2 = get_r2_client()
        bucket_name = os.environ.get('R2_BUCKET_NAME', 'medicofiles-bills')
        
        r2.delete_object(
            Bucket=bucket_name,
            Key=object_key
        )
        print(f"[R2 SUCCESS] Prescription deleted: {object_key}")
    except Exception as e:
        print(f"[R2 ERROR] Prescription deletion failed: {e}")

@medikart_bp.route('/.well-known/assetlinks.json')
def medikart_assetlinks():
    well_known_dir = os.path.join(current_app.static_folder, '.well-known')
    return send_from_directory(well_known_dir, 'assetlinks.json', mimetype='application/json')

# Medikart Public Showcase & Welcome Page
@medikart_bp.route('/medikart/about')
def medikart_about():
    return render_template('medikart_about.html')

# Chemist Medikart Control Center
@medikart_bp.route('/medicofiles/medikart')
@login_required
def chemist_medikart_hub():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 1. Fetch Catalog (ONLY items actively listed on this store's live shelf)
    cur.execute("""
        SELECT m.*,
            s.is_available,
            COALESCE(s.selling_price, m.mrp) as active_price
        FROM medikart_store_inventory s
        JOIN medikart_master_catalog m
            ON s.master_item_id = m.id
        WHERE s.store_id = %s AND s.is_available = true
        ORDER BY s.id DESC
    """, (current_user.id,))
    catalog = cur.fetchall()
    
    # 2. Fetch Active Orders for this store
    cur.execute("""
        SELECT * FROM medikart_orders 
        WHERE store_id = %s 
        ORDER BY created_at DESC
    """, (current_user.id,))
    orders = cur.fetchall()
    
    pending_count = sum(1 for o in orders if o['order_status'] == 'PENDING')
    
    cur.close()
    conn.close()
    return render_template('medikart_hub.html', catalog=catalog, orders=orders, pending_orders=pending_count)

# Live Polling API (for sound & new orders check every 4 seconds)
@medikart_bp.route('/medicofiles/medikart/api/orders-poll')
@login_required
def poll_orders():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT * FROM medikart_orders 
        WHERE store_id = %s 
        ORDER BY created_at DESC LIMIT 15
    """, (current_user.id,))
    orders = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({"orders": orders})

# Order Status Change API (Accept, Pack, Ready for Dunzo)
@medikart_bp.route('/medicofiles/medikart/api/update-order-status', methods=['POST'])
@login_required
def update_order_status():
    data = request.json
    order_id = data.get('order_id')
    new_status = data.get('status')
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE medikart_orders 
        SET order_status = %s 
        WHERE id = %s AND store_id = %s
    """, (new_status, order_id, current_user.id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})

@medikart_bp.route('/medicofiles/medikart/toggle-item', methods=['POST'])
@login_required
def toggle_store_item():
    data = request.get_json() or {}
    master_id = data.get('master_id')
    is_avail = data.get('is_available', True)
    custom_price = data.get('price')

    if not master_id:
        return jsonify({'success': False, 'message': 'Missing master_id'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Agar custom price nahi di, toh master catalog se direct MRP uthao
        if custom_price is None or custom_price == "":
            cur.execute("SELECT mrp FROM medikart_master_catalog WHERE id = %s", (master_id,))
            item = cur.fetchone()
            selling_price = float(item['mrp']) if item and item['mrp'] is not None else 0.0
        else:
            selling_price = float(custom_price)

        # Upsert into medikart_store_inventory with non-null selling_price
        cur.execute("""
            INSERT INTO medikart_store_inventory (store_id, master_item_id, selling_price, is_available, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, master_item_id) DO UPDATE SET
                is_available = EXCLUDED.is_available,
                selling_price = COALESCE(EXCLUDED.selling_price, medikart_store_inventory.selling_price),
                updated_at = NOW();
        """, (current_user.id, master_id, selling_price, is_avail))

        conn.commit()
        return jsonify({'success': True, 'message': 'Inventory updated successfully!'})
    except Exception as e:
        conn.rollback()
        print(f"[TOGGLE ITEM ERROR] {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@medikart_bp.route('/medicofiles/medikart/update-price', methods=['POST'])
@login_required
def update_store_price():
    data = request.get_json() or {}
    master_id = data.get('master_id')
    new_price = data.get('price')

    if not master_id or new_price is None:
        return jsonify({'success': False, 'message': 'Invalid parameters'}), 400

    try:
        price_val = float(new_price)
    except ValueError:
        return jsonify({'success': False, 'message': 'Price must be a valid number'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. Update chemist's active store inventory price
        cur.execute("""
            UPDATE medikart_store_inventory
            SET selling_price = %s, updated_at = NOW()
            WHERE store_id = %s AND master_item_id = %s;
        """, (price_val, current_user.id, master_id))

        # 2. Also keep master catalog aligned
        cur.execute("""
            UPDATE medikart_master_catalog
            SET default_selling_price = %s, mrp = %s
            WHERE id = %s;
        """, (price_val, price_val, master_id))

        conn.commit()
        return jsonify({'success': True, 'message': 'Price successfully updated!'})
    except Exception as e:
        conn.rollback()
        print(f"[UPDATE PRICE ERROR] {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# 1. Customer Storefront Route (No login required - for public)
@medikart_bp.route('/medikart')
def public_medikart_store():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Store 1 (Default / Your Store) ke live items fetch honge
    # current active stores ya first registered chemist store
    cur.execute("""
        SELECT s.id as inventory_id, s.selling_price, s.store_id,
                COALESCE(s.is_procurement_item, FALSE) as is_procurement_item,
                m.id as master_id, m.name, m.brand, m.category,
                m.dosage_form, m.mrp, m.requires_rx, COALESCE(m.is_strictly_rx, FALSE) as is_strictly_rx, m.image_url
        FROM medikart_store_inventory s
        JOIN medikart_master_catalog m ON s.master_item_id = m.id
        WHERE s.is_available = TRUE
        ORDER BY m.category, m.name
    """)
    live_items = cur.fetchall()
    
    # Store details (for banner)
    cur.execute('SELECT id, username, email FROM "user" LIMIT 5')
    available_stores = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        'medikart_store.html',
        items=live_items,
        stores=available_stores
    )

# Updated Order Placement Route (Saves URL in Postgres instead of heavy base64)
@medikart_bp.route('/medikart/api/place-order', methods=['POST'])
def place_order():
    data = request.json or {}
    customer_name = data.get('name')
    customer_phone = data.get('phone')
    delivery_address = data.get('address')
    cart_items = data.get('cart', [])

    # Dynamic Store Mapping: Cart item jis store ka hai, order usi ke Hub me jayega
    store_id = None
    if cart_items and len(cart_items) > 0:
        store_id = cart_items[0].get('store_id')

    if not store_id:
        store_id = data.get('store_id', 4)
    total_amount = data.get('total')
    raw_rx = data.get('prescription_data')
    utr_number = str(data.get('utr_number', '')).strip()

    # 1. Mandatory Validations
    if not customer_name or not customer_phone or not delivery_address:
        return jsonify({'success': False, 'message': 'Customer details are incomplete'}), 400

    if not cart_items:
        return jsonify({'success': False, 'message': 'Cart is empty'}), 400

    # 2. Strict 12-Digit Numeric UTR Check
    if not utr_number or len(utr_number) != 12 or not utr_number.isdigit():
        return jsonify({'success': False, 'message': 'A valid 12-digit numeric UPI UTR/Ref number is mandatory'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # === REAL-TIME GOOGLE MAPS 5 KM GEOFENCE CHECK ===
    cur.execute("""
        SELECT address, city, pincode 
        FROM store_settings 
        WHERE user_id = %s OR id = %s
        LIMIT 1;
    """, (store_id, store_id))
    store_meta = cur.fetchone()

    store_full_addr = ""
    if store_meta:
        addr = store_meta.get('address') if isinstance(store_meta, dict) else store_meta[0]
        city = store_meta.get('city') if isinstance(store_meta, dict) else store_meta[1]
        pin = store_meta.get('pincode') if isinstance(store_meta, dict) else store_meta[2]
        store_full_addr = f"{addr or ''}, {city or ''} {pin or ''}".strip(", ")

    # Agar store_settings me address empty ho toh fallback to physical store location
    if not store_full_addr or len(store_full_addr) < 5:
        store_full_addr = "Basai village, Sector 99, Gurugram, Haryana 122001"

    is_in_zone, dist_km, zone_msg = get_google_maps_distance_km(
        origin_address=store_full_addr,
        destination_address=delivery_address
    )

    if not is_in_zone:
        cur.close()
        conn.close()
        return jsonify({
            'success': False,
            'message': f"🚫 Delivery Blocked: {zone_msg}"
        }), 400

    try:
        # 3. Anti-Fraud Lock: Check for duplicate UTR usage
        cur.execute("SELECT id FROM medikart_orders WHERE utr_number = %s LIMIT 1;", (utr_number,))
        duplicate_check = cur.fetchone()
        if duplicate_check:
            return jsonify({'success': False, 'message': 'This UTR Number has already been submitted for another order.'}), 400

        # Prescription upload to Cloudflare R2 if present
        rx_cloud_url = upload_rx_to_r2(raw_rx) if raw_rx else None
        order_num = "MK-" + uuid.uuid4().hex[:6].upper()
        current_time_ist = get_ist_time()
        cust_id = session.get('medikart_customer_id')

        # 4. Insert order with payment status
        cur.execute("""
            INSERT INTO medikart_orders
            (order_number, store_id, customer_id, customer_name, customer_phone, 
                delivery_address, total_amount, order_status, items_json, 
                prescription_url, utr_number, payment_status, payment_method, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, 'PENDING_VERIFICATION', 'UPI', %s)
            RETURNING order_number;
        """, (order_num, store_id, cust_id, customer_name, customer_phone, 
                delivery_address, total_amount, json.dumps(cart_items), 
                rx_cloud_url, utr_number, current_time_ist))

        conn.commit()
        return jsonify({'success': True, 'order_number': order_num})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# Delete Completed/Old Order Route (R2 + Database Dono se Clean)
@medikart_bp.route('/medicofiles/medikart/api/delete-order', methods=['POST'])
@login_required
def delete_order():
    data = request.json
    order_id = data.get('order_id')
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 1. Pehle prescription URL nikalte hain
    cur.execute("SELECT prescription_url FROM medikart_orders WHERE id = %s AND store_id = %s", (order_id, current_user.id))
    order = cur.fetchone()
    
    if order and order.get('prescription_url'):
        # 2. Cloudflare R2 bucket se delete karo
        delete_rx_from_r2(order['prescription_url'])
        
    # 3. Ab Supabase database se order record delete karo
    cur.execute("DELETE FROM medikart_orders WHERE id = %s AND store_id = %s", (order_id, current_user.id))
    conn.commit()
    
    cur.close()
    conn.close()
    
    return jsonify({"success": True})

# 1. Product Image Upload with Auto-Resize (Square 500x500 fit)
@medikart_bp.route('/medikart/api/upload-product-image', methods=['POST'])
def upload_product_image():
    if 'image' not in request.files or 'master_id' not in request.form:
        return jsonify({'success': False, 'error': 'Image or Product ID missing'}), 400

    file = request.files['image']
    master_id = request.form['master_id']

    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    try:
        # --- Automatic Aspect Ratio & Resize Handling (Pillow) ---
        img = Image.open(file.stream)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGBA")
            bg_color = (255, 255, 255, 0)
        else:
            img = img.convert("RGB")
            bg_color = (255, 255, 255)

        # Strip/bottle clarity ke liye 500x500 tight bounding
        img.thumbnail((500, 500), Image.Resampling.LANCZOS)
        
        # Agar horizontal medicine strip ho toh auto-trim white boundaries
        canvas = Image.new(img.mode, (500, 500), bg_color)
        offset = ((500 - img.size[0]) // 2, (500 - img.size[1]) // 2)
        canvas.paste(img, offset)

        buffer = io.BytesIO()
        if canvas.mode == "RGBA":
            canvas.save(buffer, format="PNG", optimize=True)
            content_type = "image/png"
            ext = "png"
        else:
            canvas.save(buffer, format="JPEG", quality=85, optimize=True)
            content_type = "image/jpeg"
            ext = "jpg"
        buffer.seek(0)

        # Bucket name & public url base check
        bucket = os.environ.get('R2_BUCKET_NAME', 'medicofiles-bills')
        public_base = (os.environ.get('R2_PUBLIC_URL') or os.environ.get('R2_PUBLIC_DOMAIN') or '').rstrip('/')

        file_key = f"products/{master_id}_{uuid.uuid4().hex[:8]}.{ext}"

        r2 = get_r2_client()
        r2.put_object(
            Bucket=bucket,
            Key=file_key,
            Body=buffer.getvalue(),
            ContentType=content_type
        )

        # Agar public URL configure nahi hai to relative fallback handle karein
        image_url = f"{public_base}/{file_key}" if public_base else f"/{file_key}"

        # Supabase Master Catalog Update
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE medikart_master_catalog SET image_url = %s WHERE id = %s;", (image_url, master_id))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'success': True, 'image_url': image_url})

    except Exception as e:
        print("Upload error:", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# 2. Product Image Delete from R2 & Supabase
@medikart_bp.route('/medikart/api/delete-product-image', methods=['POST'])
def delete_product_image():
    data = request.get_json() or {}
    master_id = data.get('master_id')

    if not master_id:
        return jsonify({'success': False, 'error': 'Master ID required'}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT image_url FROM medikart_master_catalog WHERE id = %s;", (master_id,))
        row = cur.fetchone()

        if row and row[0]:
            old_url = row[0]
            # Key extract logic: chahe full url ho ya path, 'products/...' extract hoga
            if 'products/' in old_url:
                file_key = 'products/' + old_url.split('products/')[-1]
            else:
                file_key = f"products/{old_url.split('/')[-1]}"

            bucket = os.environ.get('R2_BUCKET_NAME', 'medicofiles-bills')
            try:
                r2 = get_r2_client()
                r2.delete_object(Bucket=bucket, Key=file_key)
                print(f"[R2 SUCCESS] Deleted key: {file_key} from {bucket}")
            except Exception as del_err:
                print("[R2 DELETE WARNING]:", del_err)

            # DB se URL remove karein
            cur.execute("UPDATE medikart_master_catalog SET image_url = NULL WHERE id = %s;", (master_id,))
            conn.commit()

        cur.close()
        conn.close()
        return jsonify({'success': True})

    except Exception as e:
        print("Delete error:", e)
        return jsonify({'success': False, 'error': str(e)}), 500

@medikart_bp.route('/medikart/api/product-details/<int:master_id>', methods=['GET'])
def get_medikart_product_details(master_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT m.id, m.name, m.brand, m.category, m.dosage_form, m.mrp,
                m.requires_rx, COALESCE(m.is_strictly_rx, FALSE) as is_strictly_rx,
                m.image_url, s.selling_price
        FROM medikart_master_catalog m
        LEFT JOIN medikart_store_inventory s ON s.master_item_id = m.id
        WHERE m.id = %s
        LIMIT 1;
    """, (master_id,))
    item = cur.fetchone()
    cur.close()
    conn.close()

    if not item:
        return jsonify({'success': False, 'error': 'Product not found'}), 404

    # 1. Check medicine_cache.json
    cache = load_med_cache()
    clean_key = item['name'].strip().lower()
    clinical_info = cache.get(clean_key)

    # 2. Agar cache me nahi hai ya composition missing hai toh Gemini call karein
    if not clinical_info or not clinical_info.get('composition'):
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=api_key)

                prompt = f"""
                You are an expert Indian Pharmacy & Clinical Drug Specialist.
                Analyze this pharmaceutical or OTC health product: '{item['name']}' (Category: {item.get('category', '')}).

                Return STRICTLY a valid JSON object with these exact keys:
                1. "composition": Exact chemical composition / active salt with strength. (e.g. 'Paracetamol (650mg)', 'Cetirizine Hydrochloride (10mg)', 'Pantoprazole (40mg) + Domperidone (30mg)'. For first-aid/skincare, state active ingredients e.g. 'Benzalkonium Chloride + Antiseptic Non-stick Pad' for bandages, 'Neem extract + Turmeric' for facewashes). Never leave empty or generic.
                2. "uses": 1 concise crisp paragraph explaining therapeutic indications and benefits.
                3. "side_effects": Common side effects or 'No common side effects for topical/first-aid use.'
                4. "safety_advice": Key precautions regarding usage, alcohol, pregnancy, or application.
                5. "storage": Storage conditions (e.g. 'Store below 25°C in a dry place away from direct light.').

                Respond ONLY in raw JSON. Do NOT wrap in ```json markdown.
                """

                # Main.py fallback models loop
                model_names = ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite' , 'gemini-flash']
                response = None

                for m_name in model_names:
                    try:
                        resp = client.models.generate_content(
                            model=m_name,
                            contents=prompt,
                            config=types.GenerateContentConfig(response_mime_type="application/json")
                        )
                        if resp and resp.text:
                            response = resp
                            break
                    except Exception as model_err:
                        print(f"[Gemini Model {m_name} failed]:", model_err)
                        continue

                if response and response.text:
                    raw_txt = response.text.strip()
                    if raw_txt.startswith("```json"):
                        raw_txt = raw_txt[7:]
                    if raw_txt.endswith("```"):
                        raw_txt = raw_txt[:-3]

                    clinical_info = json.loads(raw_txt.strip())
                    
                    # Cache me save karein permanent reuse ke liye
                    cache[clean_key] = clinical_info
                    save_med_cache(cache)
                    print(f"[CACHE STORED] Generated clinical info for: {item['name']}")

            except Exception as e:
                print(f"[Gemini Clinical Error for {item['name']}]:", e)

    # 3. Safe Dynamic Fallback (Agar API quota over ya network drop ho)
    if not clinical_info:
        clinical_info = {
            "composition": f"Standard Formulation ({item.get('dosage_form', 'Formulation')})",
            "uses": f"Prescribed and recommended for symptom relief and targeted health management.",
            "side_effects": "Generally well tolerated when used according to instructions.",
            "safety_advice": "Consult your physician or read pack instructions before use.",
            "storage": "Store in a cool, dry place away from direct sunlight."
        }

    return jsonify({
        'success': True,
        'product': dict(item),
        'clinical': clinical_info
    })

@medikart_bp.route('/medicofiles/medikart/search-master')
@login_required
def search_master_catalog():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 2.5 Lakh rows me se instant trigram search (limit 15 results for 0 latency)
    cur.execute("""
        SELECT id, name, brand, category, dosage_form, mrp, requires_rx, is_strictly_rx, composition, pack_size
        FROM medikart_master_catalog
        WHERE name ILIKE %s
        ORDER BY LENGTH(name) ASC
        LIMIT 15;
    """, (f"%{q}%",))
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(results)

@medikart_bp.route('/medicofiles/medikart/toggle-rx-status', methods=['POST'])
@login_required
def toggle_rx_status():
    data = request.get_json() or {}
    master_id = data.get('master_id')
    
    if not master_id:
        return jsonify({'success': False, 'message': 'Missing master_id'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Fetch current state
        cur.execute("SELECT requires_rx, is_strictly_rx FROM medikart_master_catalog WHERE id = %s", (master_id,))
        item = cur.fetchone()
        if not item:
            return jsonify({'success': False, 'message': 'Item not found'}), 404

        # Cycle states: Strict Rx (True, True) -> Soft Rx (True, False) -> OTC (False, False) -> Strict Rx...
        req_rx = item['requires_rx']
        strict_rx = item['is_strictly_rx']

        if req_rx and strict_rx:
            # Downgrade to Soft Rx
            next_req, next_strict = True, False
            label = "Rx Required"
        elif req_rx and not strict_rx:
            # Downgrade to OTC
            next_req, next_strict = False, False
            label = "OTC (No Rx)"
        else:
            # Upgrade back to Strict
            next_req, next_strict = True, True
            label = "Rx Mandatory ⚠️"

        cur.execute("""
            UPDATE medikart_master_catalog
            SET requires_rx = %s, is_strictly_rx = %s
            WHERE id = %s;
        """, (next_req, next_strict, master_id))

        conn.commit()
        return jsonify({
            'success': True,
            'requires_rx': next_req,
            'is_strictly_rx': next_strict,
            'label': label
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@medikart_bp.route('/medicofiles/medikart/add-custom-medicine', methods=['POST'])
@login_required
def add_custom_medicine():
    name = request.form.get('name', '').strip()
    brand = request.form.get('brand', '').strip() or 'Generic / Local'
    category = request.form.get('category', 'General Health').strip()
    dosage_form = request.form.get('dosage_form', 'allopathy').strip()
    mrp_str = request.form.get('mrp', '0').strip()
    composition = request.form.get('composition', '').strip() or name
    pack_size = request.form.get('pack_size', '').strip() or '1 Unit'
    rx_type = request.form.get('rx_type', 'strict') # otc | rx | strict

    if not name:
        return jsonify({'success': False, 'message': 'Medicine name is required'}), 400

    try:
        mrp = float(mrp_str)
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid MRP value'}), 400

    # Rx boolean configuration
    if rx_type == 'rx':
        req_rx = True
        strict_rx = False
    elif rx_type == 'otc':
        req_rx = False
        strict_rx = False
    else:
        # Default: Always Rx Mandatory
        req_rx = True
        strict_rx = True

    # Handle Photo Upload using project's verified get_r2_client()
    image_url = None
    photo_file = request.files.get('medicine_photo')
    if photo_file and photo_file.filename != '':
        try:
            r2 = get_r2_client()
            bucket = os.environ.get('R2_BUCKET_NAME', 'medicofiles-bills')
            public_base = (os.environ.get('R2_PUBLIC_URL') or os.environ.get('R2_PUBLIC_DOMAIN') or '').rstrip('/')

            ext = photo_file.filename.rsplit('.', 1)[-1].lower() if '.' in photo_file.filename else 'jpg'
            file_key = f"products/custom_{uuid.uuid4().hex[:10]}.{ext}"

            file_bytes = photo_file.read()
            content_type = photo_file.content_type or 'image/jpeg'

            r2.put_object(
                Bucket=bucket,
                Key=file_key,
                Body=file_bytes,
                ContentType=content_type
            )

            image_url = f"{public_base}/{file_key}" if public_base else f"/{file_key}"
            print(f"[R2 SUCCESS] Custom product image saved: {image_url}")
        except Exception as img_err:
            print(f"[R2 UPLOAD ERROR] {img_err}")
            image_url = None

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. Insert into medikart_master_catalog
        cur.execute("""
            INSERT INTO medikart_master_catalog (
                name, brand, category, dosage_form, mrp, default_selling_price,
                requires_rx, is_strictly_rx, composition, pack_size, image_url, is_discontinued
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)
            ON CONFLICT (name) DO UPDATE SET
                mrp = EXCLUDED.mrp,
                default_selling_price = EXCLUDED.default_selling_price,
                image_url = COALESCE(EXCLUDED.image_url, medikart_master_catalog.image_url)
            RETURNING id;
        """, (
            name, brand, category, dosage_form, mrp, mrp,
            req_rx, strict_rx, composition, pack_size, image_url
        ))
        
        master_row = cur.fetchone()
        master_id = master_row['id']

        # 2. Add directly to chemist's live shelf (medikart_store_inventory)
        cur.execute("""
            INSERT INTO medikart_store_inventory (store_id, master_item_id, selling_price, is_available, updated_at)
            VALUES (%s, %s, %s, true, NOW())
            ON CONFLICT (store_id, master_item_id) DO UPDATE SET
                selling_price = EXCLUDED.selling_price,
                is_available = true,
                updated_at = NOW();
        """, (current_user.id, master_id, mrp))

        conn.commit()
        return jsonify({'success': True, 'message': 'Medicine successfully created & added live to shelf!'})
    except Exception as e:
        conn.rollback()
        print(f"[CUSTOM MED ERROR] {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@medikart_bp.route('/medicofiles/medikart/delete-shelf-item', methods=['POST'])
@login_required
def delete_shelf_item():
    data = request.get_json() or {}
    master_id = data.get('master_id')

    if not master_id:
        return jsonify({'success': False, 'message': 'Missing master_id'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. Check if an image exists for this master item
        cur.execute("SELECT image_url FROM medikart_master_catalog WHERE id = %s", (master_id,))
        med_row = cur.fetchone()

        if med_row and med_row.get('image_url'):
            img_url = med_row['image_url']
            
            # Extract file key from URL (e.g., products/custom_xxxx.png)
            if 'products/' in img_url:
                file_key = 'products/' + img_url.split('products/')[-1]
                
                # Cloudflare R2 se delete karein
                try:
                    r2 = get_r2_client()
                    bucket = os.environ.get('R2_BUCKET_NAME', 'medicofiles-bills')
                    r2.delete_object(Bucket=bucket, Key=file_key)
                    print(f"[R2 CLEANUP] Successfully deleted {file_key} from R2 bucket.")
                except Exception as r2_err:
                    print(f"[R2 CLEANUP ERROR] {r2_err}")

            # Catalog me image_url ko wapas NULL karein
            cur.execute("UPDATE medikart_master_catalog SET image_url = NULL WHERE id = %s", (master_id,))

        # 2. Chemist ki active shelf se remove karein
        cur.execute("""
            DELETE FROM medikart_store_inventory 
            WHERE store_id = %s AND master_item_id = %s;
        """, (current_user.id, master_id))

        conn.commit()
        return jsonify({'success': True, 'message': 'Item and associated image deleted from shelf.'})

    except Exception as e:
        conn.rollback()
        print(f"[DELETE ERROR] {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# 1. SEND EMAIL OTP (Pure Resend Route)
@medikart_bp.route('/medikart/auth/send-otp', methods=['POST'])
def medikart_send_otp():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()

    if not email or '@' not in email:
        return jsonify({'success': False, 'message': 'Please enter a valid email address'}), 400

    otp = f"{random.randint(1000, 9999)}"
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Purani expired/used entries clean karein (table bloat prevention)
        cur.execute("""
            DELETE FROM medikart_otp_verifications 
            WHERE expires_at < NOW() OR is_used = TRUE;
        """)

        # Fresh OTP insert
        cur.execute("""
            INSERT INTO medikart_otp_verifications (identifier, otp_code, expires_at)
            VALUES (%s, %s, %s);
        """, (email, otp, expires_at))

        conn.commit()

        # Branded Medikart Green Email via Resend
        try:
            import resend
            resend.Emails.send({
                "from": "Medikart <orders@medicofiles.in>",
                "to": [email],
                "subject": f"{otp} is your Medikart Login Code",
                "html": f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 16px;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px;">
                        <h2 style="color: #059669; margin: 0; font-size: 22px; font-weight: 800;">🌿 Medikart Quick Commerce</h2>
                    </div>
                    <p style="font-size: 14px; color: #475569; margin-bottom: 16px;">Your one-time email login code:</p>
                    <div style="background: #f0fdf4; border: 1px dashed #22c55e; border-radius: 12px; padding: 18px; text-align: center; margin-bottom: 20px;">
                        <span style="font-size: 32px; font-weight: 900; letter-spacing: 8px; color: #15803d;">{otp}</span>
                    </div>
                    <p style="font-size: 12px; color: #94a3b8; margin: 0;">Valid for 10 minutes. Please do not share this OTP with anyone.</p>
                </div>
                """
            })
            print(f"[RESEND SUCCESS] Sent login code to {email}")
        except Exception as mail_err:
            print(f"[RESEND ERROR] {mail_err}")

        return jsonify({'success': True, 'message': 'Verification code sent to your email'})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# 2. VERIFY EMAIL OTP
@medikart_bp.route('/medikart/auth/verify-otp', methods=['POST'])
def medikart_verify_otp():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    otp = data.get('otp', '').strip()

    if not email or not otp:
        return jsonify({'success': False, 'message': 'Missing email or OTP'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
            SELECT id FROM medikart_otp_verifications
            WHERE identifier = %s AND otp_code = %s AND is_used = FALSE AND expires_at > NOW()
            ORDER BY created_at DESC LIMIT 1;
        """, (email, otp))
        valid_otp = cur.fetchone()

        if not valid_otp:
            return jsonify({'success': False, 'message': 'Invalid or expired OTP'}), 400

        cur.execute("UPDATE medikart_otp_verifications SET is_used = TRUE WHERE id = %s", (valid_otp['id'],))

        # Check existing customer by email
        cur.execute("SELECT * FROM medikart_customers WHERE email = %s", (email,))
        customer = cur.fetchone()

        if customer:
            cur.execute("UPDATE medikart_customers SET last_login = NOW() WHERE id = %s", (customer['id'],))
            conn.commit()

            session['medikart_customer_id'] = customer['id']
            session.permanent = True

            return jsonify({
                'success': True,
                'is_new_user': False,
                'customer': {
                    'id': customer['id'],
                    'name': customer['full_name'],
                    'email': customer['email'],
                    'address': customer['delivery_address']
                }
            })
        else:
            cur.execute("""
                INSERT INTO medikart_customers (email, last_login) 
                VALUES (%s, NOW()) RETURNING id;
            """, (email,))
            new_id = cur.fetchone()['id']
            conn.commit()

            session['medikart_customer_id'] = new_id
            session.permanent = True

            return jsonify({
                'success': True,
                'is_new_user': True,
                'customer_id': new_id
            })

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# 3. SAVE PROFILE (For first-time users: Name & Address)
@medikart_bp.route('/medikart/auth/save-profile', methods=['POST'])
def medikart_save_profile():
    customer_id = session.get('medikart_customer_id')
    if not customer_id:
        return jsonify({'success': False, 'message': 'Unauthorized. Please login again.'}), 401

    data = request.get_json() or {}
    full_name = data.get('full_name', '').strip()
    address = data.get('address', '').strip()

    if not full_name or not address:
        return jsonify({'success': False, 'message': 'Name and delivery address are required'}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
            UPDATE medikart_customers
            SET full_name = %s, delivery_address = %s
            WHERE id = %s RETURNING *;
        """, (full_name, address, customer_id))
        updated = cur.fetchone()
        conn.commit()

        return jsonify({
            'success': True,
            'customer': {
                'id': updated['id'],
                'name': updated['full_name'],
                'phone': updated['phone'],
                'email': updated['email'],
                'address': updated['delivery_address']
            }
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# 4. GET CURRENT SESSION STATUS
@medikart_bp.route('/medikart/auth/me', methods=['GET'])
def medikart_me():
    customer_id = session.get('medikart_customer_id')
    if not customer_id:
        return jsonify({'logged_in': False})

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id, full_name, phone, email, delivery_address FROM medikart_customers WHERE id = %s", (customer_id,))
        cust = cur.fetchone()
        if cust:
            return jsonify({
                'logged_in': True,
                'customer': {
                    'id': cust['id'],
                    'name': cust['full_name'],
                    'phone': cust['phone'],
                    'email': cust['email'],
                    'address': cust['delivery_address']
                }
            })
        return jsonify({'logged_in': False})
    finally:
        cur.close()
        conn.close()


# 5. LOGOUT
@medikart_bp.route('/medikart/auth/logout', methods=['POST'])
def medikart_logout():
    session.pop('medikart_customer_id', None)
    return jsonify({'success': True})

# GET LOGGED-IN CUSTOMER ORDERS & LIVE STATUS
@medikart_bp.route('/medikart/api/my-orders', methods=['GET'])
def get_my_orders():
    cust_id = session.get('medikart_customer_id')
    if not cust_id:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
            SELECT id, order_number, store_id, customer_name, customer_phone, 
                    delivery_address, total_amount, order_status, items_json, 
                    prescription_url, created_at
            FROM medikart_orders
            WHERE customer_id = %s
            ORDER BY created_at DESC;
        """, (cust_id,))
        orders = cur.fetchall()

        # Datetime ko string format mein sanitize karein
        formatted_orders = []
        for o in orders:
            order_dict = dict(o)
            if order_dict.get('created_at'):
                order_dict['created_at_str'] = order_dict['created_at'].strftime('%d %b %Y, %I:%M %p')
            formatted_orders.append(order_dict)

        return jsonify({'success': True, 'orders': formatted_orders})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# UPDATE PAYMENT STATUS FROM HUB
@medikart_bp.route('/medikart/api/update-payment-status', methods=['POST'])
@login_required
def update_payment_status():
    data = request.get_json() or {}
    order_id = data.get('order_id')
    new_status = data.get('payment_status')  # 'VERIFIED' ya 'PENDING_VERIFICATION'

    if not order_id or not new_status:
        return jsonify({'success': False, 'message': 'Missing data'}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE medikart_orders 
            SET payment_status = %s 
            WHERE id = %s AND store_id = %s;
        """, (new_status, order_id, current_user.id))
        conn.commit()
        return jsonify({'success': True, 'payment_status': new_status})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# CUSTOMER CONFIRM ORDER RECEIVED
@medikart_bp.route('/medikart/api/confirm-received', methods=['POST'])
def confirm_order_received():
    data = request.get_json() or {}
    order_id = data.get('order_id')
    cust_id = session.get('medikart_customer_id')

    if not order_id:
        return jsonify({'success': False, 'message': 'Order ID missing'}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        # Customer verified: order_status ko DELIVERED mark karenge
        if cust_id:
            cur.execute("""
                UPDATE medikart_orders 
                SET order_status = 'DELIVERED' 
                WHERE id = %s AND customer_id = %s;
            """, (order_id, cust_id))
        else:
            cur.execute("""
                UPDATE medikart_orders 
                SET order_status = 'DELIVERED' 
                WHERE id = %s;
            """, (order_id,))

        conn.commit()
        return jsonify({'success': True, 'order_status': 'DELIVERED'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# TOGGLE 1-2 DAYS PROCUREMENT DELIVERY (Matches by inventory_id or master_item_id)
@medikart_bp.route('/medikart/api/toggle-procurement', methods=['POST'])
@login_required
def toggle_procurement():
    data = request.get_json() or {}
    item_id = data.get('item_id')

    if not item_id:
        return jsonify({'success': False, 'message': 'Item ID missing'}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        # Step 1: Check by primary id OR master_item_id for current store
        cur.execute("""
            SELECT id, COALESCE(is_procurement_item, FALSE)
            FROM medikart_store_inventory 
            WHERE (id = %s OR master_item_id = %s) AND store_id = %s
            LIMIT 1;
        """, (item_id, item_id, current_user.id))
        
        row = cur.fetchone()
        
        # Fallback agar user_id isolated na ho
        if not row:
            cur.execute("""
                SELECT id, COALESCE(is_procurement_item, FALSE)
                FROM medikart_store_inventory 
                WHERE id = %s OR master_item_id = %s
                LIMIT 1;
            """, (item_id, item_id))
            row = cur.fetchone()

        if not row:
            return jsonify({'success': False, 'message': 'Inventory item not found'}), 404

        inv_id = row['id'] if isinstance(row, dict) else row[0]
        curr_val = row['is_procurement_item'] if isinstance(row, dict) else row[1]
        new_val = not bool(curr_val)

        # Step 2: Update row
        cur.execute("""
            UPDATE medikart_store_inventory
            SET is_procurement_item = %s
            WHERE id = %s;
        """, (new_val, inv_id))

        conn.commit()
        return jsonify({'success': True, 'is_procurement_item': new_val})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# SECURE GOOGLE REVERSE GEOCODING (GPS TO EXACT ADDRESS)
@medikart_bp.route('/medikart/api/reverse-geocode', methods=['POST'])
def reverse_geocode_coords():
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')

    if not lat or not lng:
        return jsonify({'success': False, 'message': 'Coordinates missing'}), 400

    api_key = os.getenv('GOOGLE_MAPS_API_KEY')
    if not api_key:
        return jsonify({'success': False, 'message': 'Google Maps API key missing'}), 500

    endpoint = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        'latlng': f"{lat},{lng}",
        'key': api_key
    }

    try:
        resp = requests.get(endpoint, params=params, timeout=5)
        res_data = resp.json()

        if res_data.get('status') == 'OK' and res_data.get('results'):
            # Google's best rooftop/street-level match
            top_result = res_data['results'][0]
            full_address = top_result.get('formatted_address', '')

            # Short title nikaalna (Colony / Sublocality / Sector)
            short_title = ""
            for comp in top_result.get('address_components', []):
                types = comp.get('types', [])
                if 'sublocality_level_1' in types or 'sublocality' in types or 'neighborhood' in types:
                    short_title = comp.get('long_name')
                    break
                elif 'locality' in types and not short_title:
                    short_title = comp.get('long_name')

            if not short_title:
                short_title = full_address.split(',')[0]

            return jsonify({
                'success': True,
                'title': short_title,
                'full_address': full_address
            })
        else:
            return jsonify({'success': False, 'message': 'Unable to resolve address from GPS'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500