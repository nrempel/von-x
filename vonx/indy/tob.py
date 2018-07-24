#
# Copyright 2017-2018 Government of Canada
# Public Services and Procurement Canada - buyandsell.gc.ca
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Connection handling specific to using TheOrgBook as a holder/prover
"""

import logging

from .connection import ConnectionBase, HttpSession
from .errors import IndyConfigError, IndyConnectionError
from .messages import (
    CredentialOffer,
    Credential,
    CredentialRequest,
    StoredCredential,
    ProofRequest,
    ConstructedProof,
)

LOGGER = logging.getLogger(__name__)


def assemble_issuer_spec(config: dict) -> dict:
    """
    Create the issuer JSON definition which will be submitted to TheOrgBook
    """
    issuer_spec = {}
    issuer_email = config.get("email")
    if not issuer_email:
        raise IndyConfigError("Missing issuer email address")
    issuer_did = config.get("did")
    if not issuer_did:
        raise IndyConfigError("Missing issuer DID")

    issuer_spec["issuer"] = {
        "did": issuer_did,
        "name": config.get("name") or "",
        "abbreviation": config.get("abbreviation") or "",
        "email": issuer_email,
        "url": config.get("url") or "",
    }

    if not issuer_spec["issuer"]["name"]:
        raise IndyConfigError("Missing issuer name")

    cred_type_specs = config.get("credential_types")
    if not cred_type_specs:
        raise IndyConfigError("Missing credential_types")
    ctypes = []
    for type_spec in cred_type_specs:
        schema = type_spec["schema"]
        if not type_spec.get("source_claim"):
            raise IndyConfigError("Missing 'source_claim' for credential type")
        ctype = {
            "name": type_spec.get("description") or schema.name,
            "endpoint": type_spec.get("issuer_url") or issuer_spec["issuer"]["url"],
            "schema": schema.name,
            "version": schema.version,
            "topic": type_spec["topic"]
        }
        mapping = type_spec.get("mapping")
        if mapping:
            ctype["mapping"] = mapping

        cardinality_fields = type_spec.get("cardinality_fields")
        if cardinality_fields:
            ctype["cardinality_fields"] = cardinality_fields

        ctypes.append(ctype)
    issuer_spec["credential_types"] = ctypes
    return issuer_spec


class TobConnection(ConnectionBase):
    """
    A class for managing communication with TheOrgBook API and performing the initial
    synchronization as an issuer
    """

    def __init__(self, agent_id: str, agent_type: str, agent_params: dict, conn_params: dict):
        super(TobConnection, self).__init__(agent_id, agent_type, agent_params, conn_params)
        self._api_url = self.conn_params.get('api_url')
        if not self._api_url:
            raise IndyConfigError("Missing 'api_url' for TheOrgBook connection")
        self._http_client = None

    async def open(self, service: 'IndyService') -> None:
        # TODO check DID is registered etc ..
        self._http_client = service._agent_http_client(self.agent_id)

    async def close(self) -> None:
        """
        Shut down the connection
        """
        if self._http_client:
            await self._http_client.close()
            self._http_client = None

    async def sync(self) -> None:
        """
        Submit the issuer JSON definition to TheOrgBook to register our service
        """
        if self.agent_type == 'issuer':
            spec = assemble_issuer_spec(self.agent_params)
            response = await self.post_json(
                "indy/register-issuer", spec
            )
            result = response.get("result")
            if not response.get("success"):
                raise IndyConnectionError(
                    "Issuer service was not registered: {}".format(result),
                    400,
                    response,
                )

    async def generate_credential_request(
            self, indy_offer: CredentialOffer) -> CredentialRequest:
        """
        Ask the API to generate a credential request from our credential offer

        Args:
            indy_offer: the result of preparing a credential offer
        """
        response = await self.post_json(
            "indy/generate-credential-request", {
                "credential_offer": indy_offer.offer,
                "credential_definition": indy_offer.cred_def,
            }
        )
        LOGGER.debug("Credential request response: %s", response)
        result = response.get("result")
        if not response.get("success"):
            raise IndyConnectionError(
                "Could not create credential request: {}".format(result),
                400,
                response,
            )
        return CredentialRequest(
            indy_offer,
            result["credential_request"],
            result["credential_request_metadata"],
        )

    async def store_credential(
            self, indy_cred: Credential) -> StoredCredential:
        """
        Ask the API to store a credential

        Args:
            indy_cred: the result of preparing a credential from a credential request
        """
        response = await self.post_json(
            "indy/store-credential", {
                "credential_type": indy_cred.schema_name,
                "credential_data": indy_cred.cred_data,
                "issuer_did": indy_cred.issuer_did,
                "credential_definition": indy_cred.cred_def,
                "credential_request_metadata": indy_cred.cred_req_metadata,
            }
        )
        LOGGER.debug("Store credential response: %s", response)
        result = response.get("result")
        if not response.get("success"):
            raise IndyConnectionError(
                "Credential was not stored: {}".format(result),
                400,
                response,
            )
        return StoredCredential(
            None,
            indy_cred,
            result,
        )

    async def construct_proof(self, request: ProofRequest,
                              cred_ids: set = None, params: dict = None) -> ConstructedProof:
        """
        Ask the API to construct a proof from a proof request

        Args:
            proof_request: the prepared Indy proof request
        """
        response = await self.post_json(
            "indy/construct-proof", {
                "source_id": params and params.get("source_id") or None,
                "proof_request": request.request,
                "cred_ids": list(cred_ids) if cred_ids else None,
            }
        )
        result = response.get("result")
        if not response.get("success"):
            raise IndyConnectionError(
                "Error constructing proof: {}".format(result),
                400,
                response,
            )
        return ConstructedProof(
            result,
        )


    def get_api_url(self, path: str = None) -> str:
        """
        Construct the URL for an API request

        Args:
            path: an optional path to be appended to the URL
        """
        url = self._api_url
        if not url.endswith("/"):
            url += "/"
        if path:
            url = url + path
        return url

    async def fetch_list(self, path: str) -> dict:
        """
        A standard request to a `list`-style API method

        Args:
            path: The relative path to the API method
        """
        url = self.get_api_url(path)
        LOGGER.debug("fetch_list: %s", url)
        async with HttpSession("fetch_list", self._http_client) as handler:
            response = await handler.client.get(url)
            await handler.check_status(response)
            return await response.json()

    async def post_json(self, path: str, data):
        """
        A standard POST request to an API method

        Args:
            path: The relative path to the API method
            data: The body of the request, to be converted to JSON

        Returns:
            the decoded JSON response
        """
        url = self.get_api_url(path)
        LOGGER.debug("post_json: %s", url)
        async with HttpSession("post_json", self._http_client) as handler:
            response = await handler.client.post(url, json=data)
            await handler.check_status(response)
            return await response.json()
