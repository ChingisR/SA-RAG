import psycopg2
import bcrypt
import os

DSN = os.getenv("POSTGRES_URL", "postgresql://admin:secret@postgres:5432/universal_rag_db")
conn = psycopg2.connect(DSN)
cur = conn.cursor()
hashed = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode("utf-8")
cur.execute("UPDATE users SET password_hash = %s WHERE email = 'admin@enterprise.com'", (hashed,))
conn.commit()
cur.close()
conn.close()
print("Password updated successfully!")
