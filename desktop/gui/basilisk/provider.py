"""Module to manage AI providers general information."""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from .decorators import measure_time

if TYPE_CHECKING:
	from .provider_engine.base_engine import BaseEngine

log = logging.getLogger(__name__)


class ProviderAPIType(enum.StrEnum):
	"""Define different API types for AI providers."""

	OPENAI = enum.auto()
	ANTHROPIC = enum.auto()
	OLLAMA = enum.auto()
	GEMINI = enum.auto()
	LOOM = enum.auto()


@dataclass
class Provider:
	"""Represents an AI provider.

	Attributes:
		id: The unique identifier for the provider
		name: The name of the provider
		api_type: The type of API the provider uses
		engine_cls_path: The path to the provider's engine class
		base_url: The base URL for the provider's API
		organization_mode_available: Whether the provider supports organization mode
		require_api_key: Whether the provider requires an API key
		custom: Whether the provider is custom
		env_var_name_api_key: The environment variable name to get the API key
	"""

	id: str
	name: str
	api_type: ProviderAPIType
	engine_cls_path: str
	base_url: str | None = field(default=None)
	organization_mode_available: bool = field(default=False)
	require_api_key: bool = field(default=True)
	allow_custom_base_url: bool = field(default=False)
	env_var_name_api_key: str | None = field(default=None)
	env_var_name_organization_key: str | None = field(default=None)

	@cached_property
	@measure_time
	def engine_cls(self) -> type[BaseEngine]:
		"""Get the provider's engine class.

		Returns:
			The provider's engine class
		"""
		try:
			module_path, class_name = self.engine_cls_path.rsplit(".", 1)
			module = __import__(module_path, fromlist=[class_name])
			cls = getattr(module, class_name)
			return cls
		except ImportError as e:
			log.error("Error importing engine class: %s", e)
			raise
		except AttributeError as e:
			log.error("Error getting engine class: %s", e)
			raise


# The desktop preview exposes only its configured local retained-practice reader.
# Other upstream engine source is retained for provenance, not registered here.
providers = [
	Provider(
		id="loom",
		name="Loom local",
		api_type=ProviderAPIType.LOOM,
		require_api_key=False,
		engine_cls_path="basilisk.provider_engine.loom_engine.LoomEngine",
	),
]


def get_providers(**kwargs: dict[str, Any]) -> Iterable[Provider]:
	"""Get providers by criteria.

	Args:
		**kwargs: Criteria to filter providers by (e.g. name="OpenAI")

	Returns:
		Iterable of providers that match the criteria
	"""
	match_providers = providers
	for k, v in kwargs.items():
		match_providers = filter(
			lambda x: getattr(x, k, None) == v, match_providers
		)
	return match_providers


def get_provider(**kwargs: dict[str, Any]) -> Provider:
	"""Get provider by criteria.

	Args:
		**kwargs: Criteria to filter providers by (e.g. name="OpenAI")

	Returns:
		The provider that matches the criteria

	Raises:
		ValueError: If no provider is found or multiple providers are found
	"""
	match_providers = list(get_providers(**kwargs))
	if not match_providers or len(match_providers) == 0:
		raise ValueError("No provider found")
	if len(match_providers) > 1:
		raise ValueError("Multiple providers found")
	return match_providers[0]
