from pathlib import Path

import streamlit as st


APP_DIR = Path(__file__).resolve().parent

navigation = st.navigation(
    [
        st.Page(APP_DIR / "app.py", title="TOPIK II 읽기", icon="📖", default=True),
        st.Page(APP_DIR / "listening_app.py", title="TOPIK II 듣기", icon="🎧"),
        st.Page(APP_DIR / "backup_restore_app.py", title="백업·복원", icon="💾"),
    ],
    position="sidebar",
)
navigation.run()
