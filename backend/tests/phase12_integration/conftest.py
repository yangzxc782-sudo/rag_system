"""Allow isolated M6 collection to reuse the existing top-level test helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
