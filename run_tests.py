"""Test runner script"""
import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def run_all_tests():
    """Run all test suites"""
    # Discover and run all tests
    loader = unittest.TestLoader()
    suite = loader.discover('tests', pattern='test_*.py')
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Return exit code
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    print("=" * 60)
    print("Facial Recognition Attendance System - Test Suite")
    print("=" * 60)
    print()
    
    exit_code = run_all_tests()
    
    print()
    print("=" * 60)
    if exit_code == 0:
        print("All tests passed!")
    else:
        print("Some tests failed. Check output above for details.")
    print("=" * 60)
    
    sys.exit(exit_code)
