import psycopg2
import os
import time
import bcrypt
# Passlib is deprecated in Python 3.12, using direct bcrypt monkey patch
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

# Use the DSN from main.py
DSN = os.getenv("POSTGRES_URL")
if not DSN:
    raise RuntimeError("FATAL: POSTGRES_URL environment variable is not set.")

def init_db():
    print("Initializing Database...")
    try:
        conn = psycopg2.connect(DSN)
        cur = conn.cursor()
        
        # 1. Ensure Users Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)
        
        # Seed default admin ONLY if the user doesn't already exist.
        # Never use ON CONFLICT DO UPDATE here — that would silently wipe
        # any password changes made by real admins after first deployment.
        admin_email = os.getenv("ADMIN_EMAIL", "admin@enterprise.com")
        admin_password = os.getenv("ADMIN_DEFAULT_PASSWORD", "admin123")
        cur.execute("SELECT COUNT(*) FROM users WHERE email = %s", (admin_email,))
        if cur.fetchone()[0] == 0:
            default_pw_hash = hash_password(admin_password)
            cur.execute("""
                INSERT INTO users (email, password_hash, role)
                VALUES (%s, %s, 'admin')
                ON CONFLICT (email) DO NOTHING;
            """, (admin_email, default_pw_hash))
            print(f"✅ Default admin user '{admin_email}' created.")
        else:
            print(f"ℹ️ Admin user '{admin_email}' already exists, skipping seed.")
        
        # 2. Ensure HR Employees Table
        # NOTE: Do NOT drop this table on restart — real HR data must survive.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS hr_employees (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            department VARCHAR(255),
            role VARCHAR(255),
            salary NUMERIC(10, 2),
            hire_date DATE,
            email VARCHAR(255) UNIQUE
        );
        """)
        
        # Seed sample HR data for Chingis and others to test pipelines
        cur.execute("""
            INSERT INTO hr_employees (name, department, role, salary, hire_date, email) 
            VALUES 
            ('Chingis Rustemov', 'AI Division', 'AI Architect', 185000.00, '2023-01-15', 'chingis@enterprise.com'),
            ('Elena Vazquez', 'Legal', 'Senior Counsel', 145000.00, '2021-11-01', 'elena.v@enterprise.com'),
            ('Arthur Anderson', 'HR', 'HR Director', 130000.00, '2020-05-20', 'arthur.a@enterprise.com')
            ON CONFLICT (email) DO UPDATE SET 
                salary = EXCLUDED.salary, 
                role = EXCLUDED.role;
        """)
        print("✅ HR Employees table initialized and seeded.")
        
        conn.commit()
        print("✅ Database Tables Initialized (Users & Admin).")
    except Exception as e:
        print(f"❌ DB Init Error: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    init_db()