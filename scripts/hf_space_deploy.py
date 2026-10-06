"""Upload huggingface_space/ to the owner's Space, make it private, ask for CPU Basic, and save its address.

The token is the "Hugging Face token" on /keys (stored encrypted in the database), never typed here.

    python -m scripts.hf_space_deploy            # on the server, uses the saved token and Space name
"""
from __future__ import annotations

import asyncio
from pathlib import Path

FILES = ("app.py", "requirements.txt", "README.md")


def deploy(token: str, repo_id: str, folder: str = "huggingface_space") -> dict:
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    who = api.whoami().get("name")
    api.create_repo(repo_id, repo_type="space", space_sdk="gradio", private=True, exist_ok=True)
    api.upload_folder(folder_path=folder, repo_id=repo_id, repo_type="space", allow_patterns=list(FILES),
                      commit_message="crypto analyst LLM: llama.cpp on CPU Basic (from crypto-signal-engine)")
    steps = {"user": who, "uploaded": [f for f in FILES if (Path(folder) / f).exists()]}
    try:
        api.update_repo_settings(repo_id, private=True, repo_type="space")
        steps["private"] = True
    except Exception as e:  # noqa: BLE001
        steps["private"] = f"could not set: {str(e)[:80]}"
    try:
        api.request_space_hardware(repo_id, "cpu-basic")
        steps["hardware"] = "cpu-basic"
    except Exception as e:  # noqa: BLE001
        steps["hardware"] = f"could not set: {str(e)[:80]}"
    info = api.space_info(repo_id)
    host = getattr(info, "host", None) or f"https://{repo_id.replace('/', '-').replace('_', '-').replace('.', '-').lower()}.hf.space"
    steps["url"] = host if host.startswith("http") else f"https://{host}"
    rt = getattr(info, "runtime", None)
    steps["stage"] = getattr(rt, "stage", None)
    return steps


async def deploy_from_saved_settings() -> dict:
    """Uses the token and Space name saved on /keys, then saves the Space address there too."""
    from config.overrides import apply
    from config.settings import settings
    from scheduler.keys_page import _stored
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    apply(settings, await _stored())
    tok = settings.hf_api_token.get_secret_value() if settings.hf_api_token else ""
    repo = (settings.hf_space_repo or "").strip()
    if not tok or not repo:
        return {"error": "save the Hugging Face token and the Space name on /keys first"}
    out = await asyncio.to_thread(deploy, tok, repo)
    async with AsyncSessionFactory() as s:
        await Repository(s).save_app_settings({"hf_base_url": out["url"]})
    settings.hf_base_url = out["url"]
    return out


if __name__ == "__main__":
    print(asyncio.run(deploy_from_saved_settings()))
