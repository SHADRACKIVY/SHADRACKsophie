#!/usr/bin/env python3
import sqlite3, hashlib, secrets, json, datetime as dt, os, io, csv, zipfile, html, re, shutil
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME='MONTEZ LIQUOR'
BASE_DIR=os.path.abspath(os.path.dirname(__file__))
DATA_DIR=os.environ.get('KAKA_DATA_DIR',os.path.join(os.path.expanduser('~'),'KAKA_LIQUOR_DATA'))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH=os.path.join(DATA_DIR,'kaka_liquor.db')
BACKUP_DIR=os.path.join(DATA_DIR,'backups'); os.makedirs(BACKUP_DIR, exist_ok=True)
HOST='0.0.0.0'; PORT=int(os.environ.get('KAKA_PORT','5007'))
SESSIONS={}
SESSION_IDLE_SECONDS=120


def now(): return dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
def now_str(): return now().strftime('%Y-%m-%d %H:%M:%S')
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def money(v):
    currency='KSh'
    try:
        if 'db' in globals(): currency=get_setting('currency','KSh') or 'KSh'
    except Exception: pass
    return f'{currency} {float(v):,.2f}'
def esc(v): return html.escape('' if v is None else str(v))
def json_body(h):
    n=int(h.headers.get('Content-Length','0') or 0); raw=h.rfile.read(n) if n else b'{}'
    return json.loads(raw.decode('utf-8') or '{}')

def send_json(h, obj, status=200):
    raw=json.dumps(obj,ensure_ascii=False).encode(); h.send_response(status); h.send_header('Content-Type','application/json; charset=utf-8'); h.send_header('Content-Length',str(len(raw))); h.end_headers(); h.wfile.write(raw)

def send_bytes(h, raw, content_type, filename, disposition='attachment'):
    h.send_response(200); h.send_header('Content-Type',content_type); h.send_header('Content-Length',str(len(raw))); h.send_header('Content-Disposition',f'{disposition}; filename="{filename}"'); h.end_headers(); h.wfile.write(raw)

