#!/usr/bin/env python3
"""
Credentials Reset & Verification Utility for Fresh Trade Engine
Run this script to guarantee default administrator & trader credentials are reset and active.
"""

import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from backend.database.connection import init_db, SessionLocal, engine
from backend.database.models import User, Account
from backend.core.auth import hash_user_password, verify_user_password

def reset_all_credentials():
    print("=" * 60)
    print(">>> FRESH TRADE ENGINE: USER CREDENTIALS RESET & VERIFY <<<")
    print("=" * 60)

    # Initialize DB and schema
    init_db()

    db = SessionLocal()
    try:
        # 1. Reset Admin
        admin = db.query(User).filter(User.username.ilike("admin")).first()
        if not admin:
            admin = User(
                username="admin",
                full_name="System Administrator",
                email="admin@fresh-trade.local",
                password_hash=hash_user_password("admin123"),
                role="ADMIN",
                is_active=True
            )
            db.add(admin)
            print("[+] Created default 'admin' user.")
        else:
            admin.role = "ADMIN"
            admin.is_active = True
            admin.password_hash = hash_user_password("admin123")
            print("[*] Updated 'admin' user: password reset to 'admin123' (Active: True, Role: ADMIN)")

        # 2. Reset Trader
        trader = db.query(User).filter(User.username.ilike("trader")).first()
        if not trader:
            trader = User(
                username="trader",
                full_name="Standard Trader",
                email="trader@fresh-trade.local",
                password_hash=hash_user_password("trader123"),
                role="USER",
                is_active=True
            )
            db.add(trader)
            print("[+] Created default 'trader' user.")
        else:
            trader.role = "USER"
            trader.is_active = True
            trader.password_hash = hash_user_password("trader123")
            print("[*] Updated 'trader' user: password reset to 'trader123' (Active: True, Role: USER)")

        db.commit()

        # Verify password verification works
        assert verify_user_password("admin123", admin.password_hash), "admin123 verification check failed"
        assert verify_user_password("trader123", trader.password_hash), "trader123 verification check failed"

        print("-" * 60)
        print("DATABASE USERS SUMMARY:")
        users = db.query(User).all()
        for u in users:
            print(f"  - ID: {u.id:<3} | Username: {u.username:<12} | Role: {u.role:<6} | Active: {u.is_active}")
        
        print("-" * 60)
        print("DEFAULT CREDENTIALS:")
        print("  1. Administrator:   username: admin    password: admin123   (Full Control)")
        print("  2. Normal Trader:   username: trader   password: trader123  (Client Trading)")
        print("=" * 60)
        print("[SUCCESS] Credentials verified and ready for live login!")
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Failed to reset credentials: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    reset_all_credentials()

