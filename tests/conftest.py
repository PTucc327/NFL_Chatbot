"""
conftest.py — shared pytest configuration.

The chatbot test file must mock streamlit and google.genai at the sys.modules
level before importing chatbot.py. That mock must NOT bleed into test_utils.py
or test_api_client.py, which need the real src.utils and src.api_client.

pytest collects all files in one process, so import order matters.
We handle this by:
  - test_utils.py and test_api_client.py do their own direct imports via
    importlib, bypassing sys.modules entirely.
  - test_chatbot.py uses its own module-level mock setup (already done).
"""
import sys
import os

# Ensure the project root is on the path for all test files
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
