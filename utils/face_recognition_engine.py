"""
Face recognition engine using:
- MediaPipe Tasks (FaceDetector + FaceLandmarker) for detection/landmarks/liveness
- ArcFace ONNX (onnxruntime) for identity embeddings

Compatible with MediaPipe 0.10.30+ (where mediapipe.solutions was removed).
"""

import io
import os
import logging
import urllib.request
from typing import List, Optional, Tuple

import numpy as np
import cv2
from PIL import Image

import onnxruntime as ort

from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from config import Config

logger = logging.getLogger(__name__)


class FaceRecognitionEngine:
    def __init__(self):
        # ---- thresholds / config ----
        self.match_threshold = float(getattr(Config, "FACE_MATCH_THRESHOLD", 0.35))
        self.required_angles = int(getattr(Config, "REQUIRED_ANGLES", 3))

        # ArcFace ONNX embedding model (you provide this file)
        config_model_path = str(getattr(Config, "EMBEDDING_MODEL_PATH", "") or "").strip()
        env_model_path = str(os.getenv("EMBEDDING_MODEL_PATH") or "").strip()
        self.arcface_model_path = env_model_path or config_model_path or "models/arcface_r100.onnx"

        config_model_url = str(getattr(Config, "EMBEDDING_MODEL_URL", "") or "").strip()
        env_model_url = str(os.getenv("EMBEDDING_MODEL_URL") or "").strip()
        env_model_url_legacy = str(os.getenv("ARCFACE_MODEL_URL") or "").strip()
        self.arcface_model_url = env_model_url or env_model_url_legacy or config_model_url
        os.makedirs(os.path.dirname(self.arcface_model_path) or ".", exist_ok=True)

        # MediaPipe task models (we auto-download)
        self._assets_dir = os.path.join(os.path.dirname(__file__), "..", "models", "mediapipe_assets")
        os.makedirs(self._assets_dir, exist_ok=True)

        self.face_detector_model_path = os.path.join(self._assets_dir, "blaze_face_short_range.tflite")
        self.face_landmarker_model_path = os.path.join(self._assets_dir, "face_landmarker.task")

        # Official model hosting endpoints (Google storage)
        self.face_detector_model_url = (
            "https://storage.googleapis.com/mediapipe-models/face_detector/"
            "blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
        )
        self.face_landmarker_model_url = (
            "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
            "face_landmarker/float16/latest/face_landmarker.task"
        )

        # Download if missing
        self._download_if_missing(self.face_detector_model_path, self.face_detector_model_url)
        self._download_if_missing(self.face_landmarker_model_path, self.face_landmarker_model_url)

        # ---- MediaPipe Tasks: Face Detector ----
        det_base = python.BaseOptions(model_asset_path=self.face_detector_model_path)
        det_opts = vision.FaceDetectorOptions(base_options=det_base)
        self._face_detector = vision.FaceDetector.create_from_options(det_opts)

        # ---- MediaPipe Tasks: Face Landmarker ----
        lm_base = python.BaseOptions(model_asset_path=self.face_landmarker_model_path)
        lm_opts = vision.FaceLandmarkerOptions(
            base_options=lm_base,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            num_faces=1,
        )
        self._face_landmarker = vision.FaceLandmarker.create_from_options(lm_opts)

        # ---- ArcFace ONNX runtime ----
        if not os.path.exists(self.arcface_model_path):
            if self.arcface_model_url:
                self._download_if_missing(self.arcface_model_path, self.arcface_model_url)
            else:
                raise FileNotFoundError(
                    f"ArcFace ONNX model not found at '{self.arcface_model_path}'. "
                    "Set EMBEDDING_MODEL_URL (or ARCFACE_MODEL_URL) to auto-download on startup, "
                    "or provide the file manually."
                )

        self._ort_sess = ort.InferenceSession(self.arcface_model_path, providers=["CPUExecutionProvider"])
        ort_input = self._ort_sess.get_inputs()[0]
        self._ort_input_name = ort_input.name
        self._ort_input_shape = list(ort_input.shape or [])

        # Auto-detect model layout to avoid silent bad embeddings.
        self._arcface_layout = "nchw"
        if len(self._ort_input_shape) == 4:
            ch_axis = self._ort_input_shape[1]
            last_axis = self._ort_input_shape[-1]
            if isinstance(last_axis, int) and last_axis == 3:
                self._arcface_layout = "nhwc"
            elif isinstance(ch_axis, int) and ch_axis == 3:
                self._arcface_layout = "nchw"

        self._arcface_input_size = (112, 112)
        if len(self._ort_input_shape) == 4:
            if self._arcface_layout == "nchw":
                h_dim, w_dim = self._ort_input_shape[2], self._ort_input_shape[3]
            else:
                h_dim, w_dim = self._ort_input_shape[1], self._ort_input_shape[2]
            if isinstance(w_dim, int) and isinstance(h_dim, int) and w_dim > 0 and h_dim > 0:
                self._arcface_input_size = (w_dim, h_dim)

    # -----------------------------
    # Helpers
    # -----------------------------
    def _download_if_missing(self, path: str, url: str):
        if os.path.exists(path):
            return
        logger.info(f"Downloading model asset: {url} -> {path}")
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as e:
            raise RuntimeError(f"Failed to download {url}. Error: {e}")

    def _to_rgb_np(self, image) -> np.ndarray:
        """Convert PIL/bytes/np to RGB uint8 numpy array."""
        if isinstance(image, Image.Image):
            return np.array(image.convert("RGB"))
        if isinstance(image, (bytes, bytearray)):
            img = Image.open(io.BytesIO(image)).convert("RGB")
            return np.array(img)
        if isinstance(image, np.ndarray):
            arr = image
            if arr.ndim == 3 and arr.shape[2] == 4:
                arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2RGB)
            return arr.astype(np.uint8)
        raise TypeError("Unsupported image type")


    def _arcface_preprocess(self, face_rgb: np.ndarray) -> np.ndarray:
        """Face crop -> normalized tensor expected by ArcFace input layout."""
        img = cv2.resize(face_rgb, self._arcface_input_size, interpolation=cv2.INTER_LINEAR).astype(np.float32)
        img = (img / 127.5) - 1.0
        if self._arcface_layout == "nchw":
            img = np.transpose(img, (2, 0, 1))  # CHW
        img = np.expand_dims(img, axis=0)
        return img

    def _embed(self, face_rgb: np.ndarray) -> np.ndarray:
        inp = self._arcface_preprocess(face_rgb)
        out = self._ort_sess.run(None, {self._ort_input_name: inp})[0]
        emb = out[0].astype(np.float32)
        emb /= (np.linalg.norm(emb) + 1e-9)
        return emb

    def _cosine_distance(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(1.0 - np.dot(a, b))

    def _detect_bbox_xyxy(self, rgb: np.ndarray) -> Optional[Tuple[int, int, int, int, float]]:
        """
        Uses MediaPipe FaceDetector task.
        Returns (x1,y1,x2,y2,score) in pixels.
        """
        from mediapipe import Image as mp_Image
        from mediapipe import ImageFormat as mp_ImageFormat

        h, w = rgb.shape[:2]
        mp_img = mp_Image(image_format=mp_ImageFormat.SRGB, data=rgb)
        res = self._face_detector.detect(mp_img)
        if not res.detections:
            return None

        det = res.detections[0]
        score = float(det.categories[0].score) if det.categories else 0.0
        bb = det.bounding_box  # origin_x, origin_y, width, height (pixels)
        x1 = max(0, int(bb.origin_x))
        y1 = max(0, int(bb.origin_y))
        x2 = min(w, int(bb.origin_x + bb.width))
        y2 = min(h, int(bb.origin_y + bb.height))

        # light padding
        pad = int(0.12 * max(x2 - x1, y2 - y1))
        x1 = max(0, x1 - pad)
        y1 = max(0, y1 - pad)
        x2 = min(w, x2 + pad)
        y2 = min(h, y2 + pad)

        if x2 - x1 < 40 or y2 - y1 < 40:
            return None

        return x1, y1, x2, y2, score

    def _crop_face(self, rgb: np.ndarray, bbox: Tuple[int, int, int, int]) -> np.ndarray:
        x1, y1, x2, y2 = bbox
        return rgb[y1:y2, x1:x2].copy()

    def _landmarks_px(self, rgb: np.ndarray) -> Optional[np.ndarray]:
        """Returns (468,2) pixel landmarks or None."""
        from mediapipe import Image as mp_Image
        from mediapipe import ImageFormat as mp_ImageFormat

        h, w = rgb.shape[:2]
        mp_img = mp_Image(image_format=mp_ImageFormat.SRGB, data=rgb)
        res = self._face_landmarker.detect(mp_img)
        if not res.face_landmarks:
            return None

        lm = res.face_landmarks[0]  # list of NormalizedLandmark
        pts = np.array([[p.x * w, p.y * h] for p in lm], dtype=np.float32)
        return pts

    # -----------------------------
    # Public methods (drop-in)
    # -----------------------------
    def detect_face(self, image):
        """
        Returns: (success, face_location, face_encoding)
        face_location is (top, right, bottom, left) like your old engine.
        face_encoding is an ArcFace embedding vector.
        """
        try:
            rgb = self._to_rgb_np(image)
            det = self._detect_bbox_xyxy(rgb)
            if not det:
                return False, None, None

            x1, y1, x2, y2, _ = det
            face = self._crop_face(rgb, (x1, y1, x2, y2))
            emb = self._embed(face)

            # mimic old tuple (top,right,bottom,left)
            return True, (y1, x2, y2, x1), emb
        except Exception as e:
            logger.error(f"Face detect/embed error: {e}")
            return False, None, None

    def capture_multiple_angles(self, images: List):
        encodings = []
        for idx, img in enumerate(images):
            ok, _, emb = self.detect_face(img)
            if ok:
                encodings.append(emb)
            else:
                logger.warning(f"Failed to detect face in image {idx + 1}")

        if len(encodings) < self.required_angles:
            logger.error(f"Insufficient face encodings: {len(encodings)}/{self.required_angles}")
            return None
        return encodings

    def verify_identity(self, live_image, stored_encodings: List[np.ndarray]):
        ok, _, live_emb = self.detect_face(live_image)
        if not ok or live_emb is None or not stored_encodings:
            return False, 0.0

        dists = [self._cosine_distance(live_emb, se) for se in stored_encodings]
        best = min(dists)
        is_match = best <= self.match_threshold
        confidence = max(0.0, 1.0 - best)
        return is_match, confidence

    def validate_image_quality(self, image):
        try:
            rgb = self._to_rgb_np(image)
            h, w = rgb.shape[:2]
            if w < 200 or h < 200:
                return False, "Image resolution too low. Minimum 200x200 required."

            det = self._detect_bbox_xyxy(rgb)
            if not det:
                return False, "No face detected in image."

            x1, y1, x2, y2, _ = det
            face_area_ratio = ((x2 - x1) * (y2 - y1)) / float(w * h)
            if face_area_ratio < 0.05:
                return False, "Face too small in image."

            return True, "Image quality acceptable"
        except Exception as e:
            logger.error(f"Image validation error: {e}")
            return False, f"Image validation failed: {e}"

    # -----------------------------
    # Liveness (blink + head turn)
    # -----------------------------
    def _eye_aspect_ratio(self, pts: np.ndarray, eye: str) -> float:
        # FaceLandmarker uses same 468 topology as FaceMesh
        if eye == "left":
            idx = [33, 160, 158, 133, 153, 144]
        else:
            idx = [362, 385, 387, 263, 373, 380]

        p1, p2, p3, p4, p5, p6 = [pts[i] for i in idx]
        v1 = np.linalg.norm(p2 - p6)
        v2 = np.linalg.norm(p3 - p5)
        h = np.linalg.norm(p1 - p4) + 1e-6
        return float((v1 + v2) / (2.0 * h))

    def _nose_x_ratio(self, pts: np.ndarray) -> float:
        nose = pts[1]
        left = pts[234]
        right = pts[454]
        width = np.linalg.norm(right - left) + 1e-6
        return float((nose[0] - left[0]) / width)

    def basic_liveness_check(self, frames: List[Image.Image], challenge: Optional[str] = None):
        """
        Supported challenge strings:
        - blink
        - turn_left
        - turn_right
        """
        try:
            if not frames or len(frames) < 3:
                return False, "Not enough frames"

            all_pts = []
            for f in frames:
                rgb = self._to_rgb_np(f)
                pts = self._landmarks_px(rgb)
                if pts is None:
                    return False, "Face landmarks not found in all frames"
                all_pts.append(pts)

            if challenge == "blink":
                ears = []
                for pts in all_pts:
                    ear = (self._eye_aspect_ratio(pts, "left") + self._eye_aspect_ratio(pts, "right")) / 2.0
                    ears.append(ear)
                base = float(np.median(ears))
                min_ear = float(np.min(ears))
                if base <= 0:
                    return False, "Invalid EAR baseline"
                drop_ratio = min_ear / (base + 1e-6)
                if drop_ratio > 0.75:
                    return False, "No blink detected"
                return True, "ok"

            if challenge in ("turn_left", "turn_right"):
                ratios = [self._nose_x_ratio(pts) for pts in all_pts]
                delta = ratios[-1] - ratios[0]

                if challenge == "turn_left":
                    if delta > -0.06:
                        return False, "Left turn not detected"
                    return True, "ok"

                if challenge == "turn_right":
                    if delta < 0.06:
                        return False, "Right turn not detected"
                    return True, "ok"

            # fallback: require some motion
            nose_moves = []
            for i in range(1, len(all_pts)):
                nose_moves.append(np.linalg.norm(all_pts[i][1] - all_pts[i - 1][1]))
            if float(np.mean(nose_moves)) < 1.0:
                return False, "Frames too static"
            return True, "ok"

        except Exception as e:
            logger.error(f"Liveness check error: {e}")
            return False, "Liveness check failed"
