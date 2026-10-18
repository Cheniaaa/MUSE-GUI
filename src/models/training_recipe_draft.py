from __future__ import annotations

from copy import deepcopy

import yaml


class TrainingRecipeDraft:
    def __init__(self, recipe: dict, training_config: dict):
        self._recipe = deepcopy(recipe)
        self._training_config = deepcopy(training_config)
        self._agent_configs, self._runtime_bindings = self._load_agent_configs(self._training_config)
        self._revision = 0
        self._dirty = False
        self._pending_save_revision: int | None = None

    @classmethod
    def from_recipe_payload(cls, payload: dict) -> "TrainingRecipeDraft":
        recipe = payload.get("recipe", payload) if isinstance(payload, dict) else {}
        if not isinstance(recipe, dict):
            recipe = {}
        try:
            training_config = yaml.safe_load(recipe.get("training_config_yaml") or "{}") or {}
        except yaml.YAMLError:
            training_config = {}
        if not isinstance(training_config, dict):
            training_config = {}
        return cls(recipe, training_config)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def save_pending(self) -> bool:
        return self._pending_save_revision is not None

    @property
    def pending_save_revision(self) -> int | None:
        return self._pending_save_revision

    def agent_config(self, agent_id: str) -> dict:
        return deepcopy(self._agent_configs.get(str(agent_id), {}))

    def runtime_binding(self, agent_id: str) -> dict:
        return deepcopy(self._runtime_bindings.get(str(agent_id), {}))

    def set_agent_config(self, agent_id: str, config: dict) -> None:
        agent_key = str(agent_id).strip()
        if not agent_key:
            return
        next_config = deepcopy(config) if isinstance(config, dict) else {}
        next_config.pop("entity", None)
        if self._agent_configs.get(agent_key, {}) == next_config:
            return
        self._agent_configs[agent_key] = next_config
        self._mark_dirty()

    def set_runtime_binding(self, agent_id: str, binding: dict) -> None:
        agent_key = str(agent_id).strip()
        if not agent_key:
            return
        next_binding = deepcopy(binding) if isinstance(binding, dict) else {}
        if self._runtime_bindings.get(agent_key, {}) == next_binding:
            return
        if next_binding:
            self._runtime_bindings[agent_key] = next_binding
        else:
            self._runtime_bindings.pop(agent_key, None)

    def save_payload(self) -> dict:
        recipe = deepcopy(self._recipe)
        config = deepcopy(self._training_config)
        config["agents"] = deepcopy(self._agent_configs)
        recipe["training_config_yaml"] = yaml.safe_dump(
            config,
            sort_keys=False,
            allow_unicode=True,
        )
        return {"recipe": recipe}

    def begin_save(self) -> int:
        self._pending_save_revision = self._revision
        return self._revision

    def finish_save(self, revision: int, succeeded: bool) -> None:
        if self._pending_save_revision == revision:
            self._pending_save_revision = None
        if succeeded and revision == self._revision:
            self._dirty = False

    def _mark_dirty(self) -> None:
        self._revision += 1
        self._dirty = True

    @staticmethod
    def _load_agent_configs(training_config: dict) -> tuple[dict[str, dict], dict[str, dict]]:
        agents_config = training_config.get("agents") if isinstance(training_config, dict) else {}
        if not isinstance(agents_config, dict):
            return {}, {}
        result: dict[str, dict] = {}
        runtime_bindings: dict[str, dict] = {}
        for agent_id, config in agents_config.items():
            if isinstance(config, dict):
                agent_key = str(agent_id)
                next_config = deepcopy(config)
                entity = next_config.pop("entity", None)
                if isinstance(entity, dict) and entity:
                    runtime_bindings[agent_key] = deepcopy(entity)
                result[agent_key] = next_config
        return result, runtime_bindings
