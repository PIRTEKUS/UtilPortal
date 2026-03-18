from app import create_app
from models import db, User
import os

app = create_app()

with app.app_context():
    # Construct the database if it doesn't already exist
    db.create_all()
    
    # Check if we need to create the default admin user
    if not User.query.filter_by(email='admin@utilportal.local').first():
        admin = User(email='admin@utilportal.local', role='admin')
        admin.set_password('admin')
        db.session.add(admin)
        db.session.commit()
        print("Database initialized and default Admin 'admin@utilportal.local' created (password: admin).")
    else:
        print("Database initialized. Admin user already exists.")
