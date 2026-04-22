from flask import Blueprint, render_template, abort, request, flash, redirect, url_for
from flask_login import login_required, current_user
import json
from models import Module, AuditLog, ServerConnection, db
import pyodbc
from urllib.parse import urlsplit

bp = Blueprint('portal', __name__)

@bp.route('/dashboard')
@login_required
def dashboard():
    # User can only see modules they are assigned to
    # Admins can see everything (we can add a switch, or just let them manage via admin)
    if current_user.is_admin():
        modules = Module.query.all()
    else:
        modules = current_user.modules
        
    return render_template('portal/dashboard.html', modules=modules)

@bp.route('/execute/<int:module_id>', methods=['GET', 'POST'])
@login_required
def execute(module_id):
    module = Module.query.get_or_404(module_id)
    
    # Ensure they have permission to this module
    if not current_user.is_admin() and module not in current_user.modules:
        abort(403)
        
    # If the module has a custom python script, we would dynamically load it here.
    if module.custom_script_path:
        # Placeholder for dynamic module loading
        return f"WIP: Custom module {module.custom_script_path} logic not yet implemented."
        
    # Standard Module (Generic parameter form to Stored Procedure)
    # Parse parameter definitions from JSON stored on the module
    try:
        parameters = json.loads(module.parameters_json) if module.parameters_json else []
    except json.JSONDecodeError:
        parameters = []
        
    connection_model = ServerConnection.query.get(module.connection_id) if getattr(module, 'connection_id', None) else None
    
    # Dynamic Parameter Fetching for SQL Server SPs
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
        from sqlalchemy import text, create_engine
        
        # Collect parameters submitted by user
        submitted_params = {}
        for param in parameters:
            p_name = param.get('name')
            submitted_params[p_name] = request.form.get(p_name)
            
        result_sets = []
            
        try:
            if connection_model and connection_model.server_type == 'sqlserver':
                # Use ODBC Driver 18 for SQL Server with Encrypt=Optional
                conn_str = f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={connection_model.host};UID={connection_model.username};PWD={connection_model.password};Encrypt=Optional;TrustServerCertificate=yes;"
                if module.database_name:
                    conn_str += f";DATABASE={module.database_name}"
                    
                # We need autocommit=True for some system stored procedures
                odbc_conn = pyodbc.connect(conn_str, autocommit=True)
                cursor = odbc_conn.cursor()
                
                if module.object_type == 'job':
                    # Execute SQL Server Agent Job using msdb database
                    job_name = module.stored_proc_name
                    cursor.execute(f"EXEC msdb.dbo.sp_start_job N'{job_name}'")
                    flash(f'SQL Server Job "{job_name}" has been requested to start.', 'success')
                else:
                    # Execute Stored Procedure via pyodbc
                    # Extract parameter values in order or by name depending on setup. 
                    # For simplicity, passing them logically if there are any.
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
                
            else:
                # --- MYSQL / DEFAULT ROUTING (Legacy Support) ---
                if getattr(module, 'target_connection', None):
                    # Use custom connection string if provided
                    engine = create_engine(module.target_connection)
                    connection = engine.connect()
                else:
                    # Use default portal connection (db.engine)
                    connection = db.engine.connect()
                    
                bind_placeholders = ", ".join([f":{k}" for k in submitted_params.keys()])
                call_stmt = text(f"CALL {module.stored_proc_name}({bind_placeholders})")
                
                with connection.begin():
                    # For legacy routing we only capture the first result set for simplicity
                    result = connection.execute(call_stmt, submitted_params)
                    if result.returns_rows:
                        columns = result.keys()
                        rows = result.fetchall()
                        result_sets.append({
                            'columns': columns,
                            'rows': [dict(zip(columns, row)) for row in rows]
                        })
                connection.close()
                flash(f'Module {module.name} executed successfully!', 'success')
            
            # Log success
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
            # Log error
            error_msg = str(e)
            log = AuditLog(user_id=current_user.id, module_id=module.id, 
                           parameters_used=json.dumps(submitted_params), status='error', message=error_msg)
            db.session.add(log)
            db.session.commit()
            
            flash(f'Error executing module: {error_msg}', 'danger')
            
        return redirect(url_for('portal.dashboard'))
        
    return render_template('portal/module_generic.html', module=module, parameters=parameters)
