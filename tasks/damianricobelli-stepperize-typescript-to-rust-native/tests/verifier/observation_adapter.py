"""Generic, language-neutral boundary for migration observation reports."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Protocol

try:
    from .differential import compare as compare_values
    from .observations import normalize_observation_report, observation_contract_errors
except ImportError:  # Standalone Runtime Draft layout.
    from differential import compare as compare_values
    from observations import normalize_observation_report, observation_contract_errors


@dataclasses.dataclass(frozen=True)
class AdapterDiagnostics:
    status: str
    errors: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ComparisonResult:
    status: str
    source: object = None
    target: object = None
    errors: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ScopeResult:
    unit_id: str
    status: str
    observation_ids: tuple[str, ...] = ()
    missing_observation_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ObservationAdapter(Protocol):
    def validate_contract(self, contract: object) -> AdapterDiagnostics: ...

    def normalize_report(
        self,
        payload: Mapping[str, object],
        *,
        role: str,
        config: Mapping[str, object],
        returncode: int,
        report_path: str,
    ) -> dict[str, Any]: ...

    def compare(
        self,
        source: Mapping[str, object],
        target: Mapping[str, object],
        policy: Mapping[str, object],
    ) -> ComparisonResult: ...

    def inspect_scope(
        self,
        unit: Mapping[str, object],
        observations: Sequence[Mapping[str, object]],
    ) -> ScopeResult: ...


class GenericObservationAdapter:
    """Use only typed contract data and v6 reports; never dispatch on language."""

    def validate_contract(self, contract: object) -> AdapterDiagnostics:
        contract = _contract_mapping(contract)
        errors: list[str] = []
        if not isinstance(contract, Mapping):
            return AdapterDiagnostics("FAIL", ("migration contract must be an object",))
        if not isinstance(contract.get("identity"), Mapping):
            errors.append("migration contract identity is required")
        requirements = contract.get("requirements")
        if not isinstance(requirements, Sequence) or isinstance(requirements, (str, bytes)):
            errors.append("migration contract requirements must be an array")
        else:
            seen: set[str] = set()
            for row in requirements:
                if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
                    errors.append("migration contract requirement ID is invalid")
                    continue
                identifier = str(row["id"])
                if identifier in seen:
                    errors.append(f"migration contract requirement is duplicated: {identifier}")
                seen.add(identifier)
        units = contract.get("migration_units")
        if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
            errors.append("migration contract migration_units must be an array")
        else:
            for row in units:
                if (
                    not isinstance(row, Mapping)
                    or not isinstance(row.get("id"), str)
                    or not isinstance(row.get("requirement_ids"), Sequence)
                    or isinstance(row.get("requirement_ids"), (str, bytes))
                ):
                    errors.append("migration contract migration unit is invalid")
        return AdapterDiagnostics("FAIL" if errors else "PASS", tuple(errors))

    def normalize_report(
        self,
        payload: Mapping[str, object],
        *,
        role: str,
        config: Mapping[str, object],
        returncode: int,
        report_path: str,
    ) -> dict[str, Any]:
        return normalize_observation_report(
            payload,
            role=role,
            returncode=returncode,
            report_path=report_path,
            config=config,
        )

    def validate_report(
        self,
        payload: Mapping[str, object],
        config: Mapping[str, object],
        *,
        actual_returncode: int | None = None,
        accepted_ids: Collection[str] | None = None,
    ) -> tuple[str, ...]:
        return observation_contract_errors(
            payload,
            config,
            actual_returncode=actual_returncode,
            accepted_ids=accepted_ids,
        )

    def compare(
        self,
        source: Mapping[str, object],
        target: Mapping[str, object],
        policy: Mapping[str, object],
    ) -> ComparisonResult:
        try:
            source_value = _typed_json_value(source.get("value"))
            target_value = _typed_json_value(target.get("value"))
            result = compare_values(source_value, target_value, policy)
        except (TypeError, ValueError) as exc:
            return ComparisonResult("INCONCLUSIVE", errors=(str(exc),))
        return ComparisonResult(
            status=str(result["status"]),
            source=result.get("source"),
            target=result.get("target"),
        )

    def inspect_scope(
        self,
        unit: Mapping[str, object],
        observations: Sequence[Mapping[str, object]],
    ) -> ScopeResult:
        unit_id = str(unit.get("id", ""))
        required = unit.get("observation_ids")
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            return ScopeResult(unit_id, "INCONCLUSIVE", errors=("unit observation_ids are invalid",))
        required_ids = tuple(dict.fromkeys(str(item) for item in required))
        actual = {str(row.get("id")): row for row in observations if isinstance(row, Mapping)}
        missing = tuple(identifier for identifier in required_ids if identifier not in actual)
        if missing:
            return ScopeResult(unit_id, "INCONCLUSIVE", required_ids, missing)
        statuses = [str(actual[identifier].get("status", "INCONCLUSIVE")) for identifier in required_ids]
        status = (
            "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
            else "INCONCLUSIVE" if not statuses or any(item not in {"PASS", "FAIL"} for item in statuses)
            else "FAIL" if "FAIL" in statuses else "PASS"
        )
        return ScopeResult(unit_id, status, required_ids)


def _typed_json_value(value: object) -> object:
    if not isinstance(value, Mapping) or value.get("type") != "json" or "value" not in value:
        raise ValueError("observation value must be a typed JSON envelope")
    return value["value"]


def _contract_mapping(contract: object) -> Mapping[str, object]:
    if isinstance(contract, Mapping):
        return contract
    as_dict = getattr(contract, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
        if isinstance(value, Mapping):
            return value
    return {}


__all__ = [
    "AdapterDiagnostics",
    "ComparisonResult",
    "GenericObservationAdapter",
    "ObservationAdapter",
    "ScopeResult",
]
