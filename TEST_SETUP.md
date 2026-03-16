# Test Setup and Execution Guide

## Test Results Summary

The test suite has been created and the test runner successfully discovered all test files:

✅ **Test Files Created:**
- `tests/test_models.py` - Database model tests
- `tests/test_services.py` - Service layer tests  
- `tests/test_student_service.py` - Student service tests
- `tests/test_utils.py` - Utility module tests
- `run_tests.py` - Test runner script

## Current Status

The tests cannot run yet because dependencies need to be installed first. The test runner correctly identified that the following modules are missing:
- `flask`
- `cryptography`
- And other dependencies from `requirements.txt`

## Steps to Run Tests

### 1. Install Dependencies

```bash
# Create virtual environment (recommended)
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

**Note:** Installing `face-recognition` may require additional system dependencies:
- **Windows:** Visual C++ Build Tools
- **Linux:** `cmake`, `libopenblas-dev`, `liblapack-dev`
- **Mac:** `cmake` via Homebrew

### 2. Run Tests

Once dependencies are installed, run:

```bash
# Option 1: Use the test runner script
python run_tests.py

# Option 2: Use unittest directly
python -m unittest discover tests -v

# Option 3: Use pytest (if installed)
pytest tests/ -v
```

## Test Coverage

The test suite includes:

### Model Tests (`test_models.py`)
- ✅ Admin model creation and password hashing
- ✅ Student model with encrypted face encodings
- ✅ Examination session creation
- ✅ Attendance record creation

### Service Tests (`test_services.py`)
- ✅ Admin service (create, authenticate, stats)
- ✅ Student service (get, list students)
- ✅ Attendance service (session creation, retrieval)

### Utility Tests (`test_utils.py`)
- ✅ Encryption/decryption functions
- ✅ Face recognition engine initialization
- ✅ Image validation (basic checks)

## Expected Test Results

Once dependencies are installed, you should see output like:

```
============================================================
Facial Recognition Attendance System - Test Suite
============================================================

test_admin_model (tests.test_models.TestModels) ... ok
test_student_model (tests.test_models.TestModels) ... ok
test_examination_session_model (tests.test_models.TestModels) ... ok
test_attendance_model (tests.test_models.TestModels) ... ok
test_create_admin (tests.test_services.TestAdminService) ... ok
test_authenticate_admin (tests.test_services.TestAdminService) ... ok
test_get_system_stats (tests.test_services.TestAdminService) ... ok
...

----------------------------------------------------------------------
Ran X tests in X.XXXs

OK
============================================================
All tests passed!
============================================================
```

## Troubleshooting

### Import Errors
If you see `ModuleNotFoundError`, ensure:
1. Virtual environment is activated
2. All dependencies are installed: `pip install -r requirements.txt`
3. You're running from the project root directory

### Face Recognition Tests
Some face recognition tests may fail if:
- No actual face images are provided (tests use dummy images)
- `dlib` or `face-recognition` libraries aren't properly installed
- System doesn't have required build tools

### Database Errors
Tests use in-memory SQLite database, so no database setup is needed. If you see database errors:
- Check that SQLAlchemy is installed
- Verify test configuration in `setUp()` methods

## Next Steps

1. **Install dependencies** using the steps above
2. **Run the test suite** to verify everything works
3. **Add more tests** as you develop new features
4. **Set up CI/CD** to run tests automatically

## Continuous Integration

For automated testing, consider adding:

```yaml
# .github/workflows/tests.yml (GitHub Actions example)
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.8'
      - run: pip install -r requirements.txt
      - run: python run_tests.py
```
