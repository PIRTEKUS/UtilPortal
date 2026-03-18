import os
from dotenv import load_dotenv

with open('.env_test', 'w') as f:
    f.write('AZURE_TENANT_ID=\n')
    f.write('AZURE_CLIENT_SECRET=123\n')
    f.write('AZURE_TENANT_ID=foo\n')

load_dotenv('.env_test', override=True)
print("With override=True: ", os.environ.get('AZURE_TENANT_ID'))

os.environ.pop('AZURE_TENANT_ID', None)

load_dotenv('.env_test', override=False)
print("With override=False: ", os.environ.get('AZURE_TENANT_ID'))
