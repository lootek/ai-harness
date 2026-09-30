import requests

session = requests.Session()
resp = session.get("https://api.example.com/v1/widgets", timeout=10)
print(resp.status_code, len(resp.json()))
