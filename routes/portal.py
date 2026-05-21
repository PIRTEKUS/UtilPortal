import json
from datetime import datetime as dt, date as dt_date, timezone as tz
import os
import sys
import subprocess
import threading
from flask import Blueprint, render_template, abort, request, flash, redirect, url_for, Response, stream_with_context, jsonify, current_app
from flask_login import login_required, current_user
from models import Module, AuditLog, ServerConnection, Folder, AppSetting, db
import pyodbc

bp = Blueprint('portal', __name__)

def get_user_allowed_modules(user):
    if user.is_admin():
        return Module.query.all()
    
    # Collect all modules the user has access to
    allowed_modules = set()
    
    # 1. Direct module assignments
    for m in user.modules:
        allowed_modules.add(m)
        
    # 2. Modules from direct folder assignments
    for f in user.folders:
        for m in f.modules:
            allowed_modules.add(m)
            
    # 3. Modules from role assignments (direct to role)
    for r in user.roles:
        for m in r.modules:
            allowed_modules.add(m)
            
    # 4. Modules from folder assignments via roles
    for r in user.roles:
        for f in r.folders:
            for m in f.modules:
                allowed_modules.add(m)
                
    return list(allowed_modules)

def build_tree(modules):
    # Returns a list of root folders (with nested structure) and root modules
    folders_map = {f.id: f for f in Folder.query.all()}
    
    tree_folders = {}
    root_folders = []
    root_modules = []
    
    # We only include folders that contain an allowed module
    allowed_folder_ids = set()
    for m in modules:
        if m.folder_id:
            curr = m.folder_id
            while curr:
                allowed_folder_ids.add(curr)
                f = folders_map.get(curr)
                curr = f.parent_id if f else None
        else:
            root_modules.append(m)
            
    # Reconstruct folder tree only for allowed folders
    # Wait, simpler logic: just pass the allowed_modules and let Jinja group them by folder.
    pass

def cleanup_old_results():
    """Purge result_data from AuditLog entries older than the configured retention period.
    The AuditLog row itself is preserved forever — only the bulky JSON is cleared."""
    try:
        setting = AppSetting.query.filter_by(key='results_retention_days').first()
        days = int(setting.value) if setting and setting.value else 7
        
        from datetime import timedelta
        cutoff = dt.now(tz.utc) - timedelta(days=days)
        
        stale = AuditLog.query.filter(
            AuditLog.timestamp < cutoff,
            AuditLog.result_data.isnot(None)
        ).all()
        
        for log in stale:
            log.result_data = None
        
        if stale:
            db.session.commit()
    except Exception:
        db.session.rollback()

@bp.route('/dashboard')
@login_required
def dashboard():
    # Opportunistically clean up old results on dashboard load
    cleanup_old_results()
    
    modules = get_user_allowed_modules(current_user)
    
    # For a simple tree diagram in the template, we can pass all folders
    # and in the template filter modules to only show allowed ones.
    all_folders = Folder.query.all()
    
    allowed_module_ids = set(m.id for m in modules)
    allowed_folder_ids = set()

    def check_folder(f):
        if f.id in allowed_folder_ids:
            return True
        has_allowed = False
        if any(m.id in allowed_module_ids for m in f.modules):
            has_allowed = True
        for sub in f.subfolders:
            if check_folder(sub):
                has_allowed = True
        if has_allowed:
            allowed_folder_ids.add(f.id)
        return has_allowed

    for f in all_folders:
        if not f.parent_id:
            check_folder(f)
    
    return render_template('portal/dashboard.html', modules=modules, folders=all_folders, allowed_module_ids=list(allowed_module_ids), allowed_folder_ids=list(allowed_folder_ids))


def _parse_submitted_params(parameters, form):
    """Extract and type-convert submitted form parameters."""
    submitted = {}
    for param in parameters:
        p_name = param.get('name')
        p_type = param.get('type', 'text')
        raw_value = form.get(p_name)
        
        if p_type == 'checkbox':
            submitted[p_name] = 1 if raw_value else 0
        elif p_type == 'datetime-local' and raw_value:
            try:
                submitted[p_name] = dt.fromisoformat(raw_value)
            except ValueError:
                submitted[p_name] = raw_value
        elif p_type == 'date' and raw_value:
            try:
                submitted[p_name] = dt_date.fromisoformat(raw_value)
            except ValueError:
                submitted[p_name] = raw_value
        elif p_type == 'number' and raw_value:
            try:
                submitted[p_name] = int(raw_value) if '.' not in raw_value else float(raw_value)
            except ValueError:
                submitted[p_name] = raw_value
        else:
            submitted[p_name] = raw_value
    return submitted

