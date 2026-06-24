from app import create_app
from models import db, User, Module
from sqlalchemy import text

app = create_app()

with app.app_context():
    print("Starting database migration...")
    
    # 1. Create all new tables (AppSetting, Role, Folder, and association tables)
    # SQLAlchemy's create_all will only create the tables that don't exist yet.
    db.create_all()
    print("Created new tables (if they were missing).")

    # 2. Add missing columns to the existing 'module' table
    # We use 'try/except' in case some columns were already added.
    columns_to_add = [
        "ALTER TABLE module ADD COLUMN folder_id INT",
        "ALTER TABLE module ADD COLUMN custom_code TEXT",
        "ALTER TABLE module ADD COLUMN is_python_folder BOOLEAN DEFAULT FALSE",
        "ALTER TABLE module ADD COLUMN python_entry_file VARCHAR(255)",
        "ALTER TABLE audit_log ADD COLUMN end_time DATETIME NULL",
        "ALTER TABLE audit_log ADD COLUMN pid INT NULL",
        "ALTER TABLE audit_log ADD COLUMN result_data TEXT NULL",
        "ALTER TABLE audit_log ADD COLUMN notified BOOLEAN DEFAULT 0",
    ]

    for cmd in columns_to_add:
        try:
            db.session.execute(text(cmd))
            db.session.commit()
            print(f"Executed: {cmd}")
        except Exception as e:
            db.session.rollback()
            # If the column already exists, we just skip it
            if "Duplicate column name" in str(e) or "already exists" in str(e).lower():
                print(f"Skipped (already exists): {cmd}")
            else:
                print(f"Error executing {cmd}: {e}")

    # 3. For MySQL production databases, modify column types to LONGTEXT
    try:
        dialect_name = db.engine.dialect.name
        if dialect_name == 'mysql':
            print("Detected MySQL database. Ensuring result_data and message columns are LONGTEXT...")
            mysql_cmds = [
                "ALTER TABLE audit_log MODIFY COLUMN result_data LONGTEXT NULL",
                "ALTER TABLE audit_log MODIFY COLUMN message LONGTEXT NULL"
            ]
            for cmd in mysql_cmds:
                try:
                    db.session.execute(text(cmd))
                    db.session.commit()
                    print(f"Executed: {cmd}")
                except Exception as e:
                    db.session.rollback()
                    print(f"Error executing {cmd}: {e}")
    except Exception as dialect_err:
        print(f"Could not check dialect or modify MySQL columns: {dialect_err}")

    print("Migration complete!")
