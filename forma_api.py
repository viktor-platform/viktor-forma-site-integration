import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import requests


API_BASE_URL = "https://developer.api.autodesk.com"
VALID_REGIONS = {"US", "EMEA"}


class FormaApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass(frozen=True)
class ProposalUrnParts:
    project_id: str
    proposal_id: str
    revision_id: str


@dataclass(frozen=True)
class ProposalReference:
    display_name: str
    urn: str
    proposal_id: str
    revision_id: str


def parse_element_urn(urn: str, expected_type: str | None = None) -> tuple[str, str, str, str]:
    parts = urn.strip().split(":")
    if len(parts) != 6 or parts[:2] != ["urn", "adsk-forma-elements"]:
        raise ValueError("Expected a complete Forma element URN.")
    element_type, project_id, element_id, revision = parts[2:]
    if expected_type and element_type != expected_type:
        raise ValueError(f'Expected a Forma "{expected_type}" element URN.')
    return element_type, project_id, element_id, revision


def parse_proposal_urn(urn: str) -> ProposalUrnParts:
    normalized = urn.strip()
    parts = normalized.split(":")
    if (
        len(parts) != 6
        or parts[0] != "urn"
        or parts[1] != "adsk-forma-elements"
        or parts[2] != "proposal"
        or not parts[3]
        or not parts[4]
        or not parts[5]
    ):
        raise ValueError(
            "Expected a full Forma proposal URN in the form "
            "urn:adsk-forma-elements:proposal:<project-id>:<proposal-id>:<revision-id>."
        )
    return ProposalUrnParts(
        project_id=parts[3],
        proposal_id=parts[4],
        revision_id=parts[5],
    )


def build_proposal_update_payload(
    proposal_element: dict[str, Any],
    new_element_urns: Iterable[str],
) -> dict[str, Any]:
    """Preserve the proposal composition and append new element references."""

    raw_children = proposal_element.get("children")
    if not isinstance(raw_children, list):
        raise FormaApiError("The proposal element does not contain a children array.")

    properties = proposal_element.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    flags = properties.get("flags")
    flags = flags if isinstance(flags, dict) else {}

    terrain: dict[str, Any] | None = None
    base: dict[str, Any] | None = None
    ordinary_children: list[dict[str, Any]] = []

    for raw_child in raw_children:
        if not isinstance(raw_child, dict):
            continue
        child_urn = raw_child.get("urn")
        child_key = raw_child.get("key")
        if not isinstance(child_urn, str) or not child_urn:
            raise FormaApiError("A proposal child is missing its URN.")
        if not isinstance(child_key, str) or not child_key:
            raise FormaApiError(f'Proposal child "{child_urn}" is missing its key.')
        child = {"key": child_key, "urn": child_urn}
        if raw_child.get("transform") is not None:
            child["transform"] = raw_child["transform"]
        child_flags = flags.get(child_key)
        child_flags = child_flags if isinstance(child_flags, dict) else {}

        if ":terrain:" in child_urn:
            terrain = child
        elif child_flags.get("base") is True or ":base:" in child_urn:
            base = child
        else:
            ordinary_children.append(child)

    if terrain is None:
        raise FormaApiError(
            "The proposal's terrain reference could not be identified. "
            "Open the proposal in Forma once and verify that its site terrain exists."
        )
    if base is None:
        raise FormaApiError(
            "The proposal's base reference could not be identified from its flags."
        )

    for element_urn in new_element_urns:
        if not isinstance(element_urn, str) or not element_urn:
            raise FormaApiError(
                "A created geometry response did not contain a valid URN."
            )
        ordinary_children.append(
            {
                "key": f"viktor-block-{uuid.uuid4().hex}",
                "urn": element_urn,
            }
        )

    name = properties.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "Forma proposal"

    return {
        "name": name,
        "terrain": terrain,
        "base": base,
        "children": ordinary_children,
    }