def _build_sql_call(sp_name, parameters, submitted_params, object_type='sp'):
    """Build a human-readable SQL EXEC statement for diagnostics / SSMS testing."""
    if object_type == 'job':
        return f"EXEC msdb.dbo.sp_start_job N'{sp_name}'"
    
    if not parameters:
        return f"EXEC {sp_name}"
    
    parts = []
    for p in parameters:
        val = submitted_params.get(p['name'])
        if val is None:
            parts.append('NULL')
        elif isinstance(val, (int, float)):
            parts.append(str(val))
        else:
            # Quote strings/datetimes
            parts.append(f"'{val}'")
    
    param_str = ', '.join(parts)
    return f"EXEC {sp_name} {param_str}"


def _execute_sp_sync(module, connection_model, parameters, submitted_params):
    """Execute a stored procedure synchronously.
    Returns (result_sets, error_msg, sql_call)."""
    result_sets = []
    error_msg = None
    sql_call = _build_sql_call(module.stored_proc_name, parameters, submitted_params, module.object_type)
    
    try:
        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={connection_model.host};"
            f"UID={connection_model.username};"
            f"PWD={connection_model.password};"
            f"Encrypt=Optional;TrustServerCertificate=yes;"
            f"Connection Timeout=30;"
        )
        if module.database_name:
            conn_str += f"DATABASE={module.database_name};"
            
        odbc_conn = pyodbc.connect(conn_str, autocommit=True)
        cursor = odbc_conn.cursor()
        
        if module.object_type == 'job':
            job_name = module.stored_proc_name
            cursor.execute(f"EXEC msdb.dbo.sp_start_job N'{job_name}'")
        else:
            if parameters:
                params_list = [submitted_params.get(p['name']) for p in parameters]
                placeholders = ",".join(["?" for _ in params_list])
                cursor.execute(f"EXEC {module.stored_proc_name} {placeholders}", params_list)
            else:
                cursor.execute(f"EXEC {module.stored_proc_name}")
                
            while True:
                if cursor.description:
                    columns = [col[0] for col in cursor.description]
                    rows = cursor.fetchall()
                    result_sets.append({
                        'columns': columns,
                        'rows': [dict(zip(columns, row)) for row in rows]
                    })
                if not cursor.nextset():
                    break
        
        cursor.close()
        odbc_conn.close()
    except Exception as e:
        error_msg = str(e)
    
    return result_sets, error_msg, sql_call


def _run_sp_background(app, log_id, module_id, connection_id, db_name, object_type,
                        sp_name, parameters, submitted_params):
    """Run SP execution in a background thread. Updates the AuditLog when done."""
    with app.app_context():
        log = AuditLog.query.get(log_id)
        module = Module.query.get(module_id)
        connection_model = ServerConnection.query.get(connection_id) if connection_id else None
        
        if not connection_model or connection_model.server_type != 'sqlserver':
            log.status = 'error'
            log.message = 'No valid SQL Server connection.'
            log.end_time = dt.now(tz.utc)
            db.session.commit()
            return
        
        # Store the SQL call being executed so users can see / reproduce it
        sql_call = _build_sql_call(sp_name, parameters, submitted_params, object_type)
        log.message = f'Executing: {sql_call}'
        db.session.commit()
        
        result_sets, error_msg, _ = _execute_sp_sync(module, connection_model, parameters, submitted_params)
        
        # Check if the execution was cancelled by the user while running
        db.session.refresh(log)
        if log.status != 'running':
            return
        
        log.end_time = dt.now(tz.utc)
        if error_msg:
            log.status = 'error'
            log.message = f'SQL: {sql_call}\n\nError: {error_msg}'
        else:
            log.status = 'success'
            log_msg = f'Executed successfully.'
            if result_sets:
                log_msg += f' Returned {len(result_sets)} result set(s).'
            log_msg += f'\n\nSQL: {sql_call}'
            log.message = log_msg
            if result_sets:
                log.result_data = json.dumps(result_sets, default=str)
        
        db.session.commit()


