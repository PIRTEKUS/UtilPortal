import json
import os
import sys
import subprocess
from flask import Blueprint, render_template, abort, request, flash, redirect, url_for, Response, stream_with_context
from flask_login import login_required, current_user
from models import Module, AuditLog, ServerConnection, Folder, db
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

@bp.route('/dashboard')
@login_required
def dashboard():
    modules = get_user_allowed_modules(current_user)
    
    # For a simple tree diagram in the template, we can pass all folders
    # and in the template filter modules to only show allowed ones.
    all_folders = Folder.query.all()
    
    return render_template('portal/dashboard.html', modules=modules, folders=all_folders, allowed_module_ids=[m.id for m in modules])

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
    except json.JSONDecodeError:
        parameters = []
        
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
                elif data_type in ('varchar', 'nvarchar', 'text', 'ntext') and 'max' not in data_type:
                    input_type = 'text'
                    
                parameters.append({
                    'name': row.ParameterName,
                    'label': param_name.replace('_', ' ').title(),
                    'type': input_type,
                    'required': True
                })
                
            cursor.close()
            odbc_conn.close()
        except Exception as e:
            flash(f"Warning: Could not fetch parameters dynamically from SP: {str(e)}", "warning")
            
    if request.method == 'POST':
        submitted_params = {}
        for param in parameters:
            p_name = param.get('name')
            submitted_params[p_name] = request.form.get(p_name)
            
        result_sets = []
            
        try:
            if connection_model and connection_model.server_type == 'sqlserver':
                conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={connection_model.host};UID={connection_model.username};PWD={connection_model.password};Encrypt=Optional;TrustServerCertificate=yes;"
                if module.database_name:
                    conn_str += f";DATABASE={module.database_name}"
                    
                odbc_conn = pyodbc.connect(conn_str, autocommit=True)
                cursor = odbc_conn.cursor()
                
                if module.object_type == 'job':
                    job_name = module.stored_proc_name
                    cursor.execute(f"EXEC msdb.dbo.sp_start_job N'{job_name}'")
                    flash(f'SQL Server Job "{job_name}" has been requested to start.', 'success')
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
                            
                    flash(f'Stored Procedure "{module.stored_proc_name}" executed successfully.', 'success')
                
                cursor.close()
                odbc_conn.close()
                
            log_msg = 'Executed successfully.'
            if result_sets:
                log_msg += f' Returned {len(result_sets)} result set(s).'
                
            log = AuditLog(user_id=current_user.id, module_id=module.id, 
                           parameters_used=json.dumps(submitted_params), status='success', message=log_msg)
            db.session.add(log)
            db.session.commit()
            
            if result_sets:
                return render_template('portal/module_results.html', module=module, result_sets=result_sets)
            
        except Exception as e:
            error_msg = str(e)
            log = AuditLog(user_id=current_user.id, module_id=module.id, 
                           parameters_used=json.dumps(submitted_params), status='error', message=error_msg)
            db.session.add(log)
            db.session.commit()
            flash(f'Error executing module: {error_msg}', 'danger')
            
        return redirect(url_for('portal.dashboard'))
        
    return render_template('portal/module_generic.html', module=module, parameters=parameters)

@bp.route('/execute/python/stream/<int:module_id>')
@login_required
def execute_python_stream(module_id):
    module = Module.query.get_or_404(module_id)
    
    allowed_modules = get_user_allowed_modules(current_user)
    if module not in allowed_modules:
        abort(403)

    def generate():
        import tempfile
        import shutil
        import threading
        import queue as _queue
        
        script_to_run = ""
        cwd = os.getcwd()
        python_executable = sys.executable
        
        try:
            if module.is_python_folder:
                cwd = os.path.join(os.getcwd(), 'instance', 'modules_data', str(module.id))
                # Allow the user to choose a different entry file via query param
                entry_file = request.args.get('entry_file') or module.python_entry_file or 'main.py'
                # Security: strip any path traversal attempts
                entry_file = entry_file.replace('..', '').lstrip('/')
                script_to_run = os.path.join(cwd, entry_file)
                if not os.path.exists(script_to_run):
                    yield f"data: ERROR: Entry file '{entry_file}' not found in uploaded zip.\n\n"
                    return
            elif module.custom_code:
                # Direct code: create a temporary directory to act as the module folder
                cwd = tempfile.mkdtemp(prefix=f"module_{module.id}_")
                script_to_run = os.path.join(cwd, "main.py")
                with open(script_to_run, 'w') as f:
                    f.write(module.custom_code.replace('\r\n', '\n'))
                    
            # Virtual Environment Logic
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

                    # Step 1: Install pipreqs into the module venv first
                    subprocess.run(
                        [venv_pip, "install", "--no-cache-dir", "--quiet", "pipreqs"],
                        cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )

                    # Step 2: Now use the venv's pipreqs binary (it now exists)
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
                        yield f"data: [Setup] pipreqs binary not found after install, skipping auto-generation.\n\n"

                    if os.path.exists(req_file):
                        yield f"data: [Setup] requirements.txt generated successfully.\n\n"
                    else:
                        yield f"data: [Setup] WARNING: Could not auto-generate requirements.txt. Continuing without dependencies.\n\n"
                        # Create empty file so pip install step is still skipped cleanly
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

            # Use a queue + background thread so we can send SSE keepalive
            # pings while waiting for output, preventing nginx from timing out
            # on long-running modules that go quiet between operations.
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

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            while True:
                try:
                    kind, payload = out_queue.get(timeout=15)
                    if kind == 'data':
                        yield f"data: {payload}\n\n"
                    elif kind == 'done':
                        yield f"data: \n\n"
                        yield f"data: Process exited with code {payload}\n\n"
                        break
                    elif kind == 'error':
                        yield f"data: ERROR: {payload}\n\n"
                        break
                except _queue.Empty:
                    # No output for 15s — send an SSE comment to keep nginx alive
                    yield ": keepalive\n\n"

        except Exception as e:
            yield f"data: Execution Failed: {str(e)}\n\n"
        finally:
            if not module.is_python_folder and module.custom_code and os.path.exists(cwd):
                shutil.rmtree(cwd, ignore_errors=True)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')
