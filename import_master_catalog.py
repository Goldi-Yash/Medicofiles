import os
import csv
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get('DATABASE_URL')
CSV_FILE = os.path.join(os.path.dirname(__file__), 'A_Z_medicines_dataset_of_India.csv')

def detect_category(composition, name, dosage):
    c = composition.lower()
    n = name.lower()
    d = dosage.lower()

    if any(k in c or k in n for k in ['paracetamol', 'ibuprofen', 'diclofenac', 'aceclofenac', 'tramadol', 'mefenamic', 'combiflam', 'crocin']):
        return 'Fever & Pain'
    if any(k in c or k in n for k in ['pantoprazole', 'omeprazole', 'rabeprazole', 'domperidone', 'antacid', 'magaldrate', 'ranitidine', 'famotidine']):
        return 'Acidity & Gas'
    if any(k in c or k in n for k in ['cetirizine', 'levocetirizine', 'montelukast', 'fexofenadine', 'phenylephrine', 'cough', 'chlorpheniramine', 'ambroxol', 'salbutamol', 'ascoril']):
        return 'Cold & Allergy'
    if any(k in c or k in n for k in ['amoxicillin', 'clavulanic', 'azithromycin', 'cefixime', 'ofloxacin', 'ciprofloxacin', 'cefpodoxime', 'meropenem', 'piperacillin']):
        return 'Antibiotics'
    if any(k in c or k in n for k in ['calcium', 'vitamin', 'zinc', 'iron', 'folic', 'multivitamin', 'ginseng', 'd3', 'cholecalciferol']):
        return 'Vitamins & Supplements'
    if any(k in c or k in n or k in d for k in ['cream', 'gel', 'lotion', 'ointment', 'soap', 'face wash', 'shampoo', 'dusting powder', 'antiseptic']):
        return 'Personal Care'
    if any(k in c or k in n or k in d for k in ['bandage', 'gauze', 'cotton', 'plaster', 'strip', 'betadine', 'savlon', 'dettol']):
        return 'First Aid'
    return 'General Health'

def detect_rx_flags(composition, name):
    c = composition.lower()
    n = name.lower()
    
    # Strictly Schedule H & H1 drugs (Mandatory Prescription)
    if any(k in c or k in n for k in ['amoxicillin', 'azithromycin', 'cefixime', 'ofloxacin', 'ciprofloxacin', 'meropenem', 'alprazolam', 'clonazepam', 'lorazepam', 'tramadol']):
        return True, True
    # General Schedule H (Rx Required badge)
    if any(k in c or k in n for k in ['pantoprazole', 'domperidone', 'montelukast', 'telmisartan', 'metformin', 'rosuvastatin', 'atorvastatin', 'glimepiride']):
        return True, False
    return False, False

def run_import():
    if not os.path.exists(CSV_FILE):
        print(f"Error: File '{CSV_FILE}' not found!")
        return

    print("Connecting to Supabase database...")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    print("Opening and parsing A_Z_medicines_dataset_of_India.csv...")
    
    rows_to_insert = []
    seen_names = set()
    batch_size = 2500
    total_processed = 0

    insert_sql = """
        INSERT INTO medikart_master_catalog (
            name, brand, category, dosage_form, mrp, default_selling_price,
            requires_rx, is_strictly_rx, composition, pack_size, is_discontinued
        ) VALUES %s
        ON CONFLICT (name) DO UPDATE SET
            brand = EXCLUDED.brand,
            category = EXCLUDED.category,
            dosage_form = EXCLUDED.dosage_form,
            mrp = EXCLUDED.mrp,
            default_selling_price = EXCLUDED.default_selling_price,
            requires_rx = EXCLUDED.requires_rx,
            is_strictly_rx = EXCLUDED.is_strictly_rx,
            composition = EXCLUDED.composition,
            pack_size = EXCLUDED.pack_size,
            is_discontinued = EXCLUDED.is_discontinued;
    """

    with open(CSV_FILE, mode='r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_name = (row.get('name') or '').strip()
            if not raw_name or raw_name in seen_names:
                continue

            seen_names.add(raw_name)

            # Clean Price & Default Discounted Price (10% margin)
            try:
                mrp_val = float(row.get('price(₹)') or row.get('price(â‚¹)') or 0.0)
            except Exception:
                mrp_val = 0.0

            selling_price_val = round(mrp_val * 0.90, 2) if mrp_val > 0 else 0.0

            brand = (row.get('manufacturer_name') or 'Pharma').strip()
            dosage = (row.get('type') or 'Tablet').strip()
            pack = (row.get('pack_size_label') or '').strip()

            # Merge Salty Compositions
            comp1 = (row.get('short_composition1') or '').strip()
            comp2 = (row.get('short_composition2') or '').strip()
            composition_parts = [p for p in [comp1, comp2] if p]
            composition = " + ".join(composition_parts) if composition_parts else dosage

            # Category & Rx Logic
            category = detect_category(composition, raw_name, dosage)
            req_rx, strict_rx = detect_rx_flags(composition, raw_name)

            is_disc = str(row.get('Is_discontinued', 'False')).strip().lower() == 'true'

            rows_to_insert.append((
                raw_name,
                brand[:150],
                category[:100],
                dosage[:50],
                mrp_val,
                selling_price_val,
                req_rx,
                strict_rx,
                composition,
                pack,
                is_disc
            ))

            if len(rows_to_insert) >= batch_size:
                execute_values(cur, insert_sql, rows_to_insert)
                conn.commit()
                total_processed += len(rows_to_insert)
                print(f"Pushed {total_processed} medicines into Supabase master catalog...")
                rows_to_insert = []

    if rows_to_insert:
        execute_values(cur, insert_sql, rows_to_insert)
        conn.commit()
        total_processed += len(rows_to_insert)
        print(f"Final push complete. Total {total_processed} medicines synced!")

    cur.close()
    conn.close()
    print("ALL 2.5 LAKH MEDICINES SUCCESSFULLY CATALOGED IN SUPABASE!")

if __name__ == "__main__":
    run_import()