@bp.route('/execute/<int:module_id>', methods=['GET', 'POST'])
@login_required
def execute(module_id):
    module = Module.query.get_or_404(module_id)
    
    allowed_modules = get_user_allowed_modules(current_user)
    if module not in allowed_modules:
        abort(403)
        
    # Python Module
    if module.custom_code or module.is_python_folder:
        py_files = []
        if module.is_python_folder:
            module_dir = os.path.join(os.getcwd(), 'instance', 'modules_data', str(module.id))
            if os.path.exists(module_dir):
                for root, _, filenames in os.walk(module_dir):
                    for fname in sorted(filenames):
                        if fname.endswith('.py'):
                            rel = os.path.relpath(os.path.join(root, fname), module_dir).replace('\\', '/')
                            # Skip files inside the venv subfolder
                            if not rel.startswith('venv/'):
                                py_files.append(rel)
            # Put the configured entry file first
            entry = module.python_entry_file or 'main.py'
            if entry in py_files:
                py_files.remove(entry)
            py_files.insert(0, entry)
        return render_template('portal/module_python.html', module=module, py_files=py_files)
        
    # Standard Module (Generic parameter form to Stored Procedure)
    try:
        parameters = json.loads(module.parameters_json) if module.parameters_json else []
    except json.JSONDecodeError as je:
        parameters = []
        flash(
            f'⚠️ The Parameters JSON stored for this module is invalid ({je}). '
            f'Parameters will be fetched live from SQL Server instead. '
            f'Please fix the Parameters JSON field in the module settings.',
            'warning'
        )
        
    connection_model = ServerConnection.query.get(module.connection_id) if getattr(module, 'connection_id', None) else None
    
    if not parameters and module.object_type == 'sp' and connection_model and connection_model.server_type == 'sqlserver':
        try:
            conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={connection_model.host};UID={connection_model.username};PWD={connection_model.password};Encrypt=Optional;TrustServerCertificate=yes;"
            if module.database_name:
                conn_str += f";DATABASE={module.database_name}"
            
            odbc_conn = pyodbc.connect(conn_str, autocommit=True)
            cursor = odbc_conn.cursor()
            
            query = """
            SELECT p.name AS ParameterName, t.name AS DataType
            FROM sys.parameters p
            INNER JOIN sys.types t ON p.user_type_id = t.user_type_id
            WHERE p.object_id = OBJECT_ID(?)
            ORDER BY p.parameter_id
            """
            cursor.execute(query, module.stored_proc_name)
            
            for row in cursor.fetchall():
                param_name = row.ParameterName.replace('@', '')
                data_type = row.DataType.lower()
                
                input_type = 'text'
                if data_type in ('int', 'bigint', 'smallint', 'tinyint', 'decimal', 'numeric', 'float', 'real'):
                    input_type = 'number'
                elif data_type == 'bit':
                    input_type = 'checkbox'
                elif data_type == 'date':
                    input_type = 'date'
                elif data_type in ('datetime', 'datetime2', 'smalldatetime'):
                    input_type = 'datetime-local'
                elif data_type in ('varchar', 'nvarchar', 'text', 'ntext') and 'max' not in data_type:
                    input_type = 'text'
                    
                parameters.append({
                    'name': row.ParameterName,
                    'label': param_name.replace('_', ' ').title(),
                    'type': input_type,
                    'required': input_type != 'checkbox'
                })
                
            cursor.close()
            odbc_conn.close()
        except Exception as e:
            flash(f"Warning: Could not fetch parameters dynamically from SP: {str(e)}", "warning")
            
    if request.method == 'POST':
        submitted_params = _parse_submitted_params(parameters, request.form)
        run_in_background = request.form.get('background') == '1'
        
        if run_in_background:
            # --- BACKGROUND EXECUTION ---
            sql_preview = _build_sql_call(module.stored_proc_name, parameters, submitted_params, module.object_type)
            log = AuditLog(
                user_id=current_user.id, module_id=module.id,
                parameters_used=json.dumps(submitted_params, default=str),
                status='running', message=f'Executing in background...\n\nSQL: {sql_preview}'
            )
            db.session.add(log)
            db.session.commit()
            
            app = current_app._get_current_object()
            thread = threading.Thread(
                target=_run_sp_background,
                args=(app, log.id, module.id, module.connection_id,
                      module.database_name, module.object_type,
                      module.stored_proc_name, parameters, submitted_params),
                daemon=True
            )
            thread.start()
            
            flash(f'"{module.name}" is now running in the background. You will be notified when it completes.', 'info')
            return redirect(url_for('portal.dashboard'))
        
        # --- SYNCHRONOUS EXECUTION (Execute & Wait) ---
        result_sets = []
            
        try:
            if connection_model and connection_model.server_type == 'sqlserver':
                result_sets, error_msg, sql_call = _execute_sp_sync(module, connection_model, parameters, submitted_params)
                
                if error_msg:
                    raise Exception(error_msg)
                
                if module.object_type == 'job':
                    flash(f'SQL Server Job "{module.stored_proc_name}" has been requested to start.', 'success')
                else:
                    flash(f'Stored Procedure "{module.stored_proc_name}" executed successfully.', 'success')
                
            log_msg = 'Executed successfully.'
            if result_sets:
                log_msg += f' Returned {len(result_sets)} result set(s).'
                
            log = AuditLog(user_id=current_user.id, module_id=module.id, 
                           parameters_used=json.dumps(submitted_params, default=str),
                           status='success', message=log_msg,
                           end_time=dt.now(tz.utc), notified=True)
            if result_sets:
                log.result_data = json.dumps(result_sets, default=str)
            db.session.add(log)
            db.session.commit()
            
            if result_sets:
                return render_template('portal/module_results.html', module=module,
                                       result_sets=result_sets, log_id=log.id)
            
        except Exception as e:
            error_msg = str(e)
            log = AuditLog(user_id=current_user.id, module_id=module.id, 
                           parameters_used=json.dumps(submitted_params, default=str),
                           status='error', message=error_msg,
                           end_time=dt.now(tz.utc), notified=True)
            db.session.add(log)
            db.session.commit()
            flash(f'Error executing module: {error_msg}', 'danger')
            
        return redirect(url_for('portal.dashboard'))
    
    # --- Compute execution duration stats for this module ---
    from sqlalchemy import func
    exec_stats = {'last_duration': None, 'avg_duration': None, 'recommendation': 'background'}
    
    completed_logs = AuditLog.query.filter(
        AuditLog.module_id == module.id,
        AuditLog.status.in_(['success', 'error']),
        AuditLog.end_time.isnot(None),
        AuditLog.timestamp.isnot(None)
    ).order_by(AuditLog.timestamp.desc()).limit(20).all()
    
    if completed_logs:
        # Last execution duration
        last = completed_logs[0]
        last_secs = (last.end_time - last.timestamp).total_seconds()
        exec_stats['last_duration'] = int(last_secs)
        
        # Average across recent executions
        durations = [(l.end_time - l.timestamp).total_seconds() for l in completed_logs]
        avg_secs = sum(durations) / len(durations)
        exec_stats['avg_duration'] = int(avg_secs)
        
        # Recommend "wait" if both last and avg are under 30 seconds
        if last_secs < 30 and avg_secs < 30:
            exec_stats['recommendation'] = 'wait'
        
    return render_template('portal/module_generic.html', module=module,
                           parameters=parameters, exec_stats=exec_stats)


