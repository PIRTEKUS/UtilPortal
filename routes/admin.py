import json
import os
import zipfile
import shutil
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import User, Module, ServerConnection, db, Role, Folder, AppSetting, AuditLog
from functools import wraps
import pyodbc

bp = Blueprint('admin', __name__)

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

# --- ACTIVITY LOG ---
@bp.route('/activity')
@login_required
@admin_required
def activity():
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(200).all()
    return render_template('admin/activity.html', logs=logs)

@bp.route('/activity/<int:log_id>/stop', methods=['POST'])
@login_required
@admin_required
def stop_activity(log_id):
    import signal
    log = AuditLog.query.get_or_404(log_id)
    if log.status != 'running':
        flash('This execution is no longer running.', 'info')
        return redirect(url_for('admin.activity'))
        
    if not log.pid:
        flash('Cannot stop this execution (no PID recorded).', 'danger')
        return redirect(url_for('admin.activity'))
        
    try:
        # Kill the process
        os.kill(log.pid, signal.SIGTERM)
        
        log.status = 'error'
        log.message = 'Execution forcefully stopped by admin.'
        from datetime import datetime, timezone as tz
        log.end_time = datetime.now(tz.utc)
        db.session.commit()
        flash(f'Execution {log_id} has been forcefully stopped.', 'success')
    except ProcessLookupError:
        log.status = 'error'
        log.message = 'Process had already terminated.'
        db.session.commit()
        flash('Process was already terminated.', 'info')
    except Exception as e:
        flash(f'Error stopping process: {str(e)}', 'danger')
        
    return redirect(url_for('admin.activity'))

@bp.route('/activity/<int:log_id>/details')
@login_required
@admin_required
def activity_details(log_id):
    log = AuditLog.query.get_or_404(log_id)
    
    # Calculate duration
    duration = None
    if log.end_time and log.timestamp:
        delta = (log.end_time - log.timestamp).total_seconds()
        duration = f"{int(delta)}s"
        if delta >= 60:
            duration = f"{int(delta // 60)}m {int(delta % 60)}s"
            if delta >= 3600:
                duration = f"{int(delta // 3600)}h {int((delta % 3600) // 60)}m {int(delta % 60)}s"
                
    # Determine module type
    module_type = "Generic SQL"
    if log.module:
        if log.module.custom_code or log.module.is_python_folder:
            module_type = "Python Script"
        elif log.module.object_type == 'sp':
            module_type = "Stored Procedure"
        elif log.module.object_type == 'job':
            module_type = "SQL Server Job"

    # Connection details
    conn_name = None
    conn_host = None
    if log.module and log.module.connection:
        conn_name = log.module.connection.name
        conn_host = log.module.connection.host

    # Safe parameter parsing
    params = {}
    if log.parameters_used:
        try:
            params = json.loads(log.parameters_used)
        except Exception:
            params = log.parameters_used

    return jsonify({
        'id': log.id,
        'status': log.status,
        'module_name': log.module.name if log.module else '—',
        'module_id': log.module_id,
        'module_type': module_type,
        'user_email': log.user.email if log.user else '—',
        'pid': log.pid,
        'started': log.timestamp.strftime('%Y-%m-%d %H:%M:%S') if log.timestamp else '—',
        'finished': log.end_time.strftime('%Y-%m-%d %H:%M:%S') if log.end_time else ('In progress…' if log.status == 'running' else '—'),
        'duration': duration or ('—' if log.status == 'running' else '0s'),
        'parameters': params,
        'message': log.message or '',
        'connection_name': conn_name,
        'connection_host': conn_host,
        'has_results': log.result_data is not None
    })

# --- CONNECTIONS ---
@bp.route('/connections')
@login_required
@admin_required
def connections():
    return render_template('admin/connections.html', connections=ServerConnection.query.all())

@bp.route('/connections/create', methods=['POST'])
@login_required
@admin_required
def create_connection():
    name = request.form.get('name')
    existing = ServerConnection.query.filter_by(name=name).first()
    if existing:
        flash(f'A connection with the name "{name}" already exists.', 'danger')
        return redirect(url_for('admin.connections'))
        
    new_conn = ServerConnection(
        name=name, 
        server_type=request.form.get('server_type'), 
        host=request.form.get('host'), 
        username=request.form.get('username'), 
        password=request.form.get('password')
    )
    db.session.add(new_conn)
    db.session.commit()
    flash('Server Connection created successfully.', 'success')
    return redirect(url_for('admin.connections'))

