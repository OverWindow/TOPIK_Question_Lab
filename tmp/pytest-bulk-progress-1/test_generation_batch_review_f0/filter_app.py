import streamlit as st
from topik_question_lab.ui.batch_ui import select_generation_batch
items=[{"batch_id":None,"id":1},{"batch_id":"batch-a","id":2},{"batch_id":"batch-b","id":3}]
filtered=select_generation_batch(items,"filter",lambda value:value["batch_id"])
st.write([item["id"] for item in filtered])
