"""One runner shared by navigation and the batch page."""
from pathlib import Path

import streamlit as st

@st.cache_resource
def get_runner(root: str):
    """Cache the automatic-approval runner separately from the retired AI-review worker."""
    from topik_question_lab.step_03_generation.bulk_generation import BulkRunner
    return BulkRunner(root=Path(root))