# ──────────────────────────────────────────────
# Task Polling & Background Task API
# ──────────────────────────────────────────────

@bp.route('/api/tasks/active')
@login_required
def api_tasks_active():
    """Return the current user's running tasks + recently finished tasks not yet notified."""
    try:
        running = AuditLog.query.filter_by(user_id=current_user.id, status='running').all()

        try:
            newly_done = AuditLog.query.filter(
                AuditLog.user_id == current_user.id,
                AuditLog.status.in_(['success', 'error']),
                AuditLog.notified == False  # noqa: E712
            ).all()
        except Exception:
            # 'notified' column may not exist yet (migration pending) — degrade gracefully
            newly_done = []

        tasks = []
        for log in running:
            tasks.append({
                'id': log.id,
                'module_name': log.module.name if log.module else '—',
                'status': log.status,
                'started': log.timestamp.strftime('%Y-%m-%d %H:%M:%S') if log.timestamp else None,
            })

        completed = []
        for log in newly_done:
            has_results = log.result_data is not None
            completed.append({
                'id': log.id,
                'module_name': log.module.name if log.module else '—',
                'status': log.status,
                'message': (log.message or '')[:300],   # cap length to be safe
                'has_results': has_results,
            })

        return jsonify({'running': tasks, 'completed': completed, 'running_count': len(running)})

    except Exception as e:
        # Never return a non-JSON 500 — the browser's r.json() would break page JS
        return jsonify({'running': [], 'completed': [], 'running_count': 0, 'error': str(e)}), 200


