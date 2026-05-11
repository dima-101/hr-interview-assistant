import httpx

URL = "http://127.0.0.1:8000/reports/report_storekeeper_001_Тестовый_Кандидат_20260505_220319.md"

def main():
    # если включена HR-авторизация, можно пока временно закомментировать require_hr_auth
    response = httpx.get(URL, follow_redirects=True, timeout=30.0)
    print("Status code:", response.status_code)
    print("First 500 chars:")
    print(response.text[:500])

if __name__ == "__main__":
    main()