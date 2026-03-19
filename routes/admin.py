from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from models import User, Module, ServerConnection, db
from functools import wraps
import pyodbc

bp = Blueprint('admin', __name__)

# Custom decorator to ensure only admins access these routes
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash('You do not have permission to access that page.', 'danger')
            return redirect(url_for('portal.dashboard'))
        return f(*args, **kwargs)
    return decorated_function

@bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    users_count = User.query.count()
    modules_count = Module.query.count()
    connections_count = ServerConnection.query.count()
    return render_template('admin/dashboard.html', 
                         users_count=users_count, 
                         modules_count=modules_count,
                         connections_count=connections_count)

@bp.route('/connections')
@login_required
@admin_required
def connections():
    all_connections = ServerConnection.query.all()
    return render_template('admin/connections.html', connections=all_connections)

@bp.route('/connections/create', methods=['POST'])
@login_required
@admin_required
def create_connection():
    name = request.form.get('name')
    server_type = request.form.get('server_type')
    host = request.form.get('host')
    username = request.form.get('username')
    password = request.form.get('password')
    
    existing = ServerConnection.query.filter_by(name=name).first()
    if existing:
        flash(f'A connection with the name "{name}" already exists.', 'danger')
        return redirect(url_for('admin.connections'))
        
    new_conn = ServerConnection(
        name=name, 
        server_type=server_type, 
        host=host, 
        username=username, 
        password=password
    )
    db.session.add(new_conn)
    db.session.commit()
    flash('Server Connection created successfully.', 'success')
    return redirect(url_for('admin.connections'))

@bp.route('/api/connections/<int:conn_id>/databases')
@login_required
@admin_required
def get_databases(conn_id):
    conn = ServerConnection.query.get_or_404(conn_id)
    if conn.server_type != 'sqlserver':
        return jsonify({'error': 'Only SQL Server supports dynamic DB fetching right now'}), 400
        
    try:
        # Use ODBC Driver 18 for SQL Server with Encrypt=no to bypass forced TLS handshakes on older servers
        conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={conn.host};UID={conn.username};PWD={conn.password};Encrypt=no;TrustServerCertificate=no;"
        odbc_conn = pyodbc.connect(conn_str, autocommit=True)
        cursor = odbc_conn.cursor()
        cursor.execute("SELECT name FROM sys.databases WHERE state_desc = 'ONLINE'")
        databases = [row.name for row in cursor.fetchall()]
        cursor.close()
        odbc_conn.close()
        return jsonify({'databases': databases})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/modules')
@login_required
@admin_required
def modules():
    all_modules = Module.query.all()
    all_connections = ServerConnection.query.all()
    return render_template('admin/modules.html', modules=all_modules, connections=all_connections)

@bp.route('/modules/create', methods=['POST'])
@login_required
@admin_required
def create_module():
    name = request.form.get('name')
    desc = request.form.get('description')
    mod_type = request.form.get('type')
    
    new_module = Module(name=name, description=desc)
    
    if mod_type == 'custom':
        new_module.custom_script_path = request.form.get('custom_script_path')
    else:
        new_module.connection_id = request.form.get('connection_id')
        new_module.object_type = request.form.get('object_type') # 'sp' or 'job'
        new_module.database_name = request.form.get('database_name')
        new_module.stored_proc_name = request.form.get('stored_proc_name')
        new_module.parameters_json = request.form.get('parameters_json')
        
    db.session.add(new_module)
    db.session.commit()
    
    flash(f'Module "{name}" created successfully.', 'success')
    return redirect(url_for('admin.modules'))

@bp.route('/users')
@login_required
@admin_required
def users():
    all_users = User.query.all()
    all_modules = Module.query.all()
    return render_template('admin/users.html', users=all_users, modules=all_modules)

@bp.route('/users/<int:user_id>/permissions', methods=['POST'])
@login_required
@admin_required
def update_user_permissions(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin():
        flash('Cannot modify permissions for an Admin user.', 'warning')
        return redirect(url_for('admin.users'))
        
    module_ids = request.form.getlist('module_ids')
    
    # Clear existing permissions and rebuild
    user.modules.clear()
    for m_id in module_ids:
        module = Module.query.get(int(m_id))
        if module:
            user.modules.append(module)
            
    db.session.commit()
    flash(f'Permissions updated for {user.email}.', 'success')
    return redirect(url_for('admin.users'))

@bp.route('/users/<int:user_id>/toggle_admin', methods=['POST'])
@login_required
@admin_required
def toggle_user_admin(user_id):
    user = User.query.get_or_404(user_id)
    
    if user.id == current_user.id:
        flash('You cannot revoke your own admin privileges.', 'danger')
        return redirect(url_for('admin.users'))
        
    if user.is_admin():
        user.role = 'user'
        flash(f'Admin privileges revoked for {user.email}.', 'warning')
    else:
        user.role = 'admin'
        # Clear specific module assignments as admins get access to all
        user.modules.clear()
        flash(f'User {user.email} promoted to Admin.', 'success')
        
    db.session.commit()
    return redirect(url_for('admin.users'))
