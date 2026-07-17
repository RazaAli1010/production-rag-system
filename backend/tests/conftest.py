import os

# Settings.JWT_SECRET is required with no default (a defaulted signing key would ship as a
# vulnerability), and `settings = Settings()` runs at import. Every suite therefore needs one in the
# environment before any test module imports app.core.settings. setdefault, so a real CI/dev value
# still wins.
os.environ.setdefault("JWT_SECRET", "test-only-not-a-real-secret")