@bp.route('/connections/<int:conn_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_connection(conn_id):
    conn = ServerConnection.query.get_or_404(conn_id)
    conn.name = request.form.get('name')
    conn.server_type = request.form.get('server_type')
    conn.host = request.form.get('host')
    conn.username = request.form.get('username')
    # Only update password if a new one was provided
    new_password = request.form.get('password', '').strip()
    if new_password:
        conn.password = new_password
    db.session.commit()
    flash(f'Connection "{conn.name}" updated successfully.', 'success')
    return redirect(url_for('admin.connections'))

@bp.route('/api/connections/<int:conn_id>/databases')
@login_required
@admin_required
def get_databases(conn_id):
    conn = ServerConnection.query.get_or_404(conn_id)
    if conn.server_type != 'sqlserver':
        return jsonify({'error': 'Only SQL Server supports dynamic DB fetching right now'}), 400
    try:
        conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={conn.host};UID={conn.username};PWD={conn.password};Encrypt=no;TrustServerCertificate=no;PacketSize=1024;KeepAlive=30;KeepAliveInterval=1;"
        odbc_conn = pyodbc.connect(conn_str, autocommit=True)
        cursor = odbc_conn.cursor()
        cursor.execute("SELECT name FROM sys.databases WHERE state_desc = 'ONLINE'")
        databases = [row.name for row in cursor.fetchall()]
        cursor.close()
        odbc_conn.close()
        return jsonify({'databases': databases})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- ROLES ---
@bp.route('/roles', methods=['GET', 'POST'])
@login_required
@admin_required
def roles():
    if request.method == 'POST':
        name = request.form.get('name')
        desc = request.form.get('description')
        if not Role.query.filter_by(name=name).first():
            db.session.add(Role(name=name, description=desc))
            db.session.commit()
            flash('Role created successfully.', 'success')
        else:
            flash('Role already exists.', 'danger')
        return redirect(url_for('admin.roles'))
        
    all_roles = Role.query.all()
    all_modules = Module.query.all()
    all_folders = Folder.query.all()
    return render_template('admin/roles.html', roles=all_roles, modules=all_modules, folders=all_folders)

@bp.route('/roles/edit/<int:role_id>', methods=['POST'])
@login_required
@admin_required
def edit_role(role_id):
    role = Role.query.get_or_404(role_id)
    role.name = request.form.get('name')
    role.description = request.form.get('description')
    
    role.modules.clear()
    for m_id in request.form.getlist('module_ids'):
        m = Module.query.get(int(m_id))
        if m: role.modules.append(m)
        
    role.folders.clear()
    for f_id in request.form.getlist('folder_ids'):
        f = Folder.query.get(int(f_id))
        if f: role.folders.append(f)
        
    db.session.commit()
    flash(f'Role "{role.name}" updated.', 'success')
    return redirect(url_for('admin.roles'))

# --- FOLDERS ---
def get_folders_tree_list():
    all_folders = Folder.query.all()
    from collections import defaultdict
    by_parent = defaultdict(list)
    for f in all_folders:
        by_parent[f.parent_id].append(f)
        
    for pid in by_parent:
        by_parent[pid].sort(key=lambda x: (x.display_order, x.name))
        
    flat_list = []
    def traverse(parent_id, depth):
        for f in by_parent[parent_id]:
            f.depth = depth
            flat_list.append(f)
            traverse(f.id, depth + 1)
            
    traverse(None, 0)
    return flat_list

@bp.route('/folders', methods=['GET', 'POST'])
@login_required
@admin_required
def folders():
    if request.method == 'POST':
        name = request.form.get('name')
        parent_id = request.form.get('parent_id') or None
        
        # Default order to be last among siblings
        siblings_count = Folder.query.filter_by(parent_id=parent_id).count()
        db.session.add(Folder(name=name, parent_id=parent_id, display_order=siblings_count))
        db.session.commit()
        flash('Folder created successfully.', 'success')
        return redirect(url_for('admin.folders'))
        
    all_folders_tree = get_folders_tree_list()
    return render_template('admin/folders.html', folders=all_folders_tree)

@bp.route('/folders/edit/<int:folder_id>', methods=['POST'])
@login_required
@admin_required
def edit_folder(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    folder.name = request.form.get('name')
    parent_id = request.form.get('parent_id')
    folder.parent_id = parent_id if parent_id else None
    db.session.commit()
    flash(f'Folder "{folder.name}" updated.', 'success')
    return redirect(url_for('admin.folders'))

@bp.route('/folders/<int:folder_id>/move/<direction>', methods=['POST'])
@login_required
@admin_required
def move_folder(folder_id, direction):
    folder = Folder.query.get_or_404(folder_id)
    siblings = Folder.query.filter_by(parent_id=folder.parent_id).order_by(Folder.display_order, Folder.name).all()
    
    idx = -1
    for i, s in enumerate(siblings):
        if s.id == folder.id:
            idx = i
            break
            
    if idx != -1:
        # Normalize sibling display orders first
        for i, s in enumerate(siblings):
            s.display_order = i
        db.session.commit()
        
        if direction == 'up' and idx > 0:
            siblings[idx].display_order = idx - 1
            siblings[idx - 1].display_order = idx
            db.session.commit()
            flash(f'Folder "{folder.name}" moved up.', 'success')
        elif direction == 'down' and idx < len(siblings) - 1:
            siblings[idx].display_order = idx + 1
            siblings[idx + 1].display_order = idx
            db.session.commit()
            flash(f'Folder "{folder.name}" moved down.', 'success')
            
    return redirect(url_for('admin.folders'))

# --- MODULES ---
def get_sorted_modules():
    def get_folder_path(folder):
        path = []
        curr = folder
        while curr:
            path.insert(0, curr.name)
            curr = curr.parent
        return path

    root_modules = Module.query.filter_by(folder_id=None).order_by(Module.display_order, Module.name).all()
    for m in root_modules:
        m.depth = 0
        m.folder_prefix = ""
        
    sorted_modules = list(root_modules)
    
    all_folders = Folder.query.all()
    from collections import defaultdict
    by_parent = defaultdict(list)
    for f in all_folders:
        by_parent[f.parent_id].append(f)
        
    for pid in by_parent:
        by_parent[pid].sort(key=lambda x: (x.display_order, x.name))
        
    def traverse_folder(folder_id, depth):
        for folder in by_parent[folder_id]:
            folder_modules = Module.query.filter_by(folder_id=folder.id).order_by(Module.display_order, Module.name).all()
            for m in folder_modules:
                m.depth = depth + 1
                m.folder_prefix = " / ".join(get_folder_path(folder))
                sorted_modules.append(m)
            traverse_folder(folder.id, depth + 1)
            
    traverse_folder(None, 0)
    return sorted_modules

@bp.route('/modules')
@login_required
@admin_required
def modules():
    all_modules = get_sorted_modules()
    all_connections = ServerConnection.query.all()
    all_folders = get_folders_tree_list()
    return render_template('admin/modules.html', modules=all_modules, connections=all_connections, folders=all_folders)

@bp.route('/modules/<int:module_id>/move/<direction>', methods=['POST'])
@login_required
@admin_required
def move_module(module_id, direction):
    module = Module.query.get_or_404(module_id)
    siblings = Module.query.filter_by(folder_id=module.folder_id).order_by(Module.display_order, Module.name).all()
    
    idx = -1
    for i, s in enumerate(siblings):
        if s.id == module.id:
            idx = i
            break
            
    if idx != -1:
        # Normalize sibling display orders first
        for i, s in enumerate(siblings):
            s.display_order = i
        db.session.commit()
        
        if direction == 'up' and idx > 0:
            siblings[idx].display_order = idx - 1
            siblings[idx - 1].display_order = idx
            db.session.commit()
            flash(f'Module "{module.name}" moved up.', 'success')
        elif direction == 'down' and idx < len(siblings) - 1:
            siblings[idx].display_order = idx + 1
            siblings[idx + 1].display_order = idx
            db.session.commit()
            flash(f'Module "{module.name}" moved down.', 'success')
            
    return redirect(url_for('admin.modules'))

@bp.route('/modules/create', methods=['POST'])
@login_required
@admin_required
def create_module():
    folder_id_val = request.form.get('folder_id') or None
    if folder_id_val:
        folder_id_val = int(folder_id_val)
        
    new_module = Module(
        name=request.form.get('name'), 
        description=request.form.get('description'),
        folder_id=folder_id_val
    )
    # Default order to be last in folder
    siblings_count = Module.query.filter_by(folder_id=folder_id_val).count()
    new_module.display_order = siblings_count
    mod_type = request.form.get('type')
    
    # Process parameters_json for all module types
    raw_params_json = request.form.get('parameters_json', '').strip()
    if raw_params_json:
        try:
            json.loads(raw_params_json)  # validate before storing
            new_module.parameters_json = raw_params_json
        except json.JSONDecodeError as je:
            flash(f'Parameters JSON is invalid: {je}. Module not saved.', 'danger')
            return redirect(url_for('admin.modules'))
    else:
        new_module.parameters_json = None
        
    if mod_type == 'custom':
        new_module.custom_code = request.form.get('custom_code')
        # Handle zip upload
        if 'zip_file' in request.files and request.files['zip_file'].filename:
            file = request.files['zip_file']
            filename = secure_filename(file.filename)
            zip_path = os.path.join('instance', filename)
            file.save(zip_path)
            new_module.is_python_folder = True
            new_module.python_entry_file = request.form.get('python_entry_file') or 'main.py'
            
            db.session.add(new_module)
            db.session.commit()
            
            # Extract
            extract_dir = os.path.join('instance', 'modules_data', str(new_module.id))
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            os.remove(zip_path)
            flash(f'Module "{new_module.name}" created from ZIP successfully.', 'success')
            return redirect(url_for('admin.modules'))
    else:
        new_module.connection_id = request.form.get('connection_id')
        new_module.object_type = request.form.get('object_type')
        new_module.database_name = request.form.get('database_name')
        new_module.stored_proc_name = request.form.get('stored_proc_name')

    db.session.add(new_module)
    db.session.commit()
    flash(f'Module "{new_module.name}" created successfully.', 'success')
    return redirect(url_for('admin.modules'))

@bp.route('/modules/edit/<int:module_id>', methods=['POST'])
@login_required
@admin_required
def edit_module(module_id):
    module = Module.query.get_or_404(module_id)
    module.name = request.form.get('name')
    module.description = request.form.get('description')
    
    # Adjust display_order if the folder changes
    old_folder_id = module.folder_id
    new_folder_id = request.form.get('folder_id') or None
    if new_folder_id:
        new_folder_id = int(new_folder_id)
    if old_folder_id != new_folder_id:
        module.folder_id = new_folder_id
        module.display_order = Module.query.filter_by(folder_id=new_folder_id).count()
        
    mod_type = request.form.get('type')
    
    # Process parameters_json for all module types
    raw_params_json = request.form.get('parameters_json', '').strip()
    if raw_params_json:
        try:
            json.loads(raw_params_json)  # validate before storing
            module.parameters_json = raw_params_json
        except json.JSONDecodeError as je:
            flash(f'Parameters JSON is invalid: {je}. Changes not saved.', 'danger')
            return redirect(url_for('admin.modules'))
    else:
        module.parameters_json = None
        
    if mod_type == 'custom':
        module.custom_code = request.form.get('custom_code')
        if 'zip_file' in request.files and request.files['zip_file'].filename:
            file = request.files['zip_file']
            filename = secure_filename(file.filename)
            zip_path = os.path.join('instance', filename)
            file.save(zip_path)
            module.is_python_folder = True
            module.python_entry_file = request.form.get('python_entry_file') or 'main.py'
            
            extract_dir = os.path.join('instance', 'modules_data', str(module.id))
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir)
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            os.remove(zip_path)
            
        module.connection_id = None
        module.object_type = None
        module.database_name = None
        module.stored_proc_name = None
    else:
        module.connection_id = request.form.get('connection_id') or None
        module.object_type = request.form.get('object_type')
        module.database_name = request.form.get('database_name')
        module.stored_proc_name = request.form.get('stored_proc_name')
        module.custom_code = None
        module.is_python_folder = False
        
    db.session.commit()
    flash(f'Module "{module.name}" updated successfully.', 'success')
    return redirect(url_for('admin.modules'))

@bp.route('/api/modules/<int:module_id>/files')
@login_required
@admin_required
def get_module_files(module_id):
    # Helps to select entry file after zip is uploaded, though usually they select it during upload.
    # To properly support "select from dropdown" they'd have to upload first, then edit.
    extract_dir = os.path.join('instance', 'modules_data', str(module_id))
    files = []
    if os.path.exists(extract_dir):
        for root, _, filenames in os.walk(extract_dir):
            for filename in filenames:
                if filename.endswith('.py'):
                    rel_dir = os.path.relpath(root, extract_dir)
                    rel_file = filename if rel_dir == '.' else os.path.join(rel_dir, filename)
                    files.append(rel_file.replace('\\', '/'))
    return jsonify({'files': files})

@bp.route('/modules/<int:module_id>/rebuild-env', methods=['POST'])
@login_required
@admin_required
def rebuild_module_env(module_id):
    """Delete the module's isolated venv so it gets recreated cleanly on next execution."""
    venv_dir = os.path.join('instance', 'modules_data', str(module_id), 'venv')
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir)
        flash(f'Virtual environment for module {module_id} has been cleared. It will be rebuilt on the next run.', 'success')
    else:
        flash(f'No virtual environment found for module {module_id} — nothing to clear.', 'info')
    return redirect(request.referrer or url_for('admin.modules'))

