"""Shared model loading helpers for configuration-driven model construction."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Union


# ImportSpec can be a module path string, a file path string, "module:attr_chain",
# a Path object pointing to a .py file, or Path pointing to any file path.
ImportSpec = Union[str, Path]

# Slot -> ordered import specs
MODEL_REGISTRY: dict[str, list[ImportSpec]] = {
    "asr": ["xtalk.speech.asr"],
    "llm_agent": ["xtalk.llm_agent"],
    "tts": ["xtalk.speech.tts"],
    "embeddings": ["xtalk.embeddings"],
    "speaker_encoder": ["xtalk.speech.speaker_encoder"],
    "captioner": ["xtalk.speech.captioner"],
    "caption_rewriter": ["xtalk.rewriter"],
    "vad": ["xtalk.speech.vad"],
    "speech_enhancer": ["xtalk.speech.speech_enhancer"],
    "speech_speed_controller": ["xtalk.speech.speech_speed_controller"],
    "turn_detector": ["xtalk.speech.turn_detector"],
}

# Cache for file-path modules to avoid re-importing the same file repeatedly
_FILE_MODULE_CACHE: dict[str, Any] = {}


def register_model_search_spec(
    *,
    slot: str,
    spec: ImportSpec,
    prepend: bool = True,
    registry: dict[str, list[ImportSpec]] | None = None,
) -> None:
    """Register an additional model lookup location for a registry slot.

    Parameters
    ----------
    slot : str
        Registry slot name such as ``"asr"`` or ``"llm_agent"``.
    spec : ImportSpec
        Import target to try when loading the slot.
    prepend : bool, optional
        Whether the new spec should be tried before existing registered specs.
    registry : dict[str, list[ImportSpec]] | None, optional
        Registry to mutate. When omitted, the shared ``MODEL_REGISTRY`` is used.
    """

    if not slot:
        raise ValueError("slot must be non-empty")
    if not spec:
        raise ValueError("spec must be non-empty")

    target_registry = registry if registry is not None else MODEL_REGISTRY
    paths = target_registry.get(slot)
    if paths is None:
        target_registry[slot] = [spec]
        return

    if spec in paths:
        return

    if prepend:
        paths.insert(0, spec)
    else:
        paths.append(spec)


def normalize_import_spec(spec: ImportSpec) -> str:
    """Normalize an import spec into a string representation.

    Parameters
    ----------
    spec : ImportSpec
        Import spec represented as a string or path.

    Returns
    -------
    str
        Normalized module path or absolute file path.
    """

    if isinstance(spec, Path):
        return str(spec.expanduser().resolve())
    if isinstance(spec, str):
        return spec
    raise TypeError(f"Invalid ImportSpec type: {type(spec)}")


def looks_like_file_path(spec: ImportSpec) -> bool:
    """Return whether an import spec should be treated as a file path.

    Parameters
    ----------
    spec : ImportSpec
        Import spec represented as a string or path.

    Returns
    -------
    bool
        ``True`` when the spec resembles a file path.
    """

    if isinstance(spec, Path):
        return True

    if not isinstance(spec, str):
        return False

    if spec.endswith(".py"):
        return True
    if os.path.isabs(spec):
        return True
    if (
        spec.startswith("." + os.sep)
        or spec.startswith(".." + os.sep)
        or spec.startswith("./")
        or spec.startswith("../")
    ):
        return True
    if os.sep in spec or (os.altsep and os.altsep in spec):
        return True
    return False


def import_from_file(file_path: str) -> Any:
    """Import a Python module from a file path.

    Parameters
    ----------
    file_path : str
        Python file path to import.

    Returns
    -------
    Any
        Imported Python module object.
    """

    file_abs = os.path.abspath(file_path)
    if not os.path.exists(file_abs):
        raise FileNotFoundError(f"Python file not found: {file_abs}")

    if file_abs in _FILE_MODULE_CACHE:
        return _FILE_MODULE_CACHE[file_abs]

    digest = hashlib.sha256(file_abs.encode("utf-8")).hexdigest()[:16]
    module_name = f"xtalk_userfile_{digest}"

    spec = importlib.util.spec_from_file_location(module_name, file_abs)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for file: {file_abs}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    _FILE_MODULE_CACHE[file_abs] = module
    return module


def resolve_attr_chain(obj: Any, chain: str) -> Any:
    """Resolve an attribute chain such as ``"a.b.c"`` on an object.

    Parameters
    ----------
    obj : Any
        Root module or object.
    chain : str
        Dot-delimited attribute chain.

    Returns
    -------
    Any
        Resolved attribute value.
    """

    current = obj
    for part in chain.split("."):
        current = getattr(current, part)
    return current


def import_candidate(spec: ImportSpec) -> Any:
    """Import a candidate module or object from an import spec.

    Parameters
    ----------
    spec : ImportSpec
        Candidate module path, file path, or ``module:attr`` reference.

    Returns
    -------
    Any
        Imported module or resolved object.
    """

    spec_str = normalize_import_spec(spec)

    if ":" in spec_str:
        base, attr_chain = spec_str.split(":", 1)
        base_obj = import_candidate(base)
        return resolve_attr_chain(base_obj, attr_chain)

    if looks_like_file_path(spec):
        return import_from_file(spec_str)

    return importlib.import_module(spec_str)


def init_model(model_config: dict | str, import_specs: list[ImportSpec]) -> Any:
    """Instantiate a model from configuration and import specs.

    Parameters
    ----------
    model_config : dict | str
        Model config containing ``type`` and optional ``params``, or a shorthand
        type string.
    import_specs : list[ImportSpec]
        Ordered import locations that may export the requested model class.

    Returns
    -------
    Any
        Instantiated model object, or ``None`` when ``model_config`` is empty.
    """

    if not model_config:
        return None

    if isinstance(model_config, dict) and "type" not in model_config:
        raise ValueError("Model config must contain 'type' field.")

    model_type = model_config["type"] if isinstance(model_config, dict) else model_config
    model_params = model_config.get("params", {}) if isinstance(model_config, dict) else {}

    errors: list[str] = []
    for spec in import_specs:
        try:
            container = import_candidate(spec)
        except Exception as exc:
            errors.append(f"{spec!r} import failed: {exc!r}")
            continue

        model_class = getattr(container, model_type, None)
        if model_class is None:
            continue
        if not isinstance(model_class, type):
            errors.append(
                f"{spec!r} has attribute {model_type!r} but it is not a class"
            )
            continue

        return model_class(**model_params)

    detail = "\n  - " + "\n  - ".join(errors) if errors else ""
    raise ValueError(
        f"Model class {model_type!r} not found. Tried specs: {import_specs}.{detail}"
    )


def init_registered_model(
    *,
    slot: str,
    model_config: dict | str,
    registry: dict[str, list[ImportSpec]] | None = None,
) -> Any:
    """Instantiate a model by looking up its import specs from a registry slot.

    Parameters
    ----------
    slot : str
        Registry slot such as ``"asr"`` or ``"tts"``.
    model_config : dict | str
        Model config containing ``type`` and optional ``params``, or a shorthand
        type string.
    registry : dict[str, list[ImportSpec]] | None, optional
        Registry to query. When omitted, the shared ``MODEL_REGISTRY`` is used.

    Returns
    -------
    Any
        Instantiated model object, or ``None`` when ``model_config`` is empty.
    """

    target_registry = registry if registry is not None else MODEL_REGISTRY
    import_specs = target_registry.get(slot)
    if import_specs is None:
        raise KeyError(f"Unknown model registry slot: {slot}")
    return init_model(model_config=model_config, import_specs=import_specs)


__all__ = [
    "ImportSpec",
    "MODEL_REGISTRY",
    "import_candidate",
    "init_model",
    "init_registered_model",
    "looks_like_file_path",
    "normalize_import_spec",
    "register_model_search_spec",
    "resolve_attr_chain",
]
