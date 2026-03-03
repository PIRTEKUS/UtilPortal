from flask import Flask, redirect, url_for
from config import Config
from models import db, User
from flask_login import LoginManager
import os

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    
    login = LoginManager(app)
    login.login_view = 'auth.login'
    
    @login.user_loader
    def load_user(id):
        return db.session.get(User, int(id))

    # Register Blueprints
    from routes.auth import bp as auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')
    
    from routes.admin import bp as admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')
    
    from routes.portal import bp as portal_bp
    app.register_blueprint(portal_bp, url_prefix='/portal')

    # Main entry point redirects to login for now
    @app.route('/')
    def index():
        return redirect(url_for('auth.login'))

    return app

if __name__ == '__main__':
    # Ensure database tables are created on first run
    app = create_app()
    with app.app_context():
        db.create_all()
        # Optionally create a default admin user if none exists
        if not User.query.filter_by(email='admin@utilportal.local').first():
            admin = User(email='admin@utilportal.local', role='admin')
            admin.set_password('admin')
            db.session.add(admin)
            db.session.commit()
            print("Default admin created: admin@utilportal.local / admin")
            
    app.run(debug=True)
