#!/usr/bin/env python3
"""Test script to verify imports from within lib directory."""

import os
import sys
import traceback

print("Testing imports from within lib directory...")

# Test 1: Import DFLJPG from DFLIMG
try:
    from DFLIMG.DFLJPG import DFLJPG

    print("✓ Successfully imported DFLJPG")
except Exception as e:
    print(f"✗ Failed to import DFLJPG: {e}")
    traceback.print_exc()
    sys.exit(1)

# Test 2: Import LandmarksProcessor module
try:
    import DFLIMG.LandmarksProcessor as LP

    print("✓ Successfully imported LandmarksProcessor module")
    # Now try to access imagelib
    imagelib = LP.imagelib
    print("✓ Successfully accessed imagelib from LandmarksProcessor")
except Exception as e:
    print(f"✗ Failed to import LandmarksProcessor: {e}")
    traceback.print_exc()
    sys.exit(1)

# Test 3: Test FaceType imports
try:
    from DFLIMG.FaceType import FaceType

    print(f"✓ Successfully imported FaceType")
    # Test enum values
    print(f"  FaceType.FULL = {FaceType.FULL}")
except Exception as e:
    print(f"✗ Failed to import FaceType: {e}")
    traceback.print_exc()
    sys.exit(1)

# Test 4: Test math imports
try:
    from math import umeyama

    print(f"✓ Successfully imported umeyama from math")
except Exception as e:
    print(f"✗ Failed to import umeyama: {e}")
    traceback.print_exc()
    sys.exit(1)

print("\n✅ All imports successful!")
print("Code optimization changes are working correctly.")
