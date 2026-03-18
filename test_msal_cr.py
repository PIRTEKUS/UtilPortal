import msal

tenant_with_cr = "f13fe9f6-5754-4b16-b2d2-b74568495652\r"
auth_url = f"https://login.microsoftonline.com/{tenant_with_cr}"

msal_app = msal.ConfidentialClientApplication(
    "1abbe74a-ec1a-4cc4-b623-60c3ebfbb5b5",
    authority=auth_url,
    client_credential="dummy_secret"
)

url = msal_app.get_authorization_request_url(scopes=[], redirect_uri="http://localhost")
with open("test_msal_output.txt", "w") as f:
    f.write(url)