class DB:
    def __init__(self,path=DB_PATH): self.path=path; self.init()
    def c(self):
        c=sqlite3.connect(self.path,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=30000'); return c
    def init(self):
        # Preserve a pre-migration copy if an existing database needs COGS schema/cost repair.
        if os.path.isfile(self.path) and os.path.getsize(self.path)>0:
            needs_cogs_backup=False
            try:
                probe=sqlite3.connect(self.path)
                has_table=probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sale_lines'").fetchone()
                if has_table:
                    cols={r[1] for r in probe.execute('PRAGMA table_info(sale_lines)').fetchall()}
                    if 'unit_cost' not in cols or 'cogs_cost_source' not in cols:
                        needs_cogs_backup=True
                    elif probe.execute("SELECT 1 FROM sale_lines WHERE COALESCE(unit_cost,0)<=0 AND COALESCE(cogs_cost_source,'RECORDED')='RECORDED' LIMIT 1").fetchone():
                        needs_cogs_backup=True
                probe.close()
            except Exception:
                try: probe.close()
                except Exception: pass
                raise
            if needs_cogs_backup:
                backup_dir=os.path.join(os.path.dirname(os.path.abspath(self.path)),'backups')
                os.makedirs(backup_dir,exist_ok=True)
                backup_path=os.path.join(backup_dir,'pre_cogs_schema_repair_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db')
                src=sqlite3.connect(self.path); dst=sqlite3.connect(backup_path)
                try: src.backup(dst)
                finally: dst.close(); src.close()
        c=self.c(); c.executescript('''
        CREATE TABLE IF NOT EXISTS branches(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,code TEXT UNIQUE,location TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL,full_name TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('ADMIN','SALES')),access_role TEXT NOT NULL DEFAULT 'SALES',active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,branch_id INTEGER REFERENCES branches(id));
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,product_no TEXT UNIQUE,sku TEXT UNIQUE NOT NULL,name TEXT UNIQUE NOT NULL,category TEXT NOT NULL,unit TEXT NOT NULL,sell_price REAL NOT NULL,wholesale_price REAL NOT NULL DEFAULT 0,avg_cost REAL NOT NULL DEFAULT 0,stock_qty REAL NOT NULL DEFAULT 0,reorder_level REAL NOT NULL DEFAULT 5,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_no TEXT UNIQUE NOT NULL,full_name TEXT NOT NULL,phone TEXT UNIQUE NOT NULL,id_no TEXT,address TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT,receipt_no TEXT UNIQUE NOT NULL,sold_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),customer_id INTEGER REFERENCES customers(id),customer_type TEXT NOT NULL,total REAL NOT NULL,sale_type TEXT NOT NULL DEFAULT 'RETAIL',branch_id INTEGER REFERENCES branches(id));
        CREATE TABLE IF NOT EXISTS sale_lines(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,product_id INTEGER NOT NULL REFERENCES products(id),qty REAL NOT NULL,unit_price REAL NOT NULL,unit_cost REAL NOT NULL DEFAULT 0,cogs_cost_source TEXT NOT NULL DEFAULT 'RECORDED');
        CREATE TABLE IF NOT EXISTS sale_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,amount REAL NOT NULL,payment_method TEXT NOT NULL CHECK(payment_method IN ('CASH','MPESA')),mpesa_code TEXT,paid_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id));
        CREATE UNIQUE INDEX IF NOT EXISTS ux_sale_mpesa ON sale_payments(UPPER(mpesa_code)) WHERE mpesa_code IS NOT NULL AND mpesa_code<>'';
        CREATE TABLE IF NOT EXISTS credit_accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER UNIQUE NOT NULL REFERENCES sales(id) ON DELETE CASCADE,customer_id INTEGER NOT NULL REFERENCES customers(id),principal REAL NOT NULL,due_date TEXT NOT NULL,collateral TEXT NOT NULL,collateral_value REAL NOT NULL DEFAULT 0,paid_amount REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'OPEN',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,credit_id INTEGER NOT NULL REFERENCES credit_accounts(id) ON DELETE CASCADE,payment_date TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),amount REAL NOT NULL,payment_method TEXT NOT NULL CHECK(payment_method IN ('CASH','MPESA')),mpesa_code TEXT,reference TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_credit_mpesa ON credit_payments(UPPER(mpesa_code)) WHERE mpesa_code IS NOT NULL AND mpesa_code<>'';
        CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT,purchase_no TEXT UNIQUE NOT NULL,purchased_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),supplier TEXT NOT NULL,payment_method TEXT NOT NULL,total REAL NOT NULL,reference TEXT);
        CREATE TABLE IF NOT EXISTS purchase_lines(id INTEGER PRIMARY KEY AUTOINCREMENT,purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,product_id INTEGER NOT NULL REFERENCES products(id),qty REAL NOT NULL,unit_cost REAL NOT NULL,total REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,expense_no TEXT UNIQUE NOT NULL,expense_date TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),category TEXT NOT NULL,description TEXT NOT NULL,amount REAL NOT NULL,payment_method TEXT NOT NULL,reference TEXT);
        CREATE TABLE IF NOT EXISTS stock_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,movement_time TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),product_id INTEGER NOT NULL REFERENCES products(id),movement_type TEXT NOT NULL,qty REAL NOT NULL,before_qty REAL NOT NULL,after_qty REAL NOT NULL,unit_cost REAL NOT NULL,reference TEXT,branch_id INTEGER REFERENCES branches(id));
        CREATE TABLE IF NOT EXISTS branch_stock(branch_id INTEGER NOT NULL REFERENCES branches(id),product_id INTEGER NOT NULL REFERENCES products(id),qty REAL NOT NULL DEFAULT 0,avg_cost REAL NOT NULL DEFAULT 0,buy_price REAL NOT NULL DEFAULT 0,sell_price REAL NOT NULL DEFAULT 0,wholesale_price REAL NOT NULL DEFAULT 0,reorder_level REAL NOT NULL DEFAULT 5,updated_at TEXT NOT NULL,PRIMARY KEY(branch_id,product_id));
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,user_id INTEGER,username_snapshot TEXT NOT NULL,action TEXT NOT NULL,entity TEXT,entity_id TEXT,details TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL,updated_by INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS mpesa_stk_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),checkout_request_id TEXT UNIQUE,merchant_request_id TEXT,client_ref TEXT UNIQUE NOT NULL,amount REAL NOT NULL,phone TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',mpesa_code TEXT,transaction_date TEXT,raw_response TEXT,updated_at TEXT NOT NULL,sale_id INTEGER REFERENCES sales(id));
        CREATE TABLE IF NOT EXISTS pending_payment_batches(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id),total REAL NOT NULL,product_id INTEGER NOT NULL REFERENCES products(id),qty REAL NOT NULL,customer_json TEXT NOT NULL DEFAULT '{}',due_date TEXT,collateral TEXT,collateral_value REAL NOT NULL DEFAULT 0,payment_count INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',created_sale_id INTEGER REFERENCES sales(id));
        CREATE TABLE IF NOT EXISTS pending_payment_allocations(id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id INTEGER NOT NULL REFERENCES pending_payment_batches(id) ON DELETE CASCADE,sequence_no INTEGER NOT NULL,amount REAL NOT NULL,payment_method TEXT NOT NULL CHECK(payment_method IN ('CASH','MPESA')),phone TEXT,checkout_request_id TEXT,mpesa_code TEXT,status TEXT NOT NULL DEFAULT 'PENDING',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(batch_id,sequence_no));
        CREATE TABLE IF NOT EXISTS mpesa_transactions(id INTEGER PRIMARY KEY AUTOINCREMENT,received_at TEXT NOT NULL,transaction_type TEXT,trans_id TEXT UNIQUE,trans_time TEXT,amount REAL NOT NULL,business_short_code TEXT,bill_ref_number TEXT,invoice_number TEXT,org_account_balance TEXT,msisdn TEXT,first_name TEXT,middle_name TEXT,last_name TEXT,raw_json TEXT,matched_sale_payment_id INTEGER REFERENCES sale_payments(id),status TEXT NOT NULL DEFAULT 'UNMATCHED');
        CREATE TABLE IF NOT EXISTS permissions(key TEXT PRIMARY KEY,label TEXT NOT NULL,description TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS role_permissions(role TEXT NOT NULL,key TEXT NOT NULL REFERENCES permissions(key) ON DELETE CASCADE,enabled INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(role,key));
        CREATE TABLE IF NOT EXISTS user_permissions(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,key TEXT NOT NULL REFERENCES permissions(key) ON DELETE CASCADE,enabled INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,key));
        ''')
        # Branch stock isolation: stock balances are stored per branch/product. Legacy shared
        # products.stock_qty is deliberately NOT copied into branches; opening stock must be
        # physically verified and entered for each registered branch by an authorised user.
        purchase_cols={r['name'] for r in c.execute('PRAGMA table_info(purchases)').fetchall()}
        if 'branch_id' not in purchase_cols: c.execute('ALTER TABLE purchases ADD COLUMN branch_id INTEGER REFERENCES branches(id)')
        movement_cols={r['name'] for r in c.execute('PRAGMA table_info(stock_movements)').fetchall()}
        if 'branch_id' not in movement_cols: c.execute('ALTER TABLE stock_movements ADD COLUMN branch_id INTEGER REFERENCES branches(id)')
        expense_cols={r['name'] for r in c.execute('PRAGMA table_info(expenses)').fetchall()}
        if 'branch_id' not in expense_cols: c.execute('ALTER TABLE expenses ADD COLUMN branch_id INTEGER REFERENCES branches(id)')
        # Branch attribution for historic activity is based on the employee's assigned branch.
        c.execute('UPDATE purchases SET branch_id=(SELECT branch_id FROM users WHERE users.id=purchases.user_id) WHERE branch_id IS NULL')
        c.execute('UPDATE stock_movements SET branch_id=(SELECT branch_id FROM users WHERE users.id=stock_movements.user_id) WHERE branch_id IS NULL')
        c.execute('UPDATE expenses SET branch_id=(SELECT branch_id FROM users WHERE users.id=expenses.user_id) WHERE branch_id IS NULL')
        # Branch stock rows are initialized after legacy product columns have been upgraded.
        c.execute('CREATE INDEX IF NOT EXISTS idx_branch_stock_product ON branch_stock(product_id,branch_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_movements_branch_time ON stock_movements(branch_id,movement_time)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_purchases_branch_date ON purchases(branch_id,purchased_at)')
        # Branch management migration: keep historic sales attributed to the branch at time of sale.
        user_cols={r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'branch_id' not in user_cols:
            c.execute('ALTER TABLE users ADD COLUMN branch_id INTEGER REFERENCES branches(id)')
        sales_cols={r['name'] for r in c.execute('PRAGMA table_info(sales)').fetchall()}
        if 'branch_id' not in sales_cols:
            c.execute('ALTER TABLE sales ADD COLUMN branch_id INTEGER REFERENCES branches(id)')
        c.execute('UPDATE sales SET branch_id=(SELECT branch_id FROM users WHERE users.id=sales.user_id) WHERE branch_id IS NULL')
        customer_cols={r['name'] for r in c.execute('PRAGMA table_info(customers)').fetchall()}
        if 'branch_id' not in customer_cols: c.execute('ALTER TABLE customers ADD COLUMN branch_id INTEGER REFERENCES branches(id)')
        c.execute('UPDATE customers SET branch_id=(SELECT s.branch_id FROM sales s WHERE s.customer_id=customers.id AND s.branch_id IS NOT NULL ORDER BY s.sold_at DESC LIMIT 1) WHERE branch_id IS NULL')
        ts=now_str()
        # Branches are created only through Administrator > Branch Registration; never invent a default outlet.
        c.execute('UPDATE purchases SET branch_id=(SELECT branch_id FROM users WHERE users.id=purchases.user_id) WHERE branch_id IS NULL')
        c.execute('UPDATE stock_movements SET branch_id=(SELECT branch_id FROM users WHERE users.id=stock_movements.user_id) WHERE branch_id IS NULL')
        c.execute('UPDATE expenses SET branch_id=(SELECT branch_id FROM users WHERE users.id=expenses.user_id) WHERE branch_id IS NULL')
        # Ensure every existing and newly created branch has a zeroed row for every catalogue product.
        for br in c.execute('SELECT id FROM branches').fetchall():
            for pr in c.execute('SELECT id,avg_cost,sell_price,wholesale_price,reorder_level FROM products').fetchall():
                c.execute('INSERT OR IGNORE INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',
                    (br['id'],pr['id'],float(pr['avg_cost'] or 0),float(pr['avg_cost'] or 0),float(pr['sell_price'] or 0),float(pr['wholesale_price'] or pr['sell_price'] or 0),float(pr['reorder_level'] or 0),now_str()))
        c.execute('CREATE INDEX IF NOT EXISTS idx_sales_branch_date ON sales(branch_id,sold_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_sales_user_date ON sales(user_id,sold_at)')
        # Shared-stock wholesale/retail migration: distinct prices and sale-channel tracking.
        product_cols={r['name'] for r in c.execute('PRAGMA table_info(products)').fetchall()}
        if 'wholesale_price' not in product_cols:
            c.execute('ALTER TABLE products ADD COLUMN wholesale_price REAL NOT NULL DEFAULT 0')
            # Legacy products default to their former single price until an admin configures wholesale pricing.
            c.execute('UPDATE products SET wholesale_price=sell_price WHERE wholesale_price<=0')
        sales_cols={r['name'] for r in c.execute('PRAGMA table_info(sales)').fetchall()}
        if 'sale_type' not in sales_cols:
            c.execute("ALTER TABLE sales ADD COLUMN sale_type TEXT NOT NULL DEFAULT 'RETAIL'")
        # COGS schema migration: retain each sale's cost snapshot and identify repaired/missing historical costs.
        sale_line_cols={r['name'] for r in c.execute('PRAGMA table_info(sale_lines)').fetchall()}
        if 'unit_cost' not in sale_line_cols:
            c.execute('ALTER TABLE sale_lines ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0')
        if 'cogs_cost_source' not in sale_line_cols:
            c.execute("ALTER TABLE sale_lines ADD COLUMN cogs_cost_source TEXT NOT NULL DEFAULT 'RECORDED'")
        legacy_lines=c.execute('''SELECT sl.id,sl.sale_id,sl.product_id,COALESCE(sl.unit_cost,0) unit_cost,
                    COALESCE(sl.cogs_cost_source,'RECORDED') cogs_cost_source,s.receipt_no,s.sold_at
                    FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id
                    WHERE COALESCE(sl.unit_cost,0)<=0 AND COALESCE(sl.cogs_cost_source,'RECORDED')='RECORDED'
                    ORDER BY s.sold_at,sl.id''').fetchall()
        for sl in legacy_lines:
            movement=c.execute("SELECT unit_cost FROM stock_movements WHERE product_id=? AND movement_type='SALE' AND reference=? AND unit_cost>0 ORDER BY id LIMIT 1",(sl['product_id'],sl['receipt_no'])).fetchone()
            source='SALE_MOVEMENT'; cost=float(movement['unit_cost']) if movement else 0.0
            if cost<=0:
                movement=c.execute("SELECT unit_cost FROM stock_movements WHERE product_id=? AND movement_time<=? AND unit_cost>0 ORDER BY movement_time DESC,id DESC LIMIT 1",(sl['product_id'],sl['sold_at'])).fetchone()
                if movement:
                    cost=float(movement['unit_cost']); source='HISTORICAL_MOVEMENT'
            if cost<=0:
                product=c.execute('SELECT avg_cost FROM products WHERE id=?',(sl['product_id'],)).fetchone()
                if product and float(product['avg_cost'] or 0)>0:
                    cost=float(product['avg_cost']); source='ESTIMATED_CURRENT_AVG_COST'
                else:
                    source='MISSING_COST'
            c.execute('UPDATE sale_lines SET unit_cost=?,cogs_cost_source=? WHERE id=?',(cost,source,sl['id']))
        # Normalize source labels for existing rows with a valid recorded cost.
        c.execute("UPDATE sale_lines SET cogs_cost_source='RECORDED' WHERE COALESCE(unit_cost,0)>0 AND (cogs_cost_source IS NULL OR cogs_cost_source='')")
        # Backward-compatible role profile column for databases created by older builds.
        cols=[r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()]
        if 'access_role' not in cols:
            c.execute("ALTER TABLE users ADD COLUMN access_role TEXT NOT NULL DEFAULT 'SALES'")
        c.execute("UPDATE users SET access_role=CASE WHEN role='ADMIN' THEN 'ADMIN' WHEN access_role IS NULL OR access_role='' THEN 'SALES' ELSE access_role END")
        product_cols=[r['name'] for r in c.execute('PRAGMA table_info(products)').fetchall()]
        if 'product_no' not in product_cols:
            c.execute("ALTER TABLE products ADD COLUMN product_no TEXT")
        c.execute("UPDATE products SET product_no='PRD-' || printf('%05d',id) WHERE product_no IS NULL OR product_no=''")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_products_product_no ON products(product_no)")
        # Create zeroed branch balances only after all legacy product columns are present.
        # Old shared product.stock_qty is intentionally not copied into any branch.
        for br in c.execute('SELECT id FROM branches').fetchall():
            for pr in c.execute('SELECT id,avg_cost,sell_price,wholesale_price,reorder_level FROM products').fetchall():
                c.execute('INSERT OR IGNORE INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',
                    (br['id'],pr['id'],float(pr['avg_cost'] or 0),float(pr['avg_cost'] or 0),float(pr['sell_price'] or 0),float(pr['wholesale_price'] or pr['sell_price'] or 0),float(pr['reorder_level'] or 0),now_str()))
        if c.execute('SELECT COUNT(*) n FROM users').fetchone()['n']==0:
            t=now_str(); c.execute('INSERT INTO users(username,full_name,password_hash,role,access_role,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',('admin','System Administrator',sha('admin1234'),'ADMIN','ADMIN',1,t,t)); c.execute('INSERT INTO users(username,full_name,password_hash,role,access_role,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',('sales','Sales Person',sha('sales1234'),'SALES','SALES',1,t,t)); c.execute('INSERT INTO users(username,full_name,password_hash,role,access_role,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',('management','Management User',sha('management1234'),'SALES','MANAGEMENT',1,t,t))
        else:
            # Upgrade older installations without deleting existing users/data.
            if not c.execute("SELECT 1 FROM users WHERE username='management'").fetchone():
                t=now_str(); c.execute('INSERT INTO users(username,full_name,password_hash,role,access_role,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',('management','Management User',sha('management1234'),'SALES','MANAGEMENT',1,t,t))
        # Administrator and Management accounts remain unassigned; Sales users must be assigned by Administrator.
        permission_defs=[
            ('VIEW_DASHBOARD','Dashboard','View the executive dashboard.'),
            ('CREATE_SALE','Sales / POS','Create and complete sales.'),
            ('CREATE_PURCHASE','Purchases','Create and save purchases.'),
            ('RECORD_EXPENSE','Expenses','Record business expenses.'),
            ('MANAGE_CUSTOMERS','Customers / Credit','Register customers and manage credit/repayments.'),
            ('VIEW_REPORTS','Reports','View sales and financial reports.'),
            ('VIEW_STOCK','Stock Report','View stock reports and stock status.'),
            ('LIVE_MONITORING','Live Monitoring','View live activity monitoring.'),
            ('MANAGE_PRODUCTS','Product Registration','Create and manage products and stock.'),
            ('MANAGE_USERS','User Management','Create users and assign roles.'),
            ('MANAGE_SETTINGS','Administrator Settings','Change system settings and permissions.'),
            ('VIEW_LEDGER','Ledger & P&L','View accounting ledger and Profit & Loss.'),
            ('BACKUP_DATA','Backup','Create and download database backups.'),
            ('VIEW_AUDIT','Audit Log','View system audit history.'),
            ('PRINT_EXPORT','Print & Export','Print and download PDF/Excel reports.'),
            ('CREDIT_SALES','Credit Sales','Create credit sales.'),
            ('CREDIT_REPAYMENTS','Credit Repayments','Record credit repayments.'),
            ('AI_ASSISTANT','KAKA LIQUOR WINES AND SPIRIT AI Assistant','Ask business questions and get explanations from system data.')
        ]
        sales_defaults={'VIEW_DASHBOARD','CREATE_SALE','CREATE_PURCHASE','RECORD_EXPENSE','MANAGE_CUSTOMERS','VIEW_REPORTS','VIEW_STOCK','LIVE_MONITORING','PRINT_EXPORT','CREDIT_SALES','CREDIT_REPAYMENTS','MANAGE_PRODUCTS','AI_ASSISTANT'}
        management_defaults={'VIEW_DASHBOARD','VIEW_REPORTS','VIEW_STOCK','VIEW_LEDGER','PRINT_EXPORT','AI_ASSISTANT'}
        for key,label,desc in permission_defs:
            c.execute('INSERT OR IGNORE INTO permissions(key,label,description) VALUES(?,?,?)',(key,label,desc))
            c.execute('INSERT OR IGNORE INTO role_permissions(role,key,enabled) VALUES(?,?,?)',('ADMIN',key,1))
            c.execute('INSERT OR IGNORE INTO role_permissions(role,key,enabled) VALUES(?,?,?)',('SALES',key,1 if key in sales_defaults else 0))
            c.execute('INSERT OR IGNORE INTO role_permissions(role,key,enabled) VALUES(?,?,?)',('MANAGEMENT',key,1 if key in management_defaults else 0))

        defaults={
            'business_name':APP_NAME,
            'business_subtitle':'Executive Business Management System',
            'business_phone':'',
            'business_address':'',
            'business_website':'',
            'business_tax_pin':'',
            'business_branch_name':'Main Branch',
            'receipt_footer':'Thank you for shopping with us. | Powered by SHADYTECHSOLUTIONS',
            'report_footer':'Powered by SHADYTECHSOLUTIONS',
            'dashboard_enabled':'YES',
            'currency':'KSh',
            'idle_timeout_seconds':'120',
            'idle_timeout_minutes':'2',
            'live_refresh_seconds':'5',
            'default_reorder_level':'5',
            'default_unit':'bottle',
            'default_payment_transactions':'1',
            'receipt_prefix':'MONTEZ',
            'allow_credit_sales':'YES',
            'require_mpesa_code':'YES',
            'mpesa_enabled':'NO',
            'mpesa_environment':'sandbox',
            'mpesa_shortcode':'1546252',
            'mpesa_shortcode_type':'TILL',
            'mpesa_consumer_key':'',
            'mpesa_consumer_secret':'',
            'mpesa_passkey':'',
            'mpesa_callback_base_url':'',
            'mpesa_account_reference_prefix':'MONTEZ',
            'mpesa_transaction_desc':'MONTEZ LIQUOR',
            'max_payment_transactions':'5',
            'report_default_days':'7',
            'performance_green_threshold_pct':'50',
            'performance_basis':'NET_PROFIT_MARGIN',
            'ai_assistant_enabled':'YES',
            'ai_provider':'LOCAL',
            'ai_model':'gpt-5.6-luna',
            'ai_base_url':'https://api.openai.com/v1/responses',
            'ai_api_key':'',
            'mpesa_recovery_match_window_minutes':'1440',
            'business_logo_filename':'',
            'role_admin_label':'ADMINISTRATOR / DEVELOPER',
            'role_management_label':'MANAGEMENT',
            'role_sales_label':'SALES PERSON',
            'nav_dashboard':'Dashboard', 'nav_sales':'Sales / POS', 'nav_sales_dashboard':'Sales Dashboard',
            'nav_purchases':'Purchases', 'nav_expenses':'Expenses', 'nav_customers':'Customers / Credit', 'nav_reports':'Reports',
            'nav_stock_report':'Stock Report', 'nav_reorder_report':'Reorder Report', 'nav_live':'Live Monitoring', 'nav_products':'Product Registration',
            'nav_users':'User Management', 'nav_settings':'Settings', 'nav_ledger':'Ledger & P&L', 'nav_profit_loss':'Profit & Loss',
            'nav_expense_report':'Expense Report', 'nav_backup':'Backup', 'nav_audit':'Audit Log',
            'report_end_day':'END DAY REPORT', 'report_end_week':'END WEEK REPORT', 'report_today':'TODAY',
            'report_this_week':'THIS WEEK REPORT', 'report_monthly':'MONTHLY REPORT', 'report_this_month':'THIS MONTH',
            'report_last_month':'LAST MONTH', 'report_this_year':'THIS YEAR', 'report_last_year':'LAST YEAR'
        }
        t=now_str()
        for k,v in defaults.items():
            c.execute('INSERT OR IGNORE INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,NULL)',(k,str(v),t))
        # MONTEZ rebrand migration: update only legacy built-in KAKA defaults.
        # Preserve administrator-customized settings and all historical transaction records.
        c.execute("UPDATE settings SET value='MONTEZ LIQUOR',updated_at=? WHERE key='business_name' AND value IN ('KAKA LIQUOR WINES AND SPIRIT','KAKA LIQUOR')",(t,))
        c.execute("UPDATE settings SET value='MONTEZ',updated_at=? WHERE key IN ('receipt_prefix','mpesa_account_reference_prefix') AND value='KAKA'",(t,))
        c.execute("UPDATE settings SET value='MONTEZ LIQUOR',updated_at=? WHERE key='mpesa_transaction_desc' AND value='KAKA LIQUOR WINES AND SPIRIT'",(t,))
        c.execute("UPDATE permissions SET label='MONTEZ LIQUOR AI Assistant' WHERE key='AI_ASSISTANT' AND label IN ('KAKA LIQUOR WINES AND SPIRIT AI Assistant','KAKA LIQUOR AI Assistant')")
        c.execute("UPDATE settings SET value='Thank you for shopping with us. | Powered by SHADYTECHSOLUTIONS',updated_at=? WHERE key='receipt_footer' AND value='Thank you for shopping with us.'",(t,))
        c.execute("UPDATE settings SET value='Powered by SHADYTECHSOLUTIONS',updated_at=? WHERE key='report_footer' AND (value IS NULL OR TRIM(value)='')",(t,))
        if not c.execute("SELECT 1 FROM settings WHERE key='management_dashboard_v1'").fetchone():
            c.execute("UPDATE role_permissions SET enabled=1 WHERE role='MANAGEMENT' AND key='VIEW_DASHBOARD'")
            c.execute("INSERT INTO settings(key,value,updated_at,updated_by) VALUES('management_dashboard_v1','1',?,NULL)",(t,))
        if not c.execute("SELECT 1 FROM settings WHERE key='sales_product_registration_v1'").fetchone():
            c.execute("UPDATE role_permissions SET enabled=1 WHERE role='SALES' AND key='MANAGE_PRODUCTS'")
            c.execute("INSERT INTO settings(key,value,updated_at,updated_by) VALUES('sales_product_registration_v1','1',?,NULL)",(t,))
        c.commit(); c.close()
        c=self.c()
        try:
            cols={r['name'] for r in c.execute('PRAGMA table_info(pending_payment_batches)').fetchall()}
            if 'batch_ref' not in cols:
                c.execute('ALTER TABLE pending_payment_batches ADD COLUMN batch_ref TEXT')
                c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_batch_ref ON pending_payment_batches(batch_ref)')
            cur=c.execute("SELECT value FROM settings WHERE key='mpesa_shortcode'").fetchone()
            if cur and not str(cur['value']).strip():
                c.execute("UPDATE settings SET value='1546252' WHERE key='mpesa_shortcode'")
                c.execute("UPDATE settings SET value='TILL' WHERE key='mpesa_shortcode_type' AND (value='' OR value='PAYBILL')")
            c.commit()
        finally:
            c.close()
    def audit(self,c,u,action,entity='',entity_id='',details=''):
        c.execute('INSERT INTO audit_log(created_at,user_id,username_snapshot,action,entity,entity_id,details) VALUES(?,?,?,?,?,?,?)',(now_str(),u['id'] if u else None,u['username'] if u else 'SYSTEM',action,entity,str(entity_id or ''),details))

db=DB()

def get_settings():
    c=db.c(); rows=c.execute('SELECT key,value FROM settings').fetchall(); c.close(); return {r['key']:r['value'] for r in rows}

def get_setting(key, default=''):
    c=db.c(); r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone(); c.close(); return r['value'] if r else default

def update_settings(u, values):
    change_reason=str(values.get('change_reason','')).strip()
    if len(change_reason)<5:
        raise ValueError('A reason of at least 5 characters is required for every Administrator Settings change.')
    allowed={'business_name','business_subtitle','business_phone','business_address','business_website','business_tax_pin','business_branch_name','receipt_footer','report_footer','dashboard_enabled','currency','idle_timeout_seconds','idle_timeout_minutes','live_refresh_seconds','default_reorder_level','default_unit','default_payment_transactions','receipt_prefix','allow_credit_sales','require_mpesa_code','max_payment_transactions','report_default_days','mpesa_recovery_match_window_minutes','business_logo_filename','mpesa_enabled','mpesa_environment','mpesa_shortcode','mpesa_shortcode_type','mpesa_consumer_key','mpesa_consumer_secret','mpesa_passkey','mpesa_callback_base_url','mpesa_account_reference_prefix','mpesa_transaction_desc','role_admin_label','role_management_label','role_sales_label','nav_dashboard','nav_sales','nav_sales_dashboard','nav_purchases','nav_expenses','nav_customers','nav_reports','nav_stock_report','nav_reorder_report','nav_live','nav_products','nav_users','nav_settings','nav_ledger','nav_profit_loss','nav_expense_report','nav_backup','nav_audit','report_end_day','report_end_week','report_today','report_this_week','report_monthly','report_this_month','report_last_month','report_this_year','report_last_year','performance_green_threshold_pct','performance_basis','ai_assistant_enabled','ai_provider','ai_model','ai_base_url','ai_api_key','subscription_monthly_amount'}
    c=db.c(); t=now_str()
    try:
        c.execute('BEGIN')
        changed_keys=[]
        for k,v in values.items():
            if k not in allowed: continue
            vv=str(v).strip()
            oldrow=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
            oldvalue=oldrow['value'] if oldrow else ''
            if oldvalue != vv: changed_keys.append(k)
            if k in ('idle_timeout_seconds','idle_timeout_minutes','live_refresh_seconds','default_reorder_level','default_payment_transactions','max_payment_transactions','report_default_days','performance_green_threshold_pct'):
                n=int(float(vv))
                if k=='idle_timeout_seconds' and not 30<=n<=7200: raise ValueError('Idle timeout must be between 30 and 7200 seconds.')
                if k=='idle_timeout_minutes' and not 1<=n<=120: raise ValueError('Idle logout must be between 1 and 120 minutes.')
                if k=='live_refresh_seconds' and not 2<=n<=120: raise ValueError('Live refresh must be between 2 and 120 seconds.')
                if k=='default_reorder_level' and not 0<=n<=100000: raise ValueError('Default reorder level is invalid.')
                if k in ('max_payment_transactions','default_payment_transactions') and not 1<=n<=5: raise ValueError('Payment transaction setting must be between 1 and 5.')
                if k=='report_default_days' and not 1<=n<=365: raise ValueError('Report default days must be between 1 and 365.')
                if k=='performance_green_threshold_pct' and not 1<=n<=100: raise ValueError('Performance threshold must be between 1 and 100 percent.')
                if k=='mpesa_recovery_match_window_minutes' and not 5<=n<=10080: raise ValueError('M-Pesa recovery match window must be between 5 and 10080 minutes.')
                vv=str(n)
                if k=='idle_timeout_minutes':
                    c.execute('INSERT INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',('idle_timeout_seconds',str(n*60),t,u['id']))
                elif k=='idle_timeout_seconds':
                    c.execute('INSERT INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',('idle_timeout_minutes',str(max(1,round(n/60))),t,u['id']))
            if k=='subscription_monthly_amount':
                try: amount=float(vv)
                except (TypeError,ValueError): raise ValueError('Enter a valid monthly subscription amount in KSh.')
                if not (0 < amount <= 10000000): raise ValueError('Monthly subscription amount must be greater than zero and not exceed KSh 10,000,000.')
                vv=f'{amount:.2f}'
            if k in ('allow_credit_sales','require_mpesa_code','dashboard_enabled'):
                vv=vv.upper()
                if vv not in ('YES','NO'): raise ValueError(f'{k} must be YES or NO.')
            if not vv and k in ('business_name','currency','receipt_prefix'): raise ValueError(f'{k} cannot be blank.')
            c.execute('INSERT INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',(k,vv,t,u['id']))
        db.audit(c,u,'UPDATE','SETTINGS','',f'reason={change_reason}; changed_keys={",".join(sorted(set(changed_keys))) or "no direct value changes"}')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
    return get_settings()

def user_from(h, touch=True):
    token=h.headers.get('X-Session-Token','')
    if not token or token not in SESSIONS: return None
    entry=SESSIONS[token]
    if isinstance(entry, dict):
        uid=entry.get('uid'); last=entry.get('last_activity', 0)
    else:
        uid=entry; last=0
    now_ts=dt.datetime.now(dt.timezone.utc).timestamp()
    idle_seconds=max(1,int(float(get_setting('idle_timeout_minutes','2'))*60))
    if last and now_ts-last > idle_seconds:
        try:
            c=db.c(); r=c.execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone()
            if r:
                db.audit(c,dict(r),'AUTO_LOGOUT','USER',uid,'Automatic logout after 2 minutes of inactivity')
                c.commit()
            c.close()
        except Exception:
            pass
        SESSIONS.pop(token,None)
        return None
    if touch:
        SESSIONS[token]={'uid':uid,'last_activity':now_ts}
    c=db.c(); r=c.execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone(); c.close(); return dict(r) if r else None

def role_name(u):
    if not u: return ''
    return str(u.get('access_role') or u.get('role') or 'SALES').upper()

def role_permissions(role):
    c=db.c(); rows=c.execute('SELECT key,enabled FROM role_permissions WHERE role=?',(role,)).fetchall(); c.close(); return {r['key']:bool(r['enabled']) for r in rows}

def effective_permissions(u):
    if not u: return {}
    access=role_name(u)
    c=db.c(); all_keys=[r['key'] for r in c.execute('SELECT key FROM permissions').fetchall()]
    if access=='ADMIN': c.close(); return {k:True for k in all_keys}
    base=role_permissions(access)
    rows=c.execute('SELECT key,enabled FROM user_permissions WHERE user_id=?',(u['id'],)).fetchall()
    for r in rows:
        base[r['key']]=bool(r['enabled'])
    c.close()
    return {k:bool(base.get(k,False)) for k in all_keys}

MANAGEMENT_REPORT_KEYS=set()  # Legacy compatibility; Administrator controls all permissions.

def has_perm(u,key):
    return effective_permissions(u).get(key,False)

def require_perm(h,key):
    u=require(h)
    if role_name(u)=='ADMIN': return u
    if not has_perm(u,key): raise PermissionError('This function is disabled for your account. Contact Administrator.')
    return u

def permissions_payload(admin):
    if role_name(admin)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=db.c(); defs=[dict(r) for r in c.execute('SELECT * FROM permissions ORDER BY label')]
    role_names=('ADMIN','MANAGEMENT','SALES')
    roles={role:{r['key']:bool(r['enabled']) for r in c.execute('SELECT key,enabled FROM role_permissions WHERE role=?',(role,)).fetchall()} for role in role_names}
    users=[dict(r) for r in c.execute("SELECT id,username,full_name,access_role AS role,active FROM users ORDER BY username").fetchall()]
    overrides={str(u['id']):{r['key']:bool(r['enabled']) for r in c.execute('SELECT key,enabled FROM user_permissions WHERE user_id=?',(u['id'],)).fetchall()} for u in users if str(u.get('role'))!='ADMIN'}
    c.close(); return {'definitions':defs,'roles':roles,'users':users,'user_overrides':overrides}

def update_permissions(admin,data):
    if role_name(admin)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=db.c()
    try:
        c.execute('BEGIN')
        for role in ('MANAGEMENT','SALES'):
            for key,val in ((data.get('roles') or {}).get(role,{}) or {}).items():
                if c.execute('SELECT 1 FROM permissions WHERE key=?',(key,)).fetchone():
                    c.execute('UPDATE role_permissions SET enabled=? WHERE role=? AND key=?',(1 if bool(val) else 0,role,key))
        for uid,mapping in ((data.get('users') or {}) or {}).items():
            try: user_id=int(uid)
            except Exception: continue
            user=c.execute('SELECT id,access_role FROM users WHERE id=?',(user_id,)).fetchone()
            if not user or user['access_role']=='ADMIN': continue
            for key,val in (mapping or {}).items():
                if c.execute('SELECT 1 FROM permissions WHERE key=?',(key,)).fetchone():
                    c.execute('INSERT INTO user_permissions(user_id,key,enabled) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET enabled=excluded.enabled',(user_id,key,1 if bool(val) else 0))
        db.audit(c,admin,'UPDATE','PERMISSIONS','',json.dumps(data,ensure_ascii=False)); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
    return permissions_payload(admin)

def require(h, role=None):
    u=user_from(h)
    if not u: raise PermissionError('Please log in.')
    actual=role_name(u)
    if role and actual not in ([role] if isinstance(role,str) else role): raise PermissionError('Administrator permission required.')
    return u

def validate_payments(c, payments, total):
    max_payments=max(1,int(float(get_setting('max_payment_transactions','5'))))
    if len(payments)>max_payments: raise ValueError(f'Payment transactions cannot exceed {max_payments}.')
    if not payments and total>0: raise ValueError('Add at least one payment.')
    seen=set(); paid=Decimal('0')
    for p in payments:
        amt=Decimal(str(p.get('amount',0))).quantize(Decimal('0.01'))
        method=str(p.get('payment_method','')).upper(); code=str(p.get('mpesa_code','')).strip().upper()
        if amt<=0: raise ValueError('Payment amount must be greater than zero.')
        if method not in ('CASH','MPESA'): raise ValueError('Payment method must be Cash or M-Pesa.')
        if method=='MPESA':
            if not code: raise ValueError('M-Pesa transaction code is required for every M-Pesa payment.')
            if code in seen: raise ValueError(f'M-Pesa code {code} is duplicated in this sale.')
            if c.execute('SELECT 1 FROM sale_payments WHERE UPPER(mpesa_code)=?',(code,)).fetchone() or c.execute('SELECT 1 FROM credit_payments WHERE UPPER(mpesa_code)=?',(code,)).fetchone(): raise ValueError(f'M-Pesa code {code} has already been used.')
            seen.add(code)
        else: code=''
        paid += amt
    if paid.quantize(Decimal('0.01')) != Decimal(str(total)).quantize(Decimal('0.01')): raise ValueError(f'Payments must equal sale total. Entered {money(paid)} of {money(total)}.')
    return [{**p,'amount':float(Decimal(str(p['amount'])).quantize(Decimal('0.01'))),'payment_method':str(p['payment_method']).upper(),'mpesa_code':str(p.get('mpesa_code','')).strip().upper()} for p in payments]

def create_sale(u, data):
    cart=data.get('cart') or []; payments=data.get('payments') or []; customer=data.get('customer') or {}; credit=data.get('credit') or None; sale_type=str(data.get('sale_type','RETAIL') or 'RETAIL').upper()
    if sale_type not in ('RETAIL','WHOLESALE'): raise ValueError('Sale type must be Retail or Wholesale.')
    if not cart: raise ValueError('Cart is empty.')
    c=db.c()
    try:
        branch_id=u.get('branch_id')
        if not branch_id and str(u.get('access_role') or u.get('role') or '').upper()=='SALES':
            raise ValueError('Your Sales Person account must be assigned to a branch by Administrator before making sales.')
        if not branch_id:
            branch=c.execute('SELECT id FROM branches WHERE active=1 ORDER BY id LIMIT 1').fetchone()
            if not branch: raise ValueError('No active branch exists. Ask Administrator to register a branch.')
            branch_id=branch['id']
        active_branch=c.execute('SELECT id FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone()
        if not active_branch: raise ValueError('Your assigned branch is inactive. Contact Administrator.')
        total=Decimal('0'); byprod={}; lines=[]
        for item in cart:
            p=c.execute('''SELECT p.*,COALESCE(bs.qty,0) branch_qty,COALESCE(bs.avg_cost,p.avg_cost) branch_avg_cost,
                           COALESCE(bs.sell_price,p.sell_price) branch_sell_price,
                           COALESCE(bs.wholesale_price,p.wholesale_price) branch_wholesale_price
                           FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id AND bs.branch_id=?
                           WHERE p.id=? AND p.active=1''',(branch_id,int(item['product_id']))).fetchone()
            if not p: raise ValueError('Product not found or inactive.')
            q=Decimal(str(item.get('qty',0)))
            if q<=0: raise ValueError('Quantity must be greater than zero.')
            byprod[p['id']]=byprod.get(p['id'],Decimal('0'))+q
            price=float(p['branch_wholesale_price'] or 0) if sale_type=='WHOLESALE' else float(p['branch_sell_price'])
            if price<=0: raise ValueError(f'{sale_type.title()} price is not configured for {p["name"]}. Set it in Product Registration.')
            lt=(q*Decimal(str(price))).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP); total+=lt; lines.append((p,q,price))
        for pid,q in byprod.items():
            p=c.execute('SELECT qty FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,pid)).fetchone()
            if not p or q>Decimal(str(p['qty'])): raise ValueError(f'Insufficient stock for product {pid} in this branch. Available {p["qty"] if p else 0}.')
        c.execute('BEGIN')
        customer_id=None; ctype='WALK_IN'
        if customer.get('id'):
            cr=c.execute('SELECT * FROM customers WHERE id=? AND active=1',(int(customer['id']),)).fetchone()
            if not cr: raise ValueError('Customer not found.')
            if str(u.get('access_role') or u.get('role') or '').upper()=='SALES' and int(cr['branch_id'] or 0)!=int(branch_id):
                raise ValueError('This customer belongs to another branch or has not been assigned to a branch. Ask Administrator to correct the customer record.')
            customer_id=cr['id']; ctype='REGISTERED'
        elif customer.get('full_name') and customer.get('phone'):
            no='CUS-'+now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); ts=now_str(); cur=c.execute('INSERT INTO customers(customer_no,full_name,phone,id_no,address,created_at,updated_at,branch_id) VALUES(?,?,?,?,?,?,?,?)',(no,customer['full_name'].strip(),customer['phone'].strip(),customer.get('id_no','').strip(),customer.get('address','').strip(),ts,ts,branch_id)); customer_id=cur.lastrowid; ctype='REGISTERED'
        receipt=get_setting('receipt_prefix','KAKA')+'-'+now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); ts=now_str()
        credit_payment_mode=False
        if credit:
            if not customer_id: raise ValueError('Credit sales require a registered customer.')
            if not credit.get('due_date'): raise ValueError('Credit due date is required.')
            if not credit.get('collateral'): raise ValueError('Collateral held for exchange is required.')
            dt.date.fromisoformat(credit['due_date']); payments=[]; ctype='CREDIT'; credit_payment_mode=True
        else:
            payments=validate_payments(c,payments,total)
        cur=c.execute('INSERT INTO sales(receipt_no,sold_at,user_id,customer_id,customer_type,total,sale_type,branch_id) VALUES(?,?,?,?,?,?,?,?)',(receipt,ts,u['id'],customer_id,ctype,float(total),sale_type,branch_id)); sid=cur.lastrowid
        current={}
        for p,q,price in lines:
            before=current.get(p['id'],float(c.execute('SELECT qty FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,p['id'])).fetchone()['qty'])); after=before-float(q); current[p['id']]=after
            unit_cost=float(p['branch_avg_cost'] or 0)
            c.execute('INSERT INTO sale_lines(sale_id,product_id,qty,unit_price,unit_cost,cogs_cost_source) VALUES(?,?,?,?,?,?)',(sid,p['id'],float(q),price,unit_cost,'AVERAGE_COST' if unit_cost>0 else 'MISSING_COST'))
            c.execute('UPDATE branch_stock SET qty=?,updated_at=? WHERE branch_id=? AND product_id=?',(after,ts,branch_id,p['id']))
            c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],p['id'],'SALE',float(q),before,after,unit_cost,receipt,branch_id))
        for pmt in payments:
            c.execute('INSERT INTO sale_payments(sale_id,amount,payment_method,mpesa_code,paid_at,user_id) VALUES(?,?,?,?,?,?)',(sid,pmt['amount'],pmt['payment_method'],pmt['mpesa_code'] or None,ts,u['id']))
        if credit_payment_mode:
            c.execute('INSERT INTO credit_accounts(sale_id,customer_id,principal,due_date,collateral,collateral_value,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(sid,customer_id,float(total),credit['due_date'],credit['collateral'],float(credit.get('collateral_value',0) or 0),ts,ts))
        db.audit(c,u,'CREATE','SALE',sid,f'{receipt} total={float(total):.2f} payment_count={len(payments)}')
        c.commit(); return {'receipt_no':receipt,'total':float(total),'sold_at':ts,'sale_id':sid,'customer_type':ctype,'payments':payments}
    except Exception:
        c.rollback(); raise
    finally: c.close()

def products(branch_id=None):
    c=db.c()
    if branch_id:
        rows=c.execute('''SELECT p.*,COALESCE(bs.qty,0) branch_qty,COALESCE(bs.avg_cost,p.avg_cost) branch_cost,
                          COALESCE(bs.sell_price,p.sell_price) branch_retail_price,COALESCE(bs.wholesale_price,p.wholesale_price) branch_wholesale_price,
                          COALESCE(bs.reorder_level,p.reorder_level) branch_reorder
                          FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id AND bs.branch_id=?
                          WHERE p.active=1 ORDER BY p.name''',(int(branch_id),)).fetchall()
    else:
        rows=c.execute('''SELECT p.*,COALESCE(SUM(bs.qty),0) branch_qty,COALESCE(AVG(bs.avg_cost),p.avg_cost) branch_cost
                          FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id WHERE p.active=1 GROUP BY p.id ORDER BY p.name''').fetchall()
    out=[]
    for row in rows:
        item=dict(row); item['stock_qty']=item.get('branch_qty',0); item['avg_cost']=item.get('branch_cost',item.get('avg_cost',0))
        if item.get('branch_retail_price') is not None: item['sell_price']=item['branch_retail_price']
        if item.get('branch_wholesale_price') is not None: item['wholesale_price']=item['branch_wholesale_price']
        if item.get('branch_reorder') is not None: item['reorder_level']=item['branch_reorder']
        out.append(item)
    c.close(); return out

def customers():
    c=db.c(); rows=c.execute('SELECT * FROM customers WHERE active=1 ORDER BY full_name').fetchall(); c.close(); return [dict(r) for r in rows]

def business_performance(start, end, branch_id=None):
    """Return business performance for one branch or the whole company."""
    d=pl_summary(start,end,branch_id)
    revenue=float(d.get('revenue',0) or 0); gross=float(d.get('gross_profit',0) or 0); expenses=float(d.get('expenses',0) or 0); net=float(d.get('net_profit',0) or 0)
    basis=str(get_setting('performance_basis','NET_PROFIT_MARGIN') or 'NET_PROFIT_MARGIN').upper()
    if basis=='GROSS_PROFIT_MARGIN': numerator=gross; pct=(gross/revenue*100.0) if revenue else 0.0; label='Gross Profit Margin'
    elif basis=='SALES_LESS_EXPENSES': numerator=revenue-expenses; pct=(numerator/revenue*100.0) if revenue else 0.0; label='Sales Less Expenses'
    else: numerator=net; pct=(net/revenue*100.0) if revenue else 0.0; label='Net Profit Margin'
    threshold=int(float(get_setting('performance_green_threshold_pct','50') or 50))
    status='PROFIT' if numerator>=0 else 'LOSS'; traffic='GREEN' if status=='PROFIT' and pct>=threshold else 'RED'; score=max(0.0,min(100.0,pct))
    return {'percentage':pct,'score':score,'threshold':threshold,'status':status,'traffic':traffic,'basis':basis,'label':label,'revenue':revenue,'gross_profit':gross,'expenses':expenses,'net_profit':net,'start':start,'end':end}

def dashboard(branch_id=None):
    c=db.c(); today=now().strftime('%Y-%m-%d')
    scope=' AND branch_id=?' if branch_id is not None else ''
    args=(today,int(branch_id)) if branch_id is not None else (today,)
    s=c.execute('SELECT COALESCE(SUM(total),0) v,COUNT(*) n FROM sales WHERE substr(sold_at,1,10)=? AND customer_type<>"CREDIT"'+scope,args).fetchone()
    p=c.execute('SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE substr(expense_date,1,10)=?'+scope,args).fetchone()
    pay_scope=' AND s.branch_id=?' if branch_id is not None else ''
    pay_args=(today,int(branch_id)) if branch_id is not None else (today,)
    mp=c.execute('SELECT COALESCE(SUM(sp.amount),0) v FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id WHERE substr(s.sold_at,1,10)=? AND sp.payment_method="MPESA"'+pay_scope,pay_args).fetchone()
    cash=c.execute('SELECT COALESCE(SUM(sp.amount),0) v FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id WHERE substr(s.sold_at,1,10)=? AND sp.payment_method="CASH"'+pay_scope,pay_args).fetchone()
    cogs=c.execute('SELECT COALESCE(SUM(sl.qty*sl.unit_cost),0) v,COUNT(*) n,SUM(CASE WHEN COALESCE(sl.cogs_cost_source,"RECORDED")="MISSING_COST" OR COALESCE(sl.unit_cost,0)<=0 THEN 1 ELSE 0 END) missing,SUM(CASE WHEN COALESCE(sl.cogs_cost_source,"RECORDED")="ESTIMATED_CURRENT_AVG_COST" THEN 1 ELSE 0 END) estimated FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE substr(s.sold_at,1,10)=?'+pay_scope,pay_args).fetchone()
    stock_scope=' AND bs.branch_id=?' if branch_id is not None else ''
    stock_args=(int(branch_id),) if branch_id is not None else ()
    stock=c.execute('SELECT COALESCE(SUM(bs.qty*bs.avg_cost),0) v,COALESCE(SUM(bs.qty*bs.sell_price),0) retail_v,COALESCE(SUM(bs.qty),0) units,SUM(CASE WHEN bs.qty<=bs.reorder_level THEN 1 ELSE 0 END) low FROM branch_stock bs JOIN products p ON p.id=bs.product_id WHERE p.active=1'+stock_scope,stock_args).fetchone()
    c.close()
    cv=float(cogs['v'] or 0); rev=float(s['v'] or 0)
    return {'today_sales':rev,'transactions':s['n'],'expenses':p['v'],'mpesa':mp['v'],'cash':cash['v'],'today_cogs':cv,'today_gross_profit':rev-cv,'cogs_lines':int(cogs['n'] or 0),'cogs_missing_lines':int(cogs['missing'] or 0),'cogs_estimated_lines':int(cogs['estimated'] or 0),'stock_value':stock['v'],'stock_retail_value':stock['retail_v'],'stock_units':stock['units'],'low_stock':stock['low'] or 0}

def live_monitoring(branch_id=None):
    c=db.c(); nowdt=now(); cutoff=(dt.datetime.now(dt.timezone.utc).timestamp()-int(float(get_setting('idle_timeout_minutes','2'))*60))
    active_users=0
    for v in SESSIONS.values():
        last=v.get('last_activity',0) if isinstance(v,dict) else 0
        if last>=cutoff: active_users+=1
    today=nowdt.strftime('%Y-%m-%d')
    scope=' AND branch_id=?' if branch_id is not None else ''
    args=(today,int(branch_id)) if branch_id is not None else (today,)
    k=c.execute("SELECT COALESCE(SUM(total),0) sales,COUNT(*) transactions FROM sales WHERE substr(sold_at,1,10)=? AND customer_type<>'CREDIT'"+scope,args).fetchone()
    p=c.execute("SELECT COALESCE(SUM(total),0) purchases,COUNT(*) n FROM purchases WHERE substr(purchased_at,1,10)=?"+scope,args).fetchone()
    e=c.execute("SELECT COALESCE(SUM(amount),0) expenses,COUNT(*) n FROM expenses WHERE substr(expense_date,1,10)=?"+scope,args).fetchone()
    pay_scope=' AND s.branch_id=?' if branch_id is not None else ''
    pay_args=(today,int(branch_id)) if branch_id is not None else (today,)
    mp=c.execute("SELECT COALESCE(SUM(amount),0) v FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id WHERE substr(s.sold_at,1,10)=? AND sp.payment_method='MPESA'"+pay_scope,pay_args).fetchone()['v']
    stock_scope=' AND bs.branch_id=?' if branch_id is not None else ''
    low=c.execute("SELECT COUNT(*) n FROM branch_stock bs JOIN products p ON p.id=bs.product_id WHERE p.active=1 AND bs.qty<=bs.reorder_level"+stock_scope,(int(branch_id),) if branch_id is not None else ()).fetchone()['n']
    recent_scope=' WHERE s.branch_id=?' if branch_id is not None else ''
    recent_sales=[dict(r) for r in c.execute("SELECT s.receipt_no,s.sold_at,s.total,COALESCE(u.full_name,'') user_name,COALESCE(cu.full_name,'Walk-in Customer') customer FROM sales s LEFT JOIN users u ON u.id=s.user_id LEFT JOIN customers cu ON cu.id=s.customer_id"+recent_scope+" ORDER BY s.id DESC LIMIT 8",(int(branch_id),) if branch_id is not None else ())]
    recent_purchases=[dict(r) for r in c.execute("SELECT purchase_no,purchased_at,total,supplier,COALESCE(u.full_name,'') user_name FROM purchases p JOIN users u ON u.id=p.user_id"+(" WHERE p.branch_id=?" if branch_id is not None else "")+" ORDER BY p.id DESC LIMIT 8",(int(branch_id),) if branch_id is not None else ())]
    recent_expenses=[dict(r) for r in c.execute("SELECT expense_no,expense_date,amount,category,COALESCE(u.full_name,'') user_name FROM expenses e JOIN users u ON u.id=e.user_id"+(" WHERE e.branch_id=?" if branch_id is not None else "")+" ORDER BY e.id DESC LIMIT 8",(int(branch_id),) if branch_id is not None else ())]
    c.close(); return {'timestamp':now_str(),'active_users':active_users,'today_sales':k['sales'],'transactions':k['transactions'],'today_purchases':p['purchases'],'purchase_count':p['n'],'today_expenses':e['expenses'],'expense_count':e['n'],'mpesa':mp,'low_stock':low,'recent_sales':recent_sales,'recent_purchases':recent_purchases,'recent_expenses':recent_expenses}

def report_data(start,end,branch_id=None):
    c=db.c(); scope=' AND s.branch_id=?' if branch_id is not None else ''
    args=(start,end,int(branch_id)) if branch_id is not None else (start,end)
    rows=c.execute("""SELECT s.id sale_id,s.branch_id,s.sale_type,s.receipt_no,s.sold_at,COALESCE(cu.full_name,'Walk-in Customer') customer,s.customer_type,COALESCE(u.full_name,'') attended_by,COALESCE(SUM(sl.qty),0) units,s.total,COALESCE((SELECT SUM(amount) FROM sale_payments WHERE sale_id=s.id AND payment_method='CASH'),0) cash,COALESCE((SELECT SUM(amount) FROM sale_payments WHERE sale_id=s.id AND payment_method='MPESA'),0) mpesa FROM sales s LEFT JOIN customers cu ON cu.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id LEFT JOIN sale_lines sl ON sl.sale_id=s.id WHERE s.sold_at BETWEEN ? AND ?"""+scope+" GROUP BY s.id ORDER BY s.sold_at DESC",args).fetchall(); c.close(); return [dict(r) for r in rows]

def stock_report(start,end,branch_id=None):
    c=db.c()
    if branch_id is None:
        # All-branch view is an explicit company total, not a shared product quantity.
        ps=c.execute('''SELECT p.*,COALESCE(SUM(bs.qty),0) branch_stock_qty,COALESCE(AVG(bs.avg_cost),p.avg_cost) branch_cost,COALESCE(AVG(bs.sell_price),p.sell_price) branch_sell_price,COALESCE(AVG(bs.wholesale_price),p.wholesale_price) branch_wholesale_price,COALESCE(AVG(bs.reorder_level),p.reorder_level) branch_reorder_level,
                        COALESCE(SUM(bs.qty*bs.avg_cost),0) stock_cost_value
                        FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id
                        WHERE p.active=1 GROUP BY p.id ORDER BY p.name''').fetchall()
    else:
        ps=c.execute('''SELECT p.*,COALESCE(bs.qty,0) branch_stock_qty,COALESCE(bs.avg_cost,p.avg_cost) branch_cost,COALESCE(bs.sell_price,p.sell_price) branch_sell_price,COALESCE(bs.wholesale_price,p.wholesale_price) branch_wholesale_price,COALESCE(bs.reorder_level,p.reorder_level) branch_reorder_level,
                        COALESCE(bs.qty*bs.avg_cost,0) stock_cost_value
                        FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id AND bs.branch_id=?
                        WHERE p.active=1 ORDER BY p.name''',(int(branch_id),)).fetchall()
    out=[]
    totals={'retail_units':0.0,'wholesale_units':0.0,'retail_revenue':0.0,'wholesale_revenue':0.0}
    for p in ps:
        branch_clause=' AND pu.branch_id=?' if branch_id is not None else ''
        purchase_args=(p['id'],start,end,int(branch_id)) if branch_id is not None else (p['id'],start,end)
        pur=c.execute('SELECT COALESCE(SUM(pl.qty),0) q FROM purchase_lines pl JOIN purchases pu ON pu.id=pl.purchase_id WHERE pl.product_id=? AND pu.purchased_at BETWEEN ? AND ?'+branch_clause,purchase_args).fetchone()['q']
        sale_clause=' AND s.branch_id=?' if branch_id is not None else ''
        sale_args=(p['id'],start,end,int(branch_id)) if branch_id is not None else (p['id'],start,end)
        sold=c.execute('SELECT COALESCE(SUM(sl.qty),0) q FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE sl.product_id=? AND s.sold_at BETWEEN ? AND ?'+sale_clause,sale_args).fetchone()['q']
        split=c.execute("SELECT COALESCE(SUM(CASE WHEN s.sale_type='RETAIL' THEN sl.qty ELSE 0 END),0) retail_units, COALESCE(SUM(CASE WHEN s.sale_type='WHOLESALE' THEN sl.qty ELSE 0 END),0) wholesale_units, COALESCE(SUM(CASE WHEN s.sale_type='RETAIL' THEN sl.qty*sl.unit_price ELSE 0 END),0) retail_revenue, COALESCE(SUM(CASE WHEN s.sale_type='WHOLESALE' THEN sl.qty*sl.unit_price ELSE 0 END),0) wholesale_revenue FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE sl.product_id=? AND s.sold_at BETWEEN ? AND ?"+sale_clause,sale_args).fetchone()
        for k in totals: totals[k]+=float(split[k] or 0)
        # Calculate opening and closing from this branch's movement ledger; historical shared stock is never reused.
        movement_filter=' AND branch_id=?' if branch_id is not None else ''
        movement_args=(p['id'],start,int(branch_id)) if branch_id is not None else (p['id'],start)
        opening_net=c.execute("SELECT COALESCE(SUM(CASE WHEN movement_type='SALE' THEN -qty ELSE qty END),0) q FROM stock_movements WHERE product_id=? AND (movement_time<? OR movement_type='OPENING_STOCK')"+movement_filter,movement_args).fetchone()['q']
        adj_clause=' AND branch_id=?' if branch_id is not None else ''
        adj_args=(p['id'],start,end,int(branch_id)) if branch_id is not None else (p['id'],start,end)
        adjustments=c.execute("SELECT COALESCE(SUM(CASE WHEN movement_type IN ('SALE','PURCHASE','OPENING_STOCK') THEN 0 ELSE qty END),0) q FROM stock_movements WHERE product_id=? AND movement_time BETWEEN ? AND ?"+adj_clause,adj_args).fetchone()['q']
        opening=float(opening_net or 0); closing=opening+float(pur or 0)-float(sold or 0)+float(adjustments or 0)
        current_qty=float(p['branch_stock_qty'] or 0)
        unit_cost=float(p['branch_cost'] or 0)
        out.append({'sku':p['sku'],'name':p['name'],'category':p['category'],'stock':current_qty,'opening':opening,'purchased':pur,'sold':sold,'adjustments':float(adjustments or 0),'closing':closing,'sell_price':p['branch_sell_price'],'retail_price':p['branch_sell_price'],'wholesale_price':p['branch_wholesale_price'],'avg_cost':unit_cost,'stock_value':current_qty*unit_cost,'retail_units':float(split['retail_units'] or 0),'wholesale_units':float(split['wholesale_units'] or 0),'retail_revenue':float(split['retail_revenue'] or 0),'wholesale_revenue':float(split['wholesale_revenue'] or 0),'reorder_level':p['branch_reorder_level'],'status':'OUT OF STOCK' if current_qty<=0 else ('LOW STOCK' if current_qty<=p['branch_reorder_level'] else 'OK')})
    for r in out:
        unit_total=r['retail_units']+r['wholesale_units']; rev_total=r['retail_revenue']+r['wholesale_revenue']
        r['retail_units_pct']=100*r['retail_units']/unit_total if unit_total else 0
        r['wholesale_units_pct']=100*r['wholesale_units']/unit_total if unit_total else 0
        r['retail_revenue_pct']=100*r['retail_revenue']/rev_total if rev_total else 0
        r['wholesale_revenue_pct']=100*r['wholesale_revenue']/rev_total if rev_total else 0
    out_totals={**totals,'units_total':totals['retail_units']+totals['wholesale_units'],'revenue_total':totals['retail_revenue']+totals['wholesale_revenue']}
    out_totals['retail_units_pct']=100*totals['retail_units']/out_totals['units_total'] if out_totals['units_total'] else 0
    out_totals['wholesale_units_pct']=100*totals['wholesale_units']/out_totals['units_total'] if out_totals['units_total'] else 0
    out_totals['retail_revenue_pct']=100*totals['retail_revenue']/out_totals['revenue_total'] if out_totals['revenue_total'] else 0
    out_totals['wholesale_revenue_pct']=100*totals['wholesale_revenue']/out_totals['revenue_total'] if out_totals['revenue_total'] else 0
    c.close(); return out


def purchase_report_data(start,end,branch_id=None):
    c=db.c(); scope=' AND p.branch_id=?' if branch_id is not None else ''
    args=(start,end,int(branch_id)) if branch_id is not None else (start,end)
    rows=c.execute("""SELECT p.purchase_no,p.purchased_at,p.supplier,p.payment_method,p.total,COALESCE(u.full_name,'') attended_by,COALESCE(SUM(pl.qty),0) units,p.reference,p.branch_id FROM purchases p JOIN users u ON u.id=p.user_id LEFT JOIN purchase_lines pl ON pl.purchase_id=p.id WHERE p.purchased_at BETWEEN ? AND ?"""+scope+" GROUP BY p.id ORDER BY p.purchased_at DESC",args).fetchall()
    c.close(); return [dict(r) for r in rows]

def expense_report_data(start,end,branch_id=None):
    c=db.c(); scope=' AND e.branch_id=?' if branch_id is not None else ''
    args=(start,end,int(branch_id)) if branch_id is not None else (start,end)
    rows=c.execute("""SELECT e.id expense_id,e.expense_no,e.expense_date,e.category,e.description,e.amount,e.payment_method,e.reference,COALESCE(u.full_name,'') attended_by,e.branch_id FROM expenses e JOIN users u ON u.id=e.user_id WHERE e.expense_date BETWEEN ? AND ?"""+scope+" ORDER BY e.expense_date DESC",args).fetchall()
    c.close(); return [dict(r) for r in rows]

def ledger_data(start,end,branch_id=None):
    c=db.c(); sales_scope=' AND branch_id=?' if branch_id is not None else ''
    args=(start,end,int(branch_id)) if branch_id is not None else (start,end)
    sales=c.execute('SELECT sold_at,receipt_no,total,customer_type FROM sales WHERE sold_at BETWEEN ? AND ?'+sales_scope,args).fetchall()
    purs=c.execute('SELECT purchased_at,purchase_no,total,supplier,payment_method FROM purchases WHERE purchased_at BETWEEN ? AND ?'+sales_scope,args).fetchall()
    ex=c.execute('SELECT expense_date,expense_no,amount,description,payment_method FROM expenses WHERE expense_date BETWEEN ? AND ?'+sales_scope,args).fetchall()
    c.close(); rows=[]
    for r in sales: rows.append([r['sold_at'],r['receipt_no'],'Sales Revenue','Cash / M-Pesa / Accounts Receivable',r['total'],'Sales Revenue',r['total']])
    for r in purs: rows.append([r['purchased_at'],r['purchase_no'],'Inventory Purchase','Inventory',r['total'],'Accounts Payable / Cash / M-Pesa',r['total']])
    for r in ex: rows.append([r['expense_date'],r['expense_no'],r['description'],'Expense',r['amount'],r['payment_method'],r['amount']])
    rows.sort(key=lambda x:x[0],reverse=True); return rows

def pl_summary(start,end,branch_id=None):
    """Profit and loss using sale-time cost snapshots and optional branch scope."""
    c=db.c(); scope=' AND s.branch_id=?' if branch_id is not None else ''
    args=(start,end,int(branch_id)) if branch_id is not None else (start,end)
    try:
        r=c.execute("""SELECT COALESCE(SUM(s.total),0) revenue,
            COALESCE(SUM((SELECT COALESCE(SUM(sl.qty*sl.unit_cost),0) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) cogs
            FROM sales s WHERE s.sold_at BETWEEN ? AND ?"""+scope,args).fetchone()
        expense_scope=' AND branch_id=?' if branch_id is not None else ''
        eargs=(start,end,int(branch_id)) if branch_id is not None else (start,end)
        e=c.execute('SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE expense_date BETWEEN ? AND ?'+expense_scope,eargs).fetchone()
        coverage=c.execute("""SELECT COUNT(*) total_lines,
            SUM(CASE WHEN COALESCE(sl.cogs_cost_source,'RECORDED')='MISSING_COST' OR COALESCE(sl.unit_cost,0)<=0 THEN 1 ELSE 0 END) missing_lines,
            SUM(CASE WHEN COALESCE(sl.cogs_cost_source,'RECORDED')='ESTIMATED_CURRENT_AVG_COST' THEN 1 ELSE 0 END) estimated_lines
            FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE s.sold_at BETWEEN ? AND ?"""+scope,args).fetchone()
    finally: c.close()
    rev=float(r['revenue']); cogs=float(r['cogs']); exp=float(e['v']); gp=rev-cogs; np=gp-exp
    total_lines=int(coverage['total_lines'] or 0); missing_lines=int(coverage['missing_lines'] or 0); estimated_lines=int(coverage['estimated_lines'] or 0)
    return {'revenue':rev,'cogs':cogs,'expenses':exp,'gross_profit':gp,'net_profit':np,'cogs_lines':total_lines,'cogs_missing_lines':missing_lines,'cogs_estimated_lines':estimated_lines,'cogs_coverage_pct':((total_lines-missing_lines-estimated_lines)/total_lines*100.0 if total_lines else 100.0)}

def monthly_ranges(reference=None):
    base=reference or now()
    return base.strftime('%Y-%m')

def xlsx_bytes(title, headers, rows):
    st=get_settings()
    identity=[st.get('business_name',APP_NAME), st.get('business_branch_name',''), st.get('business_phone',''), st.get('business_address',''), st.get('business_website',''), ('KRA PIN: '+st.get('business_tax_pin','')) if st.get('business_tax_pin','') else '', title]
    identity=[x for x in identity if str(x).strip()]
    def cell(v):
        s=esc(v); return f'<c t="inlineStr"><is><t>{s}</t></is></c>'
    xml_rows=[f'<row>'+cell(v)+'</row>' for v in identity]
    xml_rows.append('<row>'+''.join(cell(h) for h in headers)+'</row>')
    for r in rows: xml_rows.append('<row>'+''.join(cell(v) for v in r)+'</row>')
    if st.get('report_footer','').strip(): xml_rows.append('<row>'+cell(st['report_footer'].strip())+'</row>')
    sheet='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml_rows)+'</sheetData></worksheet>'
    wb='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets></workbook>'
    ct='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    rels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    wrels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    bio=io.BytesIO()
    with zipfile.ZipFile(bio,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',ct); z.writestr('_rels/.rels',rels); z.writestr('xl/workbook.xml',wb); z.writestr('xl/_rels/workbook.xml.rels',wrels); z.writestr('xl/worksheets/sheet1.xml',sheet)
    return bio.getvalue()

def simple_pdf(title, headers, rows):
    # Minimal one-page text PDF, intended for report printing/downloading.
    lines=[title, now_str(), ' | '.join(map(str,headers))]
    for r in rows[:55]: lines.append(' | '.join(str(x) for x in r))
    def pobj(n, body): return f'{n} 0 obj\n{body}\nendobj\n'.encode()
    content='BT /F1 8 Tf 36 760 Td 10 TL\n'+' '.join('('+re.sub(r'([\\()])',r'\\\1',str(line)) +') Tj T*\n' for line in lines)+'ET'
    objs=[]; objs.append(pobj(1,'<< /Type /Catalog /Pages 2 0 R >>')); objs.append(pobj(2,'<< /Type /Pages /Kids [3 0 R] /Count 1 >>')); objs.append(pobj(3,'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>')); objs.append(pobj(4,'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')); objs.append(pobj(5,f'<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream'))
    out=io.BytesIO(); out.write(b'%PDF-1.4\n'); offsets=[0]
    for o in objs: offsets.append(out.tell()); out.write(o)
    x=out.tell(); out.write(f'xref\n0 {len(objs)+1}\n0000000000 65535 f \n'.encode());
    for off in offsets[1:]: out.write(f'{off:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF'.encode()); return out.getvalue()


