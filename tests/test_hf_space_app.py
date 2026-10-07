"""The Space runs on Hugging Face's Gradio SDK, which serves port 7860 itself.

Starting our own uvicorn server there crashed the Space ("address already in use"); the app must launch through
Gradio and attach its API routes to Gradio's FastAPI app. Checked on the source because gradio is not a test
dependency.
"""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "huggingface_space" / "app.py").read_text()
TREE = ast.parse(SRC)


def _calls():
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call):
            yield ast.unparse(node.func)


def test_space_does_not_start_its_own_server():
    assert not any(c.endswith("uvicorn.run") for c in _calls())


def test_space_launches_gradio_and_attaches_api_routes():
    calls = list(_calls())
    assert any(c.endswith(".launch") for c in calls)
    assert "attach_routes" in calls
    for path in ("/health", "/v1/chat/completions", "/v1/classify", "/v1/embed"):
        assert f'"{path}"' in SRC
