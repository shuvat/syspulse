"""SysPulse dashboard: hosts, CPU and memory charts, and anomalies.

Streamlit reruns this whole script on every interaction. The live part is a fragment that
reruns by itself every 5 seconds, so the page updates without a reload.
Run: streamlit run app.py
"""

from datetime import UTC, datetime

import streamlit as st

import data

ANOMALY_MINUTES = 60
STATUS_ICONS = {"online": "🟢 online", "silent": "🔴 silent"}

st.set_page_config(page_title="SysPulse", page_icon="📈", layout="wide")
st.title("SysPulse")

minutes = st.sidebar.selectbox("Chart window (minutes)", [5, 10, 30, 60], index=1)
st.sidebar.caption(f"API: {data.API_URL}")


# Shared by all browser sessions for 4 seconds, so several viewers (and the 5-second
# refresh) do not multiply the requests to the API. Exceptions are not cached.
@st.cache_data(ttl=4, show_spinner=False)
def load(minutes: int) -> tuple[list, dict, list]:
    hosts = data.api_get("/hosts")
    samples_by_host = {
        h["name"]: data.api_get(f"/hosts/{h['name']}/metrics", {"minutes": minutes})
        for h in hosts
    }
    anomalies = data.api_get("/anomalies", {"minutes": ANOMALY_MINUTES})
    return hosts, samples_by_host, anomalies


@st.fragment(run_every="5s")
def live_view(minutes: int) -> None:
    try:
        hosts, samples_by_host, anomalies = load(minutes)
    except data.ApiError as exc:
        st.error(f"{exc}. Is the server running?")
        return

    st.subheader("Hosts")
    table = data.hosts_frame(hosts, samples_by_host, datetime.now(UTC))
    table["status"] = table["status"].map(STATUS_ICONS)
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            "cpu_percent": st.column_config.NumberColumn("latest CPU %", format="%.1f"),
        },
    )

    # Long format (one row per sample, color by host): each host's line is drawn from its
    # own samples, so hosts that sample on different seconds do not break each other's
    # lines (a wide table with one column per host would have NaN in every other row).
    metrics = data.break_gaps(data.metrics_frame(samples_by_host))
    cpu_col, mem_col = st.columns(2)
    with cpu_col:
        st.subheader("CPU %")
        st.caption("Relative to each container's CPU limit")
        st.line_chart(metrics, x="ts", y="cpu_percent", color="host", x_label="", y_label="%")
    with mem_col:
        st.subheader("Memory %")
        st.caption("Memory used / total")
        st.line_chart(metrics, x="ts", y="mem_percent", color="host", x_label="", y_label="%")

    st.subheader(f"Anomalies (last {ANOMALY_MINUTES} minutes)")
    if anomalies:
        st.dataframe(
            anomalies,
            hide_index=True,
            column_order=["ts", "host", "type", "value", "message"],
            column_config={"value": st.column_config.NumberColumn(format="%.1f")},
        )
    else:
        st.info("No anomalies.")


live_view(minutes)
