import msal
import traceback

print("Testing MSAL fallback")
try:
    app = msal.ConfidentialClientApplication(
        "client_id",
        authority="https://login.microsoftonline.com/f13fe9f6-5754-4b16-b2d2-b74568495652\n",
        client_credential="secret"
    )
    print("Authority used:", app.authority.authority_url)
except Exception as e:
    print("Exception thrown:", e)