@bp.route('/api/tasks/<int:log_id>/dismiss', methods=['POST'])
@login_required
def api_task_dismiss(log_id):
    """Mark a task notification as seen/dismissed."""
    log = AuditLog.query.get_or_404(log_id)
    if log.user_id != current_user.id and not current_user.is_admin():
        abort(403)
    log.notified = True
    db.session.commit()
    return jsonify({'ok': True})


# ──────────────────────────────────────────────
# My Executions — User's own history
# ──────────────────────────────────────────────

@bp.route('/executions')
@login_required
def my_executions():
    """Show the current user's own execution history."""
    logs = AuditLog.query.filter_by(user_id=current_user.id)\
        .order_by(AuditLog.timestamp.desc()).limit(200).all()
    
    setting = AppSetting.query.filter_by(key='results_retention_days').first()
    retention_days = int(setting.value) if setting and setting.value else 7
    
    return render_template('portal/my_executions.html', logs=logs, retention_days=retention_days)

@bp.route('/executions/<int:log_id>/stop', methods=['POST'])
@login_required
def stop_execution(log_id):
    log = AuditLog.query.get_or_404(log_id)
    if log.user_id != current_user.id:
        flash('Unauthorized to stop this execution.', 'danger')
        return redirect(url_for('portal.my_executions'))
        
    if log.status != 'running':
        flash('This execution is no longer running.', 'info')
        return redirect(url_for('portal.my_executions'))
        
    # Mark as error / cancelled
    log.status = 'error'
    log.message = log.message + '\n\n[Execution forcefully cancelled by user]'
    log.end_time = dt.now(tz.utc)
    db.session.commit()
    
    flash(f'Execution #{log.id} has been cancelled.', 'success')
    return redirect(url_for('portal.my_executions'))


@bp.route('/executions/<int:log_id>/results')
@login_required
def view_execution_results(log_id):
    """Re-render results from a stored execution."""
    log = AuditLog.query.get_or_404(log_id)
    if log.user_id != current_user.id and not current_user.is_admin():
        abort(403)
    
    if not log.result_data:
        flash('No results available for this execution. Results may have expired.', 'warning')
        return redirect(url_for('portal.my_executions'))
    
    try:
        result_sets = json.loads(log.result_data)
    except json.JSONDecodeError:
        flash('Could not parse stored results.', 'danger')
        return redirect(url_for('portal.my_executions'))
    
    return render_template('portal/module_results.html',
                           module=log.module, result_sets=result_sets, log_id=log.id)


