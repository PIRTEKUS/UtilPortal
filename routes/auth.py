from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user
from urllib.parse import urlsplit
from models import db, User

bp = Blueprint('auth', __name__)

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('portal.dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if user is None or not user.check_password(password):
            flash('Invalid email or password', 'danger')
            return redirect(url_for('auth.login'))
            
        login_user(user, remember=request.form.get('remember_me'))
        
        next_page = request.args.get('next')
        if not next_page or urlsplit(next_page).netloc != '':
            if user.is_admin():
                next_page = url_for('admin.dashboard')
            else:
                next_page = url_for('portal.dashboard')
                
        return redirect(next_page)
        
    return render_template('auth/login.html', title='Sign In')

@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


# --- PLACEHOLDER FOR FUTURE SSO ---
@bp.route('/login/sso')
def login_sso():
    # Redirect to Microsoft Entra ID Authorization URL
    flash('SSO Login is not yet implemented.', 'info')
    return redirect(url_for('auth.login'))

@bp.route('/login/sso/callback')
def login_sso_callback():
    # Handle token exchange and user creation/login
    pass
