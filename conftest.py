import sys
import os

# Add repo root to sys.path so tests can import migrate_agy_sessions
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

