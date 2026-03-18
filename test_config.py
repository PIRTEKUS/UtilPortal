import sys
from config import Config
print("AZURE_CLIENT_ID:", Config.AZURE_CLIENT_ID)
print("AZURE_TENANT_ID:", Config.AZURE_TENANT_ID)
print("Has AZURE_AUTHORITY attr?", hasattr(Config, 'AZURE_AUTHORITY'))
if hasattr(Config, 'AZURE_AUTHORITY'):
    print("AZURE_AUTHORITY:", Config.AZURE_AUTHORITY)