@bp.route('/execute/python/stream/<int:module_id>')
@login_required
def execute_python_stream(module_id):
    module = Module.query.get_or_404(module_id)

    allowed_modules = get_user_allowed_modules(current_user)
    if module not in allowed_modules:
        abort(403)

    # Capture these before entering the generator (no request context inside)
    user_id = current_user.id
    entry_file_arg = request.args.get('entry_file')

    def generate():
        import tempfile
        import shutil
        import threading as _threading
        import queue as _queue
        from datetime import datetime, timezone as _tz

        script_to_run = ""
        cwd = os.getcwd()
        python_executable = sys.executable
        log_id = None

        # --- Create an AuditLog entry with status='running' ---
        try:
            from flask import current_app
            log = AuditLog(
                user_id=user_id,
                module_id=module.id,
                status='running',
                message='Execution started.'
            )
            db.session.add(log)
            db.session.commit()
            log_id = log.id
        except Exception:
            db.session.rollback()

        exit_code = 1
        try:
            if module.is_python_folder:
                cwd = os.path.join(os.getcwd(), 'instance', 'modules_data', str(module.id))
                entry_file = entry_file_arg or module.python_entry_file or 'main.py'
                entry_file = entry_file.replace('..', '').lstrip('/')
                script_to_run = os.path.join(cwd, entry_file)
                if not os.path.exists(script_to_run):
                    yield f"data: ERROR: Entry file '{entry_file}' not found in uploaded zip.\n\n"
                    return
            elif module.custom_code:
                cwd = tempfile.mkdtemp(prefix=f"module_{module.id}_")
                script_to_run = os.path.join(cwd, "main.py")
                with open(script_to_run, 'w') as f:
                    f.write(module.custom_code.replace('\r\n', '\n'))

            # --- Virtual Environment ---
            venv_dir = os.path.join(cwd, 'venv')
            if os.name == 'nt':
                venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe')
                venv_pip = os.path.join(venv_dir, 'Scripts', 'pip.exe')
            else:
                venv_python = os.path.join(venv_dir, 'bin', 'python')
                venv_pip = os.path.join(venv_dir, 'bin', 'pip')

            if not os.path.exists(venv_dir):
                yield f"data: [Setup] Creating isolated virtual environment...\n\n"
                subprocess.run([sys.executable, "-m", "venv", "venv"], cwd=cwd, check=True)

                req_file = os.path.join(cwd, 'requirements.txt')
                if not os.path.exists(req_file):
                    yield f"data: [Setup] No requirements.txt found. Scanning imports to generate one...\n\n"
                    subprocess.run(
                        [venv_pip, "install", "--no-cache-dir", "--quiet", "pipreqs"],
                        cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    pipreqs_bin = os.path.join(venv_dir,
                        'Scripts' if os.name == 'nt' else 'bin', 'pipreqs')
                    if os.path.exists(pipreqs_bin):
                        req_proc = subprocess.Popen(
                            [pipreqs_bin, "--force", "--ignore", "venv", "."],
                            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1
                        )
                        for line in iter(req_proc.stdout.readline, ''):
                            stripped = line.strip()
                            if stripped:
                                yield f"data: [pipreqs] {stripped}\n\n"
                        req_proc.wait()
                    else:
                        yield f"data: [Setup] pipreqs binary not found after install, skipping.\n\n"

                    if os.path.exists(req_file):
                        yield f"data: [Setup] requirements.txt generated successfully.\n\n"
                    else:
                        yield f"data: [Setup] WARNING: Could not auto-generate requirements.txt.\n\n"
                        open(req_file, 'w').close()

                if os.path.exists(req_file) and os.path.getsize(req_file) > 0:
                    yield f"data: [Setup] Installing dependencies...\n\n"
                    pip_proc = subprocess.Popen(
                        [venv_pip, "install", "--no-cache-dir", "-r", "requirements.txt"],
                        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1
                    )
                    for line in iter(pip_proc.stdout.readline, ''):
                        stripped = line.strip()
                        if stripped:
                            yield f"data: [pip] {stripped}\n\n"
                    pip_proc.wait()
                yield f"data: [Setup] Environment ready.\n\n"

            python_executable = venv_python

            yield f"data: Starting execution of module: {module.name}...\n\n"

            process = subprocess.Popen(
                [python_executable, "-u", script_to_run],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            # --- Update AuditLog with PID ---
            if log_id:
                try:
                    from flask import current_app
                    with current_app.app_context():
                        log = AuditLog.query.get(log_id)
                        if log:
                            log.pid = process.pid
                            db.session.commit()
                except Exception:
                    pass

            out_queue = _queue.Queue()

            def _reader():
                try:
                    for line in iter(process.stdout.readline, ''):
                        out_queue.put(('data', line))
                    process.stdout.close()
                    process.wait()
                    out_queue.put(('done', process.returncode))
                except Exception as exc:
                    out_queue.put(('error', str(exc)))

            reader_thread = _threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            while True:
                try:
                    kind, payload = out_queue.get(timeout=15)
                    if kind == 'data':
                        yield f"data: {payload}\n\n"
                    elif kind == 'done':
                        exit_code = payload
                        yield f"data: \n\n"
                        yield f"data: Process exited with code {payload}\n\n"
                        break
                    elif kind == 'error':
                        yield f"data: ERROR: {payload}\n\n"
                        break
                except _queue.Empty:
                    yield ": keepalive\n\n"

        except Exception as e:
            yield f"data: Execution Failed: {str(e)}\n\n"
        finally:
            # --- Update AuditLog with end time and final status ---
            if log_id:
                try:
                    log = AuditLog.query.get(log_id)
                    if log:
                        log.end_time = datetime.now(_tz.utc)
                        log.status = 'success' if exit_code == 0 else 'error'
                        log.message = f'Exited with code {exit_code}.'
                        db.session.commit()
                except Exception:
                    db.session.rollback()

            if not module.is_python_folder and module.custom_code and os.path.exists(cwd):
                shutil.rmtree(cwd, ignore_errors=True)

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    # Tell nginx NOT to buffer this response — critical for real-time output
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp
