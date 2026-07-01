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
active_connections = {}

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

def _safe_sp_name(sp_name):
    """Wrap stored procedure name parts in brackets if they contain spaces."""
    if not sp_name:
        return sp_name
    parts = sp_name.split('.')
    safe_parts = []
    for part in parts:
        part = part.strip()
        if ' ' in part and not (part.startswith('[') and part.endswith(']')):
            safe_parts.append(f"[{part}]")
        else:
            safe_parts.append(part)
    return '.'.join(safe_parts)


def _build_sql_call(sp_name, parameters, submitted_params, object_type='sp'):
    """Build a human-readable SQL EXEC statement for diagnostics / SSMS testing."""
    if object_type == 'job':
        return f"EXEC msdb.dbo.sp_start_job N'{sp_name}'"
    
    safe_name = _safe_sp_name(sp_name)
    if not parameters:
        return f"EXEC {safe_name}"
    
    parts = []
    for p in parameters:
        val = submitted_params.get(p['name'])
        if val is None:
            parts.append('NULL')
        elif isinstance(val, (int, float)):
            parts.append(str(val))
        else:
            # Escape single quotes and quote strings/datetimes
            escaped_val = str(val).replace("'", "''")
            parts.append(f"'{escaped_val}'")
    
    param_str = ', '.join(parts)
    return f"EXEC {safe_name} {param_str}"


def _execute_sp_sync(module, connection_model, parameters, submitted_params, log_id=None):
    """Execute a stored procedure synchronously.
    Returns (result_sets, error_msg, sql_call, sql_messages)."""
    result_sets = []
    error_msg = None
    sql_call = _build_sql_call(module.stored_proc_name, parameters, submitted_params, module.object_type)
    sql_messages = []
    seen_messages = set()
    
    odbc_conn = None
    try:
        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={connection_model.host};"
            f"UID={connection_model.username};"
            f"PWD={connection_model.password};"
            f"Encrypt=Optional;TrustServerCertificate=yes;"
            f"Connection Timeout=30;"
            f"PacketSize=1024;"
        )
        if module.database_name:
            conn_str += f"DATABASE={module.database_name};"
            
        odbc_conn = pyodbc.connect(conn_str, autocommit=True)
        if log_id:
            active_connections[log_id] = odbc_conn
        
        # Configure query execution timeout on the connection (default: 1800s / 30m)
        try:
            timeout_setting = AppSetting.query.filter_by(key='sp_timeout_seconds').first()
            timeout_val = int(timeout_setting.value) if timeout_setting and timeout_setting.value else 1800
        except Exception:
            timeout_val = 1800
        odbc_conn.timeout = timeout_val
        
        cursor = odbc_conn.cursor()
        
        # Query SQL Server SPID and update AuditLog if log_id is provided
        if log_id:
            try:
                cursor.execute("SELECT @@SPID")
                spid_row = cursor.fetchone()
                if spid_row:
                    spid = spid_row[0]
                    log = AuditLog.query.get(log_id)
                    if log:
                        log.pid = spid
                        db.session.commit()
            except Exception:
                db.session.rollback()
        
        # Apply standard session settings to match SSMS behavior and optimize performance
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        cursor.execute("SET NOCOUNT ON")
        cursor.execute("SET ARITHABORT ON")
        cursor.execute("SET ANSI_WARNINGS ON")
        cursor.execute("SET ANSI_NULLS ON")
        cursor.execute("SET QUOTED_IDENTIFIER ON")
        cursor.execute("SET CONCAT_NULL_YIELDS_NULL ON")
        cursor.execute("SET ANSI_PADDING ON")
        cursor.execute("SET NUMERIC_ROUNDABORT OFF")
        
        def collect_messages():
            try:
                if hasattr(cursor, 'messages') and cursor.messages:
                    for msg in cursor.messages:
                        if isinstance(msg, (list, tuple)) and len(msg) >= 2:
                            msg_text = msg[1]
                        else:
                            msg_text = str(msg)
                        if msg_text not in seen_messages:
                            seen_messages.add(msg_text)
                            clean_msg = msg_text
                            if '[SQL Server]' in clean_msg:
                                clean_msg = clean_msg.split('[SQL Server]', 1)[1]
                            clean_msg = clean_msg.strip()
                            sql_messages.append(clean_msg)
            except Exception:
                pass
        
        if module.object_type == 'job':
            job_name = module.stored_proc_name
            cursor.execute(f"EXEC msdb.dbo.sp_start_job N'{job_name}'")
        else:
            # Append WITH RECOMPILE to prevent parameter sniffing issues with cached plans
            exec_query = sql_call
            if module.object_type == 'sp' or not module.object_type:
                if "WITH RECOMPILE" not in exec_query.upper():
                    exec_query += " WITH RECOMPILE"
            
            cursor.execute(exec_query)
                
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
        
        collect_messages()
        cursor.close()
    except Exception as e:
        error_msg = str(e)
        try:
            if 'cursor' in locals() and cursor:
                collect_messages()
        except Exception:
            pass
    finally:
        if odbc_conn:
            try:
                odbc_conn.close()
            except Exception:
                pass
        if log_id:
            active_connections.pop(log_id, None)
    
    return result_sets, error_msg, sql_call, sql_messages


