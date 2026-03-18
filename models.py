from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

db = SQLAlchemy()

# Association table mapping users to the modules they have permission to execute
user_modules = db.Table('user_modules',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('module_id', db.Integer, db.ForeignKey('module.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='user') # Can be 'admin' or 'user'
    
    # Relationship to access the modules a user can run
    modules = db.relationship('Module', secondary=user_modules, lazy='subquery',
        backref=db.backref('users', lazy=True))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_admin(self):
        return self.role == 'admin'

    def __repr__(self):
        return f'<User {self.email}>'

class ServerConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    server_type = db.Column(db.String(50), nullable=False) # 'mysql', 'sqlserver'
    host = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(255), nullable=False) # Plain text for simplicity in this iteration

    # Relationship to know which modules use this connection
    modules = db.relationship('Module', backref='connection', lazy=True)

    def __repr__(self):
        return f'<ServerConnection {self.name} ({self.server_type})>'

class Module(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    # For SQL/Stored Procedure modules
    connection_id = db.Column(db.Integer, db.ForeignKey('server_connection.id'))
    database_name = db.Column(db.String(100)) # Target database for SPs
    object_type = db.Column(db.String(20)) # 'sp' or 'job'
    stored_proc_name = db.Column(db.String(100)) # Reused for job name if object_type=='job'
    parameters_json = db.Column(db.Text) # JSON string defining required parameters
    
    # For complex modules requiring Python logic
    custom_script_path = db.Column(db.String(255)) # e.g. 'modules/my_module.py'

    def __repr__(self):
        return f'<Module {self.name}>'

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, index=True, default=lambda: datetime.now(timezone.utc))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    module_id = db.Column(db.Integer, db.ForeignKey('module.id'), nullable=False)
    parameters_used = db.Column(db.Text) # JSON string of what parameters were submitted
    status = db.Column(db.String(50)) # 'success' or 'error'
    message = db.Column(db.Text)

    user = db.relationship('User', backref=db.backref('audit_logs', lazy=True))
    module = db.relationship('Module', backref=db.backref('audit_logs', lazy=True))

    def __repr__(self):
        return f'<AuditLog {self.id} User {self.user_id} Module {self.module_id}>'
