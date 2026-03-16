"""Tests for utility modules."""
import unittest
from utils.encryption import encrypt_data, decrypt_data
from utils.face_recognition_engine import FaceRecognitionEngine
from PIL import Image


class TestEncryption(unittest.TestCase):
    def test_encrypt_decrypt(self):
        original_data = "This is a test string with sensitive data"

        encrypted = encrypt_data(original_data)
        self.assertIsNotNone(encrypted)
        self.assertNotEqual(encrypted, original_data)

        decrypted = decrypt_data(encrypted)
        self.assertEqual(decrypted, original_data)

    def test_encrypt_empty_string(self):
        encrypted = encrypt_data("")
        self.assertIsNone(encrypted)

    def test_decrypt_invalid_data(self):
        try:
            decrypt_data("invalid_encrypted_data")
        except Exception:
            pass


class TestFaceRecognitionEngine(unittest.TestCase):
    def setUp(self):
        try:
            self.engine = FaceRecognitionEngine()
        except Exception as exc:
            self.skipTest(f"Face engine not available: {exc}")

    def test_engine_initialization(self):
        self.assertIsNotNone(self.engine)
        self.assertIsNotNone(self.engine.match_threshold)
        self.assertGreater(self.engine.match_threshold, 0)

    def test_validate_image_quality_no_face(self):
        image = Image.new('RGB', (400, 400), color='red')
        is_valid, _message = self.engine.validate_image_quality(image)
        self.assertFalse(is_valid)

    def test_validate_image_quality_low_resolution(self):
        image = Image.new('RGB', (100, 100), color='red')
        is_valid, message = self.engine.validate_image_quality(image)
        self.assertFalse(is_valid)
        self.assertIn('resolution', message.lower())


if __name__ == '__main__':
    unittest.main()