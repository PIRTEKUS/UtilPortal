from flask import Flask
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

print("AZURE_CLIENT_ID in app.config:", app.config.get('AZURE_CLIENT_ID'))
print("AZURE_TENANT_ID in app.config:", app.config.get('AZURE_TENANT_ID'))
print("AZURE_AUTHORITY in app.config:", app.config.get('AZURE_AUTHORITY'))