class FormaClient:
    def __init__(
        self,
        access_token: str,
        *,
        region: str,
        timeout_seconds: float = 45.0,
        session: requests.Session | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("APS access token cannot be empty.")
        normalized_region = region.strip().upper()
        if normalized_region not in VALID_REGIONS:
            raise ValueError('Forma region must be either "US" or "EMEA".')

        self._access_token = access_token
        self._region = normalized_region
        self._timeout_seconds = timeout_seconds
        self._session = session or requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-Ads-Region": self._region,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def list_proposals(
        self, project_id: str, *, limit: int = 100
    ) -> list[ProposalReference]:
        payload = self._request_json(
            "GET",
            "/forma/proposal/v1alpha/proposals",
            project_id=project_id,
            query={"limit": str(limit)},
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise FormaApiError(
                "List proposals response did not contain a results array."
            )

        proposals: list[ProposalReference] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            urn = item.get("urn")
            if not isinstance(urn, str):
                continue
            try:
                parts = parse_proposal_urn(urn)
            except ValueError:
                continue
            display_name = (
                item.get("displayName")
                or item.get("name")
                or item.get("title")
                or parts.proposal_id
            )
            proposals.append(
                ProposalReference(
                    display_name=str(display_name),
                    urn=urn,
                    proposal_id=parts.proposal_id,
                    revision_id=parts.revision_id,
                )
            )
        return proposals

    def create_basic_geometries(
        self, project_id: str, elements: list[dict[str, Any]]
    ) -> list[str]:
        if not elements:
            raise ValueError("At least one geometry element is required.")
        payload = self._request_json(
            "POST",
            "/forma/basic/v1alpha/geometries/batch-create",
            project_id=project_id,
            json_body=elements,
        )
        records: Any = payload
        if isinstance(payload, dict):
            records = payload.get("results", payload.get("elements"))
        if not isinstance(records, list):
            raise FormaApiError(
                "Batch-create response did not contain the expected array of "
                "geometry URNs."
            )

        urns: list[str] = []
        for record in records:
            if isinstance(record, str):
                urns.append(record)
            elif isinstance(record, dict) and isinstance(record.get("urn"), str):
                urns.append(record["urn"])
        if len(urns) != len(elements):
            raise FormaApiError(
                f"Forma returned {len(urns)} URNs for {len(elements)} requested "
                "geometries."
            )
        return urns

    def get_elements_batch(
        self, project_id: str, urns: list[str]
    ) -> dict[str, dict[str, Any]]:
        payload = self._request_json(
            "POST",
            "/forma/element-service/v1alpha/elements-batch",
            project_id=project_id,
            json_body={"urns": urns},
        )
        container: Any = (
            payload.get("results", payload)
            if isinstance(payload, dict)
            else payload
        )
        if isinstance(container, dict):
            raw_map = container.get("elements", container.get("element", container))
        else:
            raw_map = None
        if not isinstance(raw_map, dict):
            raise FormaApiError(
                "Elements-batch response did not contain an elements object map."
            )

        element_map: dict[str, dict[str, Any]] = {}
        for urn, value in raw_map.items():
            if isinstance(urn, str) and isinstance(value, dict):
                element_map[urn] = value
        return element_map

    def attach_elements_to_proposal(
        self,
        *,
        project_id: str,
        proposal_urn: str,
        element_urns: list[str],
    ) -> str | None:
        parts = parse_proposal_urn(proposal_urn)
        if parts.project_id != project_id:
            raise FormaApiError(
                f'Proposal URN belongs to project "{parts.project_id}", but the '
                f'entered project ID is "{project_id}".'
            )

        elements = self.get_elements_batch(project_id, [proposal_urn])
        proposal_element = elements.get(proposal_urn)
        if proposal_element is None and len(elements) == 1:
            proposal_element = next(iter(elements.values()))
        if proposal_element is None:
            raise FormaApiError(
                "The proposal root element was not returned by the Element Service API."
            )

        body = build_proposal_update_payload(proposal_element, element_urns)
        encoded_proposal_id = quote(parts.proposal_id, safe="")
        encoded_revision_id = quote(parts.revision_id, safe="")
        payload = self._request_json(
            "PUT",
            (
                "/forma/proposal/v1alpha/proposals/"
                f"{encoded_proposal_id}/revisions/{encoded_revision_id}"
            ),
            project_id=project_id,
            json_body=body,
        )
        return _extract_urn(payload)

    def get_proposal_terrain_urn(self, project_id: str, proposal_urn: str) -> str:
        elements = self.get_elements_batch(project_id, [proposal_urn])
        proposal = elements.get(proposal_urn)
        if proposal is None and len(elements) == 1:
            proposal = next(iter(elements.values()))
        children = proposal.get("children") if isinstance(proposal, dict) else None
        if not isinstance(children, list):
            raise FormaApiError("The proposal does not contain terrain children.")
        for child in children:
            urn = child.get("urn") if isinstance(child, dict) else None
            if isinstance(urn, str) and ":terrain:" in urn:
                return urn
        raise FormaApiError("The proposal terrain URN could not be identified.")

    def download_terrain_glb(self, project_id: str, terrain_urn: str) -> bytes:
        _, urn_project_id, element_id, revision = parse_element_urn(
            terrain_urn, "terrain"
        )
        if urn_project_id != project_id:
            raise FormaApiError("The terrain URN belongs to a different Forma site.")
        path = (
            "/forma/terrain/v1alpha/terrains/"
            f"{quote(element_id, safe='')}/revisions/{quote(revision, safe='')}/download"
        )
        url = f"{API_BASE_URL}{path}"
        headers = dict(self.headers)
        headers["Accept"] = "model/gltf-binary, application/octet-stream"
        headers.pop("Content-Type", None)
        try:
            response = self._session.request(
                "GET",
                url,
                params={"authcontext": project_id},
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FormaApiError(f"Could not download the Forma terrain: {exc}") from exc
        if not response.ok:
            payload = _decode_response(response)
            raise FormaApiError(
                f"Forma terrain download failed ({response.status_code} {response.reason}): "
                f"{_compact_payload(payload)}",
                status_code=response.status_code,
                response_body=payload,
            )
        return bytes(response.content)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        project_id: str,
        query: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        normalized_project_id = project_id.strip()
        if not normalized_project_id:
            raise ValueError("Forma project/site ID cannot be empty.")

        params = {"authcontext": normalized_project_id}
        if query:
            params.update(query)
        url = f"{API_BASE_URL}{path}"

        try:
            response = self._session.request(
                method,
                url,
                params=params,
                headers=self.headers,
                json=json_body,
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FormaApiError(
                f"Could not reach the Forma API for {method} {path}: {exc}"
            ) from exc

        response_payload = _decode_response(response)
        if not response.ok:
            details = _compact_payload(response_payload)
            raise FormaApiError(
                f"Forma API request failed for {method} {path} "
                f"({response.status_code} {response.reason}): {details}",
                status_code=response.status_code,
                response_body=response_payload,
            )
        return response_payload


def _decode_response(response: requests.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return response.text


def _compact_payload(payload: Any) -> str:
    if payload in (None, "", {}):
        return "empty response body"
    if isinstance(payload, str):
        return payload[:1_000]
    return json.dumps(payload, ensure_ascii=False)[:1_000]


def _extract_urn(payload: Any) -> str | None:
    if isinstance(payload, dict):
        direct = payload.get("urn")
        if isinstance(direct, str):
            return direct
        for key in ("proposal", "element", "result"):
            nested = payload.get(key)
            nested_urn = _extract_urn(nested)
            if nested_urn:
                return nested_urn
    if isinstance(payload, list):
        for item in payload:
            nested_urn = _extract_urn(item)
            if nested_urn:
                return nested_urn
    return None
