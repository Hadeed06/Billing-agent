from functools import lru_cache
from importlib import resources

# Base package where template files live
PKG = "src.core.prompts.templates"

@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    """
    Load a prompt by name. Supports subpaths like 'cigna/cigna_prompt_template'.
    Automatically appends '.txt' if not present.
    """
    filename = name if name.endswith(".txt") else f"{name}.txt"
    parts = filename.replace("\\", "/").split("/")
    try:
        return resources.files(PKG).joinpath(*parts).read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Prompt '{filename}' not found in {PKG}") from e

def render_prompt(name: str, **vars) -> str:
    text = load_prompt(name)
    return text.format(**vars) if vars else text
