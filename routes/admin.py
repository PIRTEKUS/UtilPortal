from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from models import User, Module, db
from functools import wraps

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
    return render_template('admin/dashboard.html', 
                         users_count=users_count, 
                         modules_count=modules_count)

@bp.route('/modules')
@login_required
@admin_required
def modules():
    all_modules = Module.query.all()
    return render_template('admin/modules.html', modules=all_modules)

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
        new_module.target_connection = request.form.get('target_connection')
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
