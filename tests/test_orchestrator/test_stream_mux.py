import io
from orchestrator.stream_mux import StreamMux


def test_stream_mux_tags_chunks_by_agent_id():
    buf = io.StringIO()
    mux = StreamMux(out=buf)
    mux.emit(agent_id="tool-agent", trace_id="t1", chunk="hello\n")
    mux.emit(agent_id="skill-agent", trace_id="t1", chunk="world\n")
    output = buf.getvalue()
    assert "[tool] hello" in output
    assert "[skill] world" in output


def test_stream_mux_handles_chunk_without_newline():
    buf = io.StringIO()
    mux = StreamMux(out=buf)
    mux.emit(agent_id="tool-agent", trace_id="t1", chunk="partial")
    mux.emit(agent_id="tool-agent", trace_id="t1", chunk=" done\n")
    output = buf.getvalue()
    assert output.count("[tool]") == 1
    assert "partial done" in output


def test_stream_mux_orchestrator_tag():
    buf = io.StringIO()
    mux = StreamMux(out=buf)
    mux.emit(agent_id="orchestrator", trace_id="t1", chunk="routing...\n")
    assert "[orchestrator] routing" in buf.getvalue()
