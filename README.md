# 💊 Medicofiles — Smart Medical Store Operating System

> A high-performance, full-stack pharmacy management workspace designed to digitize inventory tracking, billing, customer ledgers, and automated stock ingestion.

---

## 🌟 Key Features

* **⚡ Lightning-Fast POS Billing:** Instant checkout with support for loose tablet/strip unit calculations and automatic stock updates.
* **📦 Smart Inventory & Batch Radar:** Batch-wise stock management with dynamic expiry tracking and automated low-stock warnings.
* **📄 Automated Bill Ingestion (PDF & Excel Parser):** Extract medicine names, batch numbers, MRPs, and quantities directly from supplier invoices (`.pdf`, `.xlsx`, `.xls`, `.csv`).
* **📖 Customer Udhar Ledger:** Manage unpaid accounts, credit balances, and transaction history effortlessly.
* **🩺 Smart Treatment & Symptom Assistant:** Map disease/symptom tags with relevant medicines and dosages for instant quick-reference during customer inquiries.
* **🌙 Dark / Light Glassmorphism UI:** Modern, responsive interface with smooth theme switching and interactive modals.

---

## 🛠️ Tech Stack

* **Backend:** Python, Flask, Flask-SQLAlchemy, Flask-Login, Flask-Mail
* **Database:** SQLite
* **Parser & Data Extraction:** `pandas`, `openpyxl`, `pdfplumber`
* **Frontend:** HTML5, CSS3, JavaScript (ES6+), Bootstrap 5, Jinja2
* **Security & Auth:** OAuth2 (Google Login), Environment Variables (`python-dotenv`), Password Hashing

---

## 🚀 Getting Started

### 1. Prerequisites
Ensure you have **Python 3.9+** installed on your machine.

### 2. Clone the Repository
```bash
git clone [https://github.com/Goldi-Yash/Medicofiles.git](https://github.com/Goldi-Yash/Medicofiles.git)
cd Medicofiles

### 3. Setup Virtual Environment

python -m venv venv
venv\Scripts\activate

### 4. Install Dependencies

pip install -r requirements.txt\

### 5. Setup Environment Variables
### Create a .env file in the root directory and add your configurations:

SECRET_KEY=your_secret_key_here
client_id=your_google_oauth_client_id
client_secret=your_google_oauth_client_secret
MAIL_USERNAME=your_email@gmail.com
MAIL_PASSWORD=your_app_password

### 6. Run the Application

python main.py
# Open your browser and navigate to http://127.0.0.1:5000.

### 📂 Project Structure

MEDICO/
├── instance/            # Local SQLite database (git-ignored)
├── static/              # CSS, JavaScript, and Image previews
├── templates/           # Jinja2 HTML templates
├── .env                 # Environment variables (git-ignored)
├── .gitignore           # Git ignore rules
├── main.py              # Main Flask application logic & routes
├── requirements.txt     # Python dependency list
└── README.md            # Project documentation

### 📜 License
### This project is open-source and available under the MIT License.


