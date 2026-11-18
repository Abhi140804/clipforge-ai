"""Face detector backends, discovered at runtime.

Order:
  1. YuNet (OpenCV FaceDetectorYN) when FACE_MODEL_PATH points at an ONNX model
     (e.g. face_detection_yunet_2023mar.onnx from the OpenCV model zoo)
  2. Haar cascade shipped with standard `opencv-python` wheels (cv2.data.haarcascades)
  3. A cascade xml given via FACE_CASCADE_PATH
If none loads, `available()` is False and callers fall back to a blurred-background layout.
"""
import os
import threading

import cv2
import numpy as np

_lock = threading.Lock()
_state = {"backend": None, "obj": None, "checked": False}
_yunet_local = threading.local()


def _try_init():
    if _state["checked"]:
        return
    with _lock:
        if _state["checked"]:
            return
        model = os.environ.get("FACE_MODEL_PATH", "")
        if model and os.path.exists(model) and hasattr(cv2, "FaceDetectorYN"):
            try:
                cv2.FaceDetectorYN.create(model, "", (320, 320), 0.7)
                _state.update(backend="yunet", obj=model)
            except Exception:
                pass
        if _state["backend"] is None:
            paths = []
            hc = getattr(getattr(cv2, "data", None), "haarcascades", "")
            if hc:
                paths.append(hc + "haarcascade_frontalface_default.xml")
            if os.environ.get("FACE_CASCADE_PATH"):
                paths.append(os.environ["FACE_CASCADE_PATH"])
            for p in paths:
                if os.path.exists(p):
                    c = cv2.CascadeClassifier(p)
                    if not c.empty():
                        _state.update(backend="cascade", obj=c)
                        break
        _state["checked"] = True


def available() -> bool:
    _try_init()
    return _state["backend"] is not None


def backend_name() -> str:
    _try_init()
    return _state["backend"] or "none"


def detect_faces(gray: np.ndarray) -> list[tuple]:
    """Returns [(cx, cy, w, h)] in pixels of `gray` (uint8, HxW)."""
    _try_init()
    h, w = gray.shape[:2]
    if _state["backend"] == "cascade":
        faces = _state["obj"].detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(max(24, int(w * 0.05)),) * 2)
        return [(x + fw / 2, y + fh / 2, fw, fh) for (x, y, fw, fh) in faces]
    if _state["backend"] == "yunet":
        det = getattr(_yunet_local, "det", None)
        if det is None:
            det = _yunet_local.det = cv2.FaceDetectorYN.create(_state["obj"], "", (w, h), 0.7)
        det.setInputSize((w, h))
        _, res = det.detect(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
        if res is None:
            return []
        return [(x + fw / 2, y + fh / 2, fw, fh) for (x, y, fw, fh) in res[:, :4]]
    return []