def _run_sp_background(app, log_id, module_id, connection_id, db_name, object_type,
                        sp_name, parameters, submitted_params):
    """Run SP execution in a background thread. Updates the AuditLog when done."""
    import traceback
    import sys
    with app.app_context():
        try:
            print(f"[_run_sp_background] Thread started for log_id={log_id}", file=sys.stderr, flush=True)
            log = AuditLog.query.get(log_id)
            module = Module.query.get(module_id)
            connection_model = ServerConnection.query.get(connection_id) if connection_id else None
            
            if not connection_model or connection_model.server_type != 'sqlserver':
                print(f"[_run_sp_background] No valid SQL Server connection for log_id={log_id}", file=sys.stderr, flush=True)
                log.status = 'error'
                log.message = 'No valid SQL Server connection.'
                log.end_time = dt.now(tz.utc)
                db.session.commit()
                return
            
            # Store the SQL call being executed so users can see / reproduce it
            sql_call = _build_sql_call(sp_name, parameters, submitted_params, object_type)
            print(f"[_run_sp_background] Storing EXEC message for log_id={log_id}: {sql_call}", file=sys.stderr, flush=True)
            log.message = f'Executing: {sql_call}'
            db.session.commit()
            
            print(f"[_run_sp_background] Calling _execute_sp_sync for log_id={log_id}...", file=sys.stderr, flush=True)
            result_sets, error_msg, _, sql_messages = _execute_sp_sync(module, connection_model, parameters, submitted_params, log_id=log_id)
            print(f"[_run_sp_background] _execute_sp_sync finished for log_id={log_id}. error_msg={error_msg}", file=sys.stderr, flush=True)
            
            # Check if the execution was cancelled by the user while running
            db.session.refresh(log)
            if log.status != 'running':
                print(f"[_run_sp_background] Execution log_id={log_id} was cancelled during run. Status is {log.status}.", file=sys.stderr, flush=True)
                return
            
            log.end_time = dt.now(tz.utc)
            if error_msg:
                log.status = 'error'
                log_msg = f'SQL: {sql_call}\n\nError: {error_msg}'
                if sql_messages:
                    log_msg += f'\n\nSQL Messages:\n' + '\n'.join(sql_messages)
                log.message = log_msg
            else:
                log.status = 'success'
                log_msg = f'Executed successfully.'
                if result_sets:
                    log_msg += f' Returned {len(result_sets)} result set(s).'
                log_msg += f'\n\nSQL: {sql_call}'
                if sql_messages:
                    log_msg += f'\n\nSQL Messages:\n' + '\n'.join(sql_messages)
                log.message = log_msg
                if result_sets:
                    print(f"[_run_sp_background] Serializing results for log_id={log_id}...", file=sys.stderr, flush=True)
                    log.result_data = json.dumps(result_sets, default=str)
            
            print(f"[_run_sp_background] Committing final status for log_id={log_id}...", file=sys.stderr, flush=True)
            db.session.commit()
            print(f"[_run_sp_background] Execution log_id={log_id} finished successfully.", file=sys.stderr, flush=True)
        except Exception as thread_ex:
            print(f"[_run_sp_background] Exception caught in thread for log_id={log_id}: {thread_ex}", file=sys.stderr, flush=True)
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            db.session.rollback()
            try:
                # Reload log in case session was rolled back
                log = AuditLog.query.get(log_id)
                if log:
                    log.status = 'error'
                    log.message = f"Uncaught exception in background thread:\n{str(thread_ex)}\n\nTraceback:\n{traceback.format_exc()}"
                    log.end_time = dt.now(tz.utc)
                    db.session.commit()
            except Exception:
                pass
        finally:
            db.session.remove()


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
            conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={connection_model.host};UID={connection_model.username};PWD={connection_model.password};Encrypt=Optional;TrustServerCertificate=yes;PacketSize=1024;"
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
        # Check if this module is already running to prevent concurrent runs
        running_log = AuditLog.query.filter_by(module_id=module.id, status='running').first()
        if running_log:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
                return jsonify({
                    'error': f'⚠️ "{module.name}" is already running (Execution #{running_log.id}). Please wait for it to complete or cancel it from "My Executions" before running it again.'
                }), 400
            flash(
                f'⚠️ "{module.name}" is already running (Execution #{running_log.id}). '
                f'Please wait for it to complete or cancel it from "My Executions" before running it again.',
                'warning'
            )
            return redirect(url_for('portal.execute', module_id=module.id))

        submitted_params = _parse_submitted_params(parameters, request.form)
        run_in_background = request.form.get('background') == '1'
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
        
        if run_in_background or is_ajax:
            # --- BACKGROUND / STREAMING EXECUTION ---
            sql_preview = _build_sql_call(module.stored_proc_name, parameters, submitted_params, module.object_type)
            log = AuditLog(
                user_id=current_user.id, module_id=module.id,
                parameters_used=json.dumps(submitted_params, default=str),
                status='running', message=f'Executing: {sql_preview}'
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
            
            if is_ajax:
                return jsonify({
                    'status': 'background' if run_in_background else 'stream',
                    'log_id': log.id,
                    'redirect_url': url_for('portal.dashboard')
                })
                
            flash(f'"{module.name}" is now running in the background. You will be notified when it completes.', 'info')
            return redirect(url_for('portal.dashboard'))
        
        # --- SYNCHRONOUS EXECUTION (Execute & Wait) ---
        result_sets = []
        sql_messages = []
            
        try:
            if connection_model and connection_model.server_type == 'sqlserver':
                result_sets, error_msg, sql_call, sql_messages = _execute_sp_sync(module, connection_model, parameters, submitted_params)
                
                if error_msg:
                    raise Exception(error_msg)
                
                if module.object_type == 'job':
                    flash(f'SQL Server Job "{module.stored_proc_name}" has been requested to start.', 'success')
                else:
                    flash(f'Stored Procedure "{module.stored_proc_name}" executed successfully.', 'success')
                
            log_msg = 'Executed successfully.'
            if result_sets:
                log_msg += f' Returned {len(result_sets)} result set(s).'
            log_msg += f'\n\nSQL: {sql_call}'
            if sql_messages:
                log_msg += f'\n\nSQL Messages:\n' + '\n'.join(sql_messages)
                
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
            log_msg = f'Error executing module: {error_msg}'
            if 'sql_call' in locals():
                log_msg = f'SQL: {sql_call}\n\nError: {error_msg}'
            if sql_messages:
                log_msg += f'\n\nSQL Messages:\n' + '\n'.join(sql_messages)
            log = AuditLog(user_id=current_user.id, module_id=module.id, 
                           parameters_used=json.dumps(submitted_params, default=str),
                           status='error', message=log_msg,
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
    
    # Close connection if there is an active database connection for this log_id
    conn_to_close = active_connections.pop(log_id, None)
    if conn_to_close:
        try:
            conn_to_close.close()
        except Exception:
            pass
            
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
        execution_logs = []

        def log_yield(msg, append_newline=True):
            if append_newline:
                execution_logs.append(msg + "\n")
            else:
                execution_logs.append(msg)
            return f"data: {msg}\n\n"

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
                    yield log_yield(f"ERROR: Entry file '{entry_file}' not found in uploaded zip.")
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
                yield log_yield("[Setup] Creating isolated virtual environment...")
                subprocess.run([sys.executable, "-m", "venv", "venv"], cwd=cwd, check=True)

                req_file = os.path.join(cwd, 'requirements.txt')
                if not os.path.exists(req_file):
                    yield log_yield("[Setup] No requirements.txt found. Scanning imports to generate one...")
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
                                yield log_yield(f"[pipreqs] {stripped}")
                        req_proc.wait()
                    else:
                        yield log_yield("[Setup] pipreqs binary not found after install, skipping.")

                    if os.path.exists(req_file):
                        yield log_yield("[Setup] requirements.txt generated successfully.")
                    else:
                        yield log_yield("[Setup] WARNING: Could not auto-generate requirements.txt.")
                        open(req_file, 'w').close()

                if os.path.exists(req_file) and os.path.getsize(req_file) > 0:
                    yield log_yield("[Setup] Installing dependencies...")
                    pip_proc = subprocess.Popen(
                        [venv_pip, "install", "--no-cache-dir", "-r", "requirements.txt"],
                        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1
                    )
                    for line in iter(pip_proc.stdout.readline, ''):
                        stripped = line.strip()
                        if stripped:
                            yield log_yield(f"[pip] {stripped}")
                    pip_proc.wait()
                yield log_yield("[Setup] Environment ready.")

            python_executable = venv_python

            yield log_yield(f"Starting execution of module: {module.name}...")

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
                        yield log_yield(payload, append_newline=False)
                    elif kind == 'done':
                        exit_code = payload
                        yield f"data: \n\n"
                        yield log_yield(f"Process exited with code {payload}")
                        break
                    elif kind == 'error':
                        yield log_yield(f"ERROR: {payload}")
                        break
                except _queue.Empty:
                    yield ": keepalive\n\n"

        except Exception as e:
            yield log_yield(f"Execution Failed: {str(e)}")
        finally:
            # --- Update AuditLog with end time and final status ---
            if log_id:
                try:
                    log = AuditLog.query.get(log_id)
                    if log:
                        log.end_time = datetime.now(_tz.utc)
                        # Check if it was killed/stopped externally
                        if log.status == 'running':
                            log.status = 'success' if exit_code == 0 else 'error'
                            log.message = "".join(execution_logs)
                        else:
                            # It was cancelled/stopped externally, append the console log so far
                            log.message = "".join(execution_logs) + f"\n\n[{log.message}]"
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


@bp.route('/execute/sp/stream/<int:log_id>')
@login_required
def execute_sp_stream(log_id):
    log = AuditLog.query.get_or_404(log_id)
    if log.user_id != current_user.id and not current_user.is_admin():
        abort(403)

    def generate():
        import time
        from datetime import datetime, timezone

        yield "data: [System] Connected to task stream.\n\n"
        yield f"data: [System] Starting database execution...\n\n"
        
        last_logged_len = 0
        while True:
            # Refresh to get the latest status and message updates
            db.session.refresh(log)
            
            # Print elapsed time to keep connection active and inform user
            if log.timestamp:
                # Convert log.timestamp to offset-aware if naive, or perform offset-naive comparison
                log_time = log.timestamp.replace(tzinfo=None)
                now_time = datetime.now(timezone.utc).replace(tzinfo=None)
                elapsed = int((now_time - log_time).total_seconds())
                yield f"data: [System] Query executing... (elapsed: {elapsed}s)\n\n"
            
            # Stream any intermediate message text that was saved (if any)
            if log.message and len(log.message) > last_logged_len:
                new_part = log.message[last_logged_len:]
                last_logged_len = len(log.message)
                for line in new_part.split('\n'):
                    if line.strip():
                        yield f"data: {line}\n\n"

            if log.status != 'running':
                if log.status == 'success':
                    yield f"data: REDIRECT: {url_for('portal.view_execution_results', log_id=log.id)}\n\n"
                else:
                    yield f"data: [System] Task finished with status: {log.status.upper()}\n\n"
                break
                
            time.sleep(2)

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

