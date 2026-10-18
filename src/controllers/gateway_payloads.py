from __future__ import annotations

from collections.abc import Iterable
import re


def build_simulation_start_payload(environment_template_id: str, drone_rows: Iterable[dict]) -> dict:
    selections = []
    for row in drone_rows:
        selections.append(
            {
                "drone_type_option_id": row["drone_type_option_id"],
                "startup_placement_id": row["startup_placement_id"],
            }
        )
    return {
        "session": {
            "environment_template_id": environment_template_id,
            "drone_selections": selections,
        }
    }


def build_training_run_payload(
    *,
    package_id: str,
    recipe_id: str,
    run_name: str,
    agent_bindings: Iterable[dict],
    observable_ready_timeout_sec: float = 0.0,
) -> dict:
    return {
        "package_id": package_id,
        "recipe_id": recipe_id,
        "run_name": run_name,
        "agent_bindings": list(agent_bindings),
        "observable_ready_timeout_sec": observable_ready_timeout_sec,
    }


def generated_run_name(package_name: str, recipe_name: str) -> str:
    raw = f"{package_name}_{recipe_name}".strip("_")
    normalized = re.sub(r"[\s\\/:*?\"<>|]+", "_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def first_default_recipe_id(package_response: dict) -> str:
    value = package_response.get("default_recipe_id")
    if value:
        return str(value)
    return ""
