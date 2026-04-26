import requests

url = "http://127.0.0.1:5000/precision_test/api/analyze"
payload = {
    "code_diff": "test diff",
    "project_type": "backend"
}

try:
    response = requests.post(url, json=payload)
    print("Status Code:", response.status_code)
    print("Response JSON:", response.json())
except Exception as e:
    print("Error:", e)
