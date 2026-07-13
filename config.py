import os
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess'
    # Default to SQLite for local rapid dev if MySQL isn't provided yet
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        # Recycle connections every 55 s — just under MySQL's default 60 s
        # wait_timeout — prevents "MySQL server has gone away" on long SP calls.
        'pool_recycle': 55,
        # Emit a lightweight SELECT 1 before handing out a pooled connection.
        # If the connection is dead, SQLAlchemy transparently reconnects.
        'pool_pre_ping': True,
    }
    
    # Placeholders for future Microsoft Entra ID (Azure AD) SSO
    AZURE_CLIENT_ID = os.environ.get('AZURE_CLIENT_ID')
    AZURE_TENANT_ID = os.environ.get('AZURE_TENANT_ID')
    AZURE_CLIENT_SECRET = os.environ.get('AZURE_CLIENT_SECRET')

    # Authority URL for MSAL
    if AZURE_TENANT_ID:
        AZURE_AUTHORITY = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    else:
        AZURE_AUTHORITY = "https://login.microsoftonline.com/common"