# --- USERS ---
@bp.route('/users')
@login_required
@admin_required
def users():
    return render_template('admin/users.html', 
                         users=User.query.all(), 
                         modules=Module.query.all(),
                         roles=Role.query.all(),
                         folders=Folder.query.all())

@bp.route('/users/<int:user_id>/permissions', methods=['POST'])
@login_required
@admin_required
def update_user_permissions(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin():
        flash('Cannot modify permissions for an Admin user.', 'warning')
        return redirect(url_for('admin.users'))
        
    user.modules.clear()
    for m_id in request.form.getlist('module_ids'):
        m = Module.query.get(int(m_id))
        if m: user.modules.append(m)
            
    user.roles.clear()
    for r_id in request.form.getlist('role_ids'):
        r = Role.query.get(int(r_id))
        if r: user.roles.append(r)
        
    user.folders.clear()
    for f_id in request.form.getlist('folder_ids'):
        f = Folder.query.get(int(f_id))
        if f: user.folders.append(f)
            
    db.session.commit()
    flash(f'Permissions updated for {user.email}.', 'success')
    return redirect(url_for('admin.users'))

@bp.route('/users/<int:user_id>/roles', methods=['POST'])
@login_required
@admin_required
def update_user_roles(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin():
        flash('Cannot modify roles for an Admin user.', 'warning')
        return redirect(url_for('admin.users'))

    user.roles.clear()
    for r_id in request.form.getlist('role_ids'):
        r = Role.query.get(int(r_id))
        if r:
            user.roles.append(r)

    db.session.commit()
    flash(f'Roles updated for {user.email}.', 'success')
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
        user.modules.clear()
        user.roles.clear()
        user.folders.clear()
        flash(f'User {user.email} promoted to Admin.', 'success')
    db.session.commit()
    return redirect(url_for('admin.users'))

# --- SETTINGS ---
@bp.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    def get_setting(key):
        s = AppSetting.query.filter_by(key=key).first()
        return s.value if s else None

    def save_setting(key, value):
        s = AppSetting.query.filter_by(key=key).first()
        if not s:
            s = AppSetting(key=key, value=value)
            db.session.add(s)
        else:
            s.value = value

    if request.method == 'POST':
        # Save text fields
        for field in ['company_name', 'company_tagline', 'company_email', 'navbar_bg_color', 'navbar_font_color', 'results_retention_days', 'sp_timeout_seconds']:
            val = request.form.get(field, '').strip()
            save_setting(field, val)

        # Logo upload
        if 'logo' in request.files and request.files['logo'].filename:
            file = request.files['logo']
            filename = secure_filename(file.filename)
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            file.save(os.path.join(upload_dir, filename))
            save_setting('company_logo', filename)

        db.session.commit()
        flash('Settings saved successfully.', 'success')
        return redirect(url_for('admin.settings'))

    return render_template('admin/settings.html',
        logo=get_setting('company_logo'),
        company_name=get_setting('company_name') or '',
        company_tagline=get_setting('company_tagline') or '',
        company_email=get_setting('company_email') or '',
        navbar_bg_color=get_setting('navbar_bg_color') or '#ffffff',
        navbar_font_color=get_setting('navbar_font_color') or '#212529',
        results_retention_days=get_setting('results_retention_days') or '7',
        sp_timeout_seconds=get_setting('sp_timeout_seconds') or '1800',
    )
