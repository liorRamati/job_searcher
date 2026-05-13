import os

# Safety net: prevent accidental writes to production Sheets or live LLM calls
# during any test run. Modules check JOB_AGENT_ENV before making external calls.
os.environ.setdefault("JOB_AGENT_ENV", "test")
