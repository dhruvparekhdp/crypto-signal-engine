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
    assert "ssr_mode=False" in SRC  # SSR moves Python off port 7860 and the Space exited
    for path in ("/health", "/v1/chat/completions", "/v1/classify", "/v1/embed"):
        assert f'"{path}"' in SRC


def test_space_starts_on_zerogpu_hardware():
    # ZeroGPU kills an app without a @spaces.GPU function; the import must stay optional for CPU Basic
    assert "@spaces.GPU" in SRC and "except ImportError" in SRC


def test_space_provides_musl_for_the_llama_cpp_wheel():
    # the prebuilt CPU wheel needs libc.musl-x86_64.so.1, which the Space image lacks
    space = Path(__file__).resolve().parents[1] / "huggingface_space"
    assert "musl-dev" in (space / "packages.txt").read_text()
    assert "_musl_shim()" in SRC
    from scripts.hf_space_deploy import FILES
    assert "packages.txt" in FILES
