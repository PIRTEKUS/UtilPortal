import msal
import uuid
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, session
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

def _build_msal_app():
    return msal.ConfidentialClientApplication(
        current_app.config['AZURE_CLIENT_ID'],
        authority=current_app.config.get('AZURE_AUTHORITY'),
        client_credential=current_app.config['AZURE_CLIENT_SECRET']
    )

@bp.route('/login/sso')
def login_sso():
    if not current_app.config.get('AZURE_CLIENT_ID'):
        flash('SSO Login is not configured. Please check your environment variables.', 'warning')
        return redirect(url_for('auth.login'))

    session["state"] = str(uuid.uuid4())
    
    msal_app = _build_msal_app()
    auth_url = msal_app.get_authorization_request_url(
        scopes=[],
        state=session["state"],
        redirect_uri=url_for("auth.login_sso_callback", _external=True)
    )
    return redirect(auth_url)

@bp.route('/login/sso/callback')
def login_sso_callback():
    if request.args.get('state') != session.get("state"):
        flash("State mismatch error. Please try logging in again.", "danger")
        return redirect(url_for("auth.login"))
        
    if "error" in request.args:
        flash(f"SSO Error: {request.args.get('error_description')}", "danger")
        return redirect(url_for("auth.login"))
        
    if "code" not in request.args:
        flash("No authorization code received.", "danger")
        return redirect(url_for("auth.login"))
        
    msal_app = _build_msal_app()
    result = msal_app.acquire_token_by_authorization_code(
        request.args["code"],
        scopes=[],
        redirect_uri=url_for("auth.login_sso_callback", _external=True)
    )
    
    if "error" in result:
        flash(f"SSO Error: {result.get('error_description', 'Unknown error')}", "danger")
        return redirect(url_for("auth.login"))
        
    claims = result.get("id_token_claims")
    if not claims:
        flash("Could not retrieve user claims from SSO.", "danger")
        return redirect(url_for("auth.login"))
        
    email = claims.get('preferred_username') or claims.get('email')
    if not email:
        flash("SSO provider did not return an email address.", "danger")
        return redirect(url_for("auth.login"))
        
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, role='user') # default role
        db.session.add(user)
        db.session.commit()
        
    login_user(user)
    
    if user.is_admin():
        return redirect(url_for('admin.dashboard'))
    return redirect(url_for('portal.dashboard'))
