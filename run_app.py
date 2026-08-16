"""Project-root Streamlit entrypoint.

Run: streamlit run run_app.py
"""

from __future__ import annotations

from app.ui import main

if __name__ == "__main__":
    main()
else:
    # Streamlit executes the file as a script; call main on import/run.
    main()
