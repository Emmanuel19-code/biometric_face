"""Quick start script to test the API"""
import requests
import json

BASE_URL = "http://localhost:5000"

def test_api():
    """Test basic API functionality"""
    print("Testing Facial Recognition Attendance System API\n")
    
    # Test health check
    print("1. Testing health check...")
    try:
        response = requests.get(f"{BASE_URL}/health")
        print(f"   Status: {response.status_code}")
        print(f"   Response: {response.json()}\n")
    except Exception as e:
        print(f"   Error: {e}\n")
        return
    
    # Test root endpoint
    print("2. Testing root endpoint...")
    try:
        response = requests.get(f"{BASE_URL}/")
        print(f"   Status: {response.status_code}")
        print(f"   Response: {json.dumps(response.json(), indent=2)}\n")
    except Exception as e:
        print(f"   Error: {e}\n")
        return
    
    # Test admin login
    print("3. Testing admin login...")
    try:
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "username": "admin",
            "password": "admin123"
        })
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Login successful!")
            print(f"   Admin: {data['admin']['username']}")
            print(f"   Token received: {len(data['access_token'])} characters\n")
            return data['access_token']
        else:
            print(f"   Error: {response.json()}\n")
    except Exception as e:
        print(f"   Error: {e}\n")
    
    return None

if __name__ == "__main__":
    print("=" * 50)
    print("Facial Recognition Attendance System - API Test")
    print("=" * 50)
    print(f"\nMake sure the server is running on {BASE_URL}\n")
    
    token = test_api()
    
    if token:
        print("\n✓ Basic API tests passed!")
        print("\nNext steps:")
        print("1. Register students using POST /api/students/register")
        print("2. Create examination sessions using POST /api/admin/sessions")
        print("3. Verify attendance using POST /api/attendance/verify")
        print("\nSee API_USAGE.md for detailed examples.")
    else:
        print("\n✗ API tests failed. Check if server is running.")
