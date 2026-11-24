# visual_embed.py
# Optional v2 learned visual embedding sidecar (MobileNetV3-Large fine-tuned on Scryfall printings
# with a spatial Conv1x1 -> Flatten -> 128-d head, run via OpenCV's dnn module - no PyTorch needed
# on the server).
#
# Why a sidecar instead of replacing phash_rgb_v1:
#   On 10,697 real captures + 129 hard AI-read captures (scripts/experiments/image_match/),
#   phash_rgb_v1 alone handles ~98% of cards in 4 ms with 0.3% wrong-card rate, while
#   0.5 * phash_rgb + 0.5 * ft_int8 called ONLY on the ~2% of cards where phash_rgb returns
#   'review' cuts review rates 2.5x-4x (collection 1.44% -> 0.59%, scanned 2.56% -> 0.64%,
#   hard cases 18.6% -> 7.75%) with 0 added real errors and ~0.2 ms amortized CPU time.
#
# Int8 storage:
#   Each 128-d L2-normalized embedding is quantized to int8 ([-127, 127], 128 bytes/printing =
#   ~14.4 MB for 112k printings). Because cosine similarity normalizes each row by its own L2 norm,
#   the per-row quantization scale cancels out identically:
#       cos(dequant(q), query) = (q . query) / ||q||_2
#   Measured on 112,765 printings: int8 retrieval matches fp32 to 0.00-0.04 points (essentially lossless).

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from card_scanner.config import Config
from card_scanner.core import image_index

logger = logging.getLogger('ai')

DESCRIPTOR = 'ft_mnv3_int8_v2'
DIMS = 128
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

# Looked for beside this module first (packaged), then in the user's data directory (data/embed_net.onnx)
MODEL_FILE = Path(__file__).with_name('embed_net.onnx')
DATA_MODEL_NAME = 'embed_net.onnx'


def quantize_int8(vector):
    """Quantize a 1-D float embedding (DIMS,) into int8 [-127, 127] (128 bytes)."""
    v = np.asarray(vector, dtype=np.float32).reshape(DIMS)
    peak = float(np.abs(v).max())
    if not np.isfinite(peak) or peak <= 0.0:
        return np.zeros(DIMS, dtype=np.int8)
    return np.clip(np.round(v * (127.0 / peak)), -127, 127).astype(np.int8)


def row_inv_norms(embeds_int8, chunk=16384):
    """1 / ||row||_2 (float32, (N,)) for an (N, DIMS) int8 embedding table, computed in bounded chunks."""
    n = len(embeds_int8)
    out = np.zeros(n, dtype=np.float32)
    for start in range(0, n, chunk):
        block = embeds_int8[start:start + chunk].astype(np.float32)
        norms = np.linalg.norm(block, axis=1)
        nz = norms > 0
        out[start:start + len(block)][nz] = 1.0 / norms[nz]
    return out


def cosine_similarities(embeds_int8, inv_norms, query, chunk=16384):
    """Cosine similarities (float32, (N,)) between an (N, DIMS) int8 table and a unit float32 query (DIMS,)."""
    n = len(embeds_int8)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    q = np.asarray(query, dtype=np.float32).reshape(DIMS)
    q_norm = float(np.linalg.norm(q))
    if not np.isfinite(q_norm) or q_norm <= 0.0:
        return np.zeros(n, dtype=np.float32)
    q = q / q_norm
    sims = np.empty(n, dtype=np.float32)
    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        sims[start:end] = embeds_int8[start:end].astype(np.float32) @ q
    sims *= inv_norms
    return sims


class VisualEmbedder:
    """OpenCV dnn wrapper for the v2 card embedding ONNX model. Thread-safe via a single lock."""

    def __init__(self, path=MODEL_FILE):
        self.path = Path(path)
        self.net = cv2.dnn.readNetFromONNX(str(self.path))
        self._lock = threading.Lock()

    def embed(self, rgb):
        """L2-normalized float32 embedding (DIMS,) of an RGB card picture (matching finetune.py's to_tensor)."""
        prepared = image_index.prepare(rgb)
        blob = ((prepared.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
        with self._lock:
            self.net.setInput(np.ascontiguousarray(blob))
            raw = self.net.forward().reshape(-1).astype(np.float32)
        if raw.shape != (DIMS,):
            raise ValueError(f"Expected {DIMS}-d embedding from {self.path.name}, got {raw.shape}")
        norm = float(np.linalg.norm(raw))
        return raw / norm if norm > 0.0 else np.zeros(DIMS, dtype=np.float32)

    def embed_int8(self, rgb):
        """Quantized int8 embedding (DIMS,) for storing in the sidecar index."""
        return quantize_int8(self.embed(rgb))


def find_model(path=None):
    """Path to the v2 ONNX model if available (explicit path -> beside module -> data/embed_net.onnx), else None."""
    candidates = [Path(path)] if path else [MODEL_FILE, Config.DATA_DIR / DATA_MODEL_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load(path=None):
    """Load VisualEmbedder if the ONNX file exists and OpenCV can read it, else None (never fails startup)."""
    found = find_model(path)
    if found is None:
        return None
    try:
        return VisualEmbedder(found)
    except Exception as e:
        logger.warning(f"Visual embedding model {found.name} could not be loaded ({e}) - using colour hash alone")
        return None
