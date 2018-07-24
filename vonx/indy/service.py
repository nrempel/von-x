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
The Indy service implements handlers for all the ledger-related messages, sychronizes
agents and connections, and handles the core logic for working with credentials and proofs.
"""

import json
import hashlib
import logging
import pathlib
import random
import string
from typing import Mapping, Sequence

from didauth.indy import seed_to_did
from didauth.ext.aiohttp import SignedRequest, SignedRequestAuth
from von_agent.error import AbsentSchema, AbsentCredDef
from von_agent.nodepool import NodePool
from von_agent.util import cred_def_id, revealed_attrs, schema_id, schema_key

from ..common.service import (
    Exchange,
    ServiceBase,
    ServiceRequest,
    ServiceResponse,
    ServiceSyncError,
)
from ..common.util import log_json
from .config import (
    AgentType,
    AgentCfg,
    ConnectionCfg,
    ProofSpecCfg,
    SchemaCfg,
    WalletCfg,
)
from .connection import HttpSession
from .errors import IndyConfigError, IndyConnectionError, IndyError
from .messages import (
    IndyServiceAck,
    IndyServiceFail,
    LedgerStatusReq,
    LedgerStatus,
    RegisterWalletReq,
    WalletStatusReq,
    WalletStatus,
    RegisterAgentReq,
    AgentStatusReq,
    AgentStatus,
    RegisterCredentialTypeReq,
    RegisterConnectionReq,
    ConnectionStatusReq,
    ConnectionStatus,
    IssueCredentialReq,
    Credential,
    CredentialOffer,
    CredentialRequest,
    StoredCredential,
    GenerateCredentialRequestReq,
    StoreCredentialReq,
    ResolveSchemaReq,
    ResolvedSchema,
    ProofRequest,
    ConstructProofReq,
    ConstructedProof,
    RegisterProofSpecReq,
    ProofSpecStatus,
    GenerateProofRequestReq,
    RequestProofReq,
    VerifiedProof,
)

LOGGER = logging.getLogger(__name__)


def _make_id(pfx: str = '', length=12) -> str:
    return pfx + ''.join(random.choice(string.ascii_letters) for _ in range(length))


def _prepare_proof_request(spec: ProofSpecCfg) -> ProofRequest:
    """
    Prepare the JSON payload for a proof request

    Args:
        spec: the proof request specification
    """
    req_attrs = {}
    for schema in spec.schemas:
        s_id = schema["definition"].schema_id
        s_uniq = hashlib.sha1(s_id.encode('ascii')).hexdigest()
        for attr in schema["attributes"]:
            req_attrs["{}_{}_uuid".format(s_uniq, attr)] = {
                "name": attr,
                "restrictions": [{
                    "schema_id": s_id,
                }]
            }
    return ProofRequest({
        "name": spec.spec_id,
        "nonce": str(random.randint(10000000000, 100000000000)),  # FIXME - how best to generate?
        "version": spec.version,
        "requested_attributes": req_attrs,
        "requested_predicates": {},
    })


class IndyService(ServiceBase):
    """
    A class for managing interactions with the Hyperledger Indy ledger
    """

    def __init__(self, pid: str, exchange: Exchange, env: Mapping, spec: dict = None):
        super(IndyService, self).__init__(pid, exchange, env)
        self._config = {}
        self._genesis_path = None
        self._agents = {}
        self._connections = {}
        self._ledger_url = None
        self._name = pid
        self._opened = False
        self._pool = None
        self._proof_specs = {}
        self._wallets = {}
        self._verifier = None
        self._update_config(spec)

    def _update_config(self, spec) -> None:
        """
        Load configuration settings
        """
        if spec:
            self._config.update(spec)
        if "name" in spec:
            self._name = spec["name"]
        if "ledger_url" in spec:
            self._ledger_url = spec["ledger_url"]

    async def _service_sync(self) -> bool:
        """
        Perform the initial setup of the ledger connection, including downloading the
        genesis transaction file
        """
        await self._setup_pool()
        synced = True
        for wallet in self._wallets.values():
            if not wallet.created:
                await wallet.create(self._pool)
        for agent in self._agents.values():
            if not await self._sync_agent(agent):
                synced = False
        for connection in self._connections.values():
            if not await self._sync_connection(connection):
                synced = False
        for spec in self._proof_specs.values():
            if not await self._sync_proof_spec(spec):
                synced = False
        return synced

    async def _service_stop(self) -> None:
        """
        Shut down active connections
        """
        for connection in self._connections.values():
            await connection.close()
        for agent in self._agents.values():
            await agent.close()
        for wallet in self._wallets.values():
            await wallet.close()

    def _add_agent(self, agent_type: str, wallet_id: str, **params) -> str:
        """
        Add an agent configuration

        Args:
            agent_type: the agent type, issuer or holder
            wallet_id: the identifier for a previously-registered wallet
            params: parameters to be passed to the :class:`AgentCfg` constructor
        """
        if wallet_id not in self._wallets:
            raise IndyConfigError("Wallet ID not registered: {}".format(wallet_id))
        cfg = AgentCfg(agent_type, wallet_id, **params)
        if not cfg.agent_id:
            cfg.agent_id = _make_id("agent-")
        if cfg.agent_id in self._agents:
            raise IndyConfigError("Duplicate agent ID: {}".format(cfg.agent_id))
        agents = self._agents.copy()
        agents[cfg.agent_id] = cfg
        self._agents = agents
        return cfg.agent_id

    def _get_agent_status(self, agent_id: str) -> ServiceResponse:
        """
        Return the status of a registered agent

        Args:
            agent_id: the unique identifier of the agent
        """
        if agent_id in self._agents:
            msg = AgentStatus(agent_id, self._agents[agent_id].status)
        else:
            msg = IndyServiceFail("Unregistered agent: {}".format(agent_id))
        return msg

    def _add_credential_type(self, issuer_id: str, schema_name: str,
                             schema_version: str, origin_did: str,
                             attr_names: Sequence, config: Mapping = None) -> None:
        """
        Add a credential type to a given issuer

        Args:
            issuer_id: the identifier of the issuer service
            schema_name: the name of the schema used by the credential type
            schema_version: the version of the schema used by the credential type
            origin_did: the DID of the service issuing the schema (optional)
            attr_names: a list of schema attribute names
            config: additional configuration for the credential type
        """
        agent = self._agents[issuer_id]
        if not agent:
            raise IndyConfigError("Agent ID not registered: {}".format(issuer_id))
        schema = SchemaCfg(schema_name, schema_version, attr_names, origin_did)
        agent.add_credential_type(schema, **(config or {}))

    def _add_connection(self, connection_type: str, agent_id: str, **params) -> str:
        """
        Add a connection configuration

        Args:
            connection_type: the type of the connection, normally TheOrgBook
            agent_id: the identifier of the registered agent
            params: parameters to be passed to the :class:`ConnectionCfg` constructor
        """
        if agent_id not in self._agents:
            raise IndyConfigError("Agent ID not registered: {}".format(agent_id))
        cfg = ConnectionCfg(connection_type, agent_id, self._agents[agent_id].agent_type.value, **params)
        if not cfg.connection_id:
            cfg.connection_id = _make_id("connection-")
        if cfg.connection_id in self._connections:
            raise IndyConfigError("Duplicate connection ID: {}".format(cfg.connection_id))
        conns = self._connections.copy()
        conns[cfg.connection_id] = cfg
        self._connections = conns
        return cfg.connection_id

    def _get_connection_status(self, connection_id: str) -> ServiceResponse:
        """
        Return the status of a registered connection

        Args:
            connection_id: the unique identifier of the connection
        """
        if connection_id in self._connections:
            msg = ConnectionStatus(connection_id, self._connections[connection_id].status)
        else:
            msg = IndyServiceFail("Unregistered connection: {}".format(connection_id))
        return msg

    def _add_wallet(self, **params) -> str:
        """
        Add a wallet configuration

        Args:
            params: parameters to be passed to the :class:`WalletCfg` constructor
        """
        cfg = WalletCfg(**params)
        if not cfg.wallet_id:
            cfg.wallet_id = _make_id("wallet-")
        if cfg.wallet_id in self._wallets:
            raise IndyConfigError("Duplicate wallet ID: {}".format(cfg.wallet_id))
        wallets = self._wallets.copy()
        wallets[cfg.wallet_id] = cfg
        self._wallets = wallets
        return cfg.wallet_id

    def _get_wallet_status(self, wallet_id: str) -> ServiceResponse:
        """
        Return the status of a registered wallet

        Args:
            wallet_id: the unique identifier of the wallet
        """
        if wallet_id in self._wallets:
            msg = WalletStatus(wallet_id, self._wallets[wallet_id].status)
        else:
            msg = IndyServiceFail("Unregistered wallet: {}".format(wallet_id))
        return msg

    async def _sync_agent(self, agent: AgentCfg) -> bool:
        """
        Perform agent synchronization, registering the DID and publishing schemas
        and credential definitions as required

        Args:
            agent: the Indy agent configuration
        """
        if not agent.synced:
            if not agent.created:
                wallet = self._wallets[agent.wallet_id]
                if not wallet.created:
                    return False
                await agent.create(wallet)

            await agent.open()

            if not agent.registered:
                # check DID is registered
                auto_register = self._config.get("auto_register", True)
                await self._check_registration(agent, auto_register, agent.role)

                # check endpoint is registered (if any)
                # await self._check_endpoint(agent.instance, agent.endpoint)
                agent.registered = True

            # publish schemas
            for cred_type in agent.cred_types:
                await self._publish_schema(agent, cred_type)

            agent.synced = True
            LOGGER.info("Indy agent synced: %s", agent.agent_id)
        return agent.synced

    async def _sync_connection(self, connection: ConnectionCfg) -> bool:
        """
        Perform synchronization on a connection object
        """
        agent = self._agents[connection.agent_id]

        if not connection.synced:
            if not connection.created:
                if not agent.synced:
                    return False
                agent_cfg = agent.get_connection_params(connection)
                await connection.create(agent_cfg)

            try:
                if not connection.opened:
                    await connection.open(self)

                await connection.sync()
            except IndyConnectionError as e:
                raise ServiceSyncError("Error syncing connection {}: {}".format(
                    connection.connection_id, str(e))) from None
        return connection.synced

    async def _setup_pool(self) -> None:
        """
        Initialize the Indy NodePool, fetching the genesis transaction if necessary
        """
        if not self._opened:
            await self._check_genesis_path()
            self._pool = NodePool(self._name, self._genesis_path)
            await self._pool.open()
            self._opened = True

    async def _check_genesis_path(self) -> None:
        """
        Make sure that the genesis path is defined, and download the transaction file if needed.
        """
        if not self._genesis_path:
            path = self._config.get("genesis_path")
            if not path:
                raise IndyConfigError("Missing genesis_path")
            genesis_path = pathlib.Path(path)
            if not genesis_path.exists():
                ledger_url = self._ledger_url
                if not ledger_url:
                    raise IndyConfigError(
                        "Cannot retrieve genesis transaction without ledger_url"
                    )
                parent_path = pathlib.Path(genesis_path.parent)
                if not parent_path.exists():
                    parent_path.mkdir(parents=True)
                await self._fetch_genesis_txn(ledger_url, genesis_path)
            elif genesis_path.is_dir():
                raise IndyConfigError("genesis_path must not point to a directory")
            self._genesis_path = path

    async def _fetch_genesis_txn(self, ledger_url: str, target_path: str) -> bool:
        """
        Download the genesis transaction file from the ledger server

        Args:
            ledger_url: the root address of the von-network ledger
            target_path: the filesystem path of the genesis transaction file once downloaded
        """
        LOGGER.info(
            "Fetching genesis transaction file from %s/genesis", ledger_url
        )

        try:
            async with HttpSession('fetching genesis transaction', timeout=15) as handler:
                response = await handler.client.get("{}/genesis".format(ledger_url))
                await handler.check_status(response, (200,))
                data = await response.text()
        except IndyConnectionError as e:
            raise ServiceSyncError(str(e)) from None

        # check data is valid json
        LOGGER.debug("Genesis transaction response: %s", data)
        lines = data.splitlines()
        if not lines or not json.loads(lines[0]):
            raise ServiceSyncError("Genesis transaction file is not valid JSON")

        # write result to provided path
        with target_path.open("x") as output_file:
            output_file.write(data)
        return True

    async def _check_registration(self, agent: AgentCfg, auto_register: bool = True,
                                  role: str = "") -> None:
        """
        Look up our nym on the ledger and register it if not present

        Args:
            agent: the initialized and opened agent to be checked
            auto_register: whether to automatically register the DID on the ledger
        """
        did = agent.did
        LOGGER.debug("Checking DID registration %s", did)
        nym_json = await agent.instance.get_nym(did)
        LOGGER.debug("get_nym result for %s: %s", did, nym_json)

        nym_info = json.loads(nym_json)
        if not nym_info:
            if not auto_register:
                raise ServiceSyncError(
                    "DID is not registered on the ledger and auto-registration disabled"
                )

            ledger_url = self._ledger_url
            if not ledger_url:
                raise IndyConfigError("Cannot register DID without ledger_url")
            LOGGER.info("Registering DID %s", did)

            try:
                async with HttpSession('DID registration', timeout=30) as handler:
                    response = await handler.client.post(
                        "{}/register".format(ledger_url),
                        json={"did": did, "verkey": agent.verkey, "role": role},
                    )
                    await handler.check_status(response, (200,))
                    nym_info = await response.json()
            except IndyConnectionError as e:
                raise ServiceSyncError(str(e)) from None
            LOGGER.debug("Registration response: %s", nym_info)
            if not nym_info or not nym_info["did"]:
                raise ServiceSyncError(
                    "DID registration failed: {}".format(nym_info)
                )

    async def _check_endpoint(self, agent: AgentCfg, endpoint: str) -> None:
        """
        Look up our endpoint on the ledger and register it if not present

        Args:
            agent: the initialized and opened agent to be checked
            endpoint: the endpoint to be added to the ledger, if not defined
        """
        if not endpoint:
            return None
        did = agent.did
        LOGGER.debug("Checking endpoint registration %s", endpoint)
        endp_json = await agent.instance.get_endpoint(did)
        LOGGER.debug("get_endpoint result for %s: %s", did, endp_json)

        endp_info = json.loads(endp_json)
        if not endp_info:
            endp_info = await agent.instance.send_endpoint()
            LOGGER.debug("Endpoint stored: %s", endp_info)

    async def _publish_schema(self, issuer: AgentCfg, cred_type: dict) -> None:
        """
        Check the ledger for a specific schema and version, and publish it if not found.
        Also publish the related credential definition if not found

        Args:
            issuer: the initialized and opened issuer instance publishing the schema
            cred_type: a dict which will be updated with the published schema and credential def
        """

        if not cred_type or "definition" not in cred_type:
            raise IndyConfigError("Missing schema definition")
        definition = cred_type["definition"]

        if not cred_type.get("ledger_schema"):
            LOGGER.info(
                "Checking for schema: %s (%s)",
                definition.name,
                definition.version,
            )
            # Check if schema exists on ledger

            try:
                s_key = schema_key(
                    schema_id(issuer.did, definition.name, definition.version)
                )
                schema_json = await issuer.instance.get_schema(s_key)
                ledger_schema = json.loads(schema_json)
                log_json("Schema found on ledger:", ledger_schema, LOGGER)
            except AbsentSchema:
                # If not found, send the schema to the ledger
                LOGGER.info(
                    "Publishing schema: %s (%s)",
                    definition.name,
                    definition.version,
                )
                schema_json = await issuer.instance.send_schema(
                    json.dumps(
                        {
                            "name": definition.name,
                            "version": definition.version,
                            "attr_names": definition.attr_names,
                        }
                    )
                )
                ledger_schema = json.loads(schema_json)
                if not ledger_schema or not ledger_schema.get("seqNo"):
                    raise ServiceSyncError("Schema was not published to ledger")
                log_json("Published schema:", ledger_schema, LOGGER)
            cred_type["ledger_schema"] = ledger_schema

        if not cred_type.get("cred_def"):
            # Check if credential definition has been published
            LOGGER.info(
                "Checking for credential def: %s (%s)",
                definition.name,
                definition.version,
            )

            try:
                cred_def_json = await issuer.instance.get_cred_def(
                    cred_def_id(issuer.did, cred_type["ledger_schema"]["seqNo"])
                )
                cred_def = json.loads(cred_def_json)
                log_json("Credential def found on ledger:", cred_def, LOGGER)
            except AbsentCredDef:
                # If credential definition is not found then publish it
                LOGGER.info(
                    "Publishing credential def: %s (%s)",
                    definition.name,
                    definition.version,
                )
                cred_def_json = await issuer.instance.send_cred_def(
                    schema_json, revocation=False
                )
                cred_def = json.loads(cred_def_json)
                log_json("Published credential def:", cred_def, LOGGER)
            cred_type["cred_def"] = cred_def

    async def _issue_credential(self, connection_id: str, schema_name: str,
                                schema_version: str, origin_did: str,
                                cred_data: Mapping) -> ServiceResponse:
        """
        Issue a credential to the connection target

        Args:
            connection_id: the identifier of the registered connection
            schema_name: the name of the credential schema
            schema_version: the version of the credential schema
            origin_did: the origin DID of the ledger schema (may be None)
            cred_data: the raw credential attributes
        """
        conn = self._connections.get(connection_id)
        if not conn:
            raise IndyConfigError("Unknown connection id: {}".format(connection_id))
        if not conn.synced:
            raise IndyConfigError("Connection is not yet synchronized: {}".format(connection_id))
        issuer = self._agents[conn.agent_id]
        if issuer.agent_type != AgentType.issuer:
            raise IndyConfigError(
                "Cannot issue credential from non-issuer agent: {}".format(issuer.agent_id))
        if not issuer.synced:
            raise IndyConfigError("Issuer is not yet synchronized: {}".format(issuer.agent_id))
        cred_type = issuer.find_credential_type(schema_name, schema_version, origin_did)
        if not cred_type:
            raise IndyConfigError("Could not locate credential type: {}/{} {}".format(
                schema_name, schema_version, origin_did))

        cred_offer = await self._create_cred_offer(issuer, cred_type)
        log_json("Created cred offer:", cred_offer, LOGGER)
        cred_request = await conn.instance.generate_credential_request(cred_offer)
        log_json("Got cred request:", cred_request, LOGGER)
        cred = await self._create_cred(issuer, cred_request, cred_data)
        log_json("Created cred:", cred, LOGGER)
        stored = await conn.instance.store_credential(cred)
        log_json("Stored credential:", stored, LOGGER)
        return stored

    async def _create_cred_offer(self, issuer: AgentCfg,
                                 cred_type) -> CredentialOffer:
        """
        Create a credential offer for a specific connection from a given issuer

        Args:
            issuer: the issuer configuration object
            cred_type: the credential type definition
        """
        schema = cred_type["definition"]

        LOGGER.info(
            "Creating Indy credential offer for issuer %s, schema %s",
            issuer.agent_id,
            schema.name,
        )
        cred_offer_json = await issuer.instance.create_cred_offer(
            cred_type["ledger_schema"]["seqNo"]
        )
        return CredentialOffer(
            schema.name,
            schema.version,
            json.loads(cred_offer_json),
            cred_type["cred_def"],
        )

    async def _create_cred(self, issuer: AgentCfg, request: CredentialRequest,
                           cred_data: Mapping) -> Credential:
        """
        Create a credential from a credential request for a specific issuer

        Args:
            issuer: the issuer configuration object
            request: a credential request returned from the holder service
            cred_data: the raw credential attributes
        """

        cred_offer = request.cred_offer
        (cred_json, cred_revoc_id) = await issuer.instance.create_cred(
            json.dumps(cred_offer.offer),
            request.data,
            cred_data,
        )
        return Credential(
            cred_offer.schema_name,
            issuer.did,
            json.loads(cred_json),
            cred_offer.cred_def,
            request.metadata,
            cred_revoc_id,
        )

    async def _generate_credential_request(self, holder_id: str,
                                           cred_offer: CredentialOffer) -> CredentialRequest:
        """
        Generate a credential request for a given holder agent from a credential offer
        """
        holder = self._agents.get(holder_id)
        if not holder:
            raise IndyConfigError("Unknown holder id: {}".format(holder_id))
        if not holder.synced:
            raise IndyConfigError("Holder is not yet synchronized: {}".format(holder_id))
        (cred_req, req_metadata_json) = await holder.instance.create_cred_req(
            json.dumps(cred_offer.offer),
            json.dumps(cred_offer.cred_def),
        )
        return CredentialRequest(
            cred_offer,
            cred_req,
            json.loads(req_metadata_json),
        )

    async def _store_credential(self, holder_id: str,
                                credential: Credential) -> StoredCredential:
        """
        Store a credential in a given holder agent's wallet
        """
        holder = self._agents.get(holder_id)
        if not holder:
            raise IndyConfigError("Unknown holder id: {}".format(holder_id))
        if not holder.synced:
            raise IndyConfigError("Holder is not yet synchronized: {}".format(holder_id))
        cred_id = await holder.instance.store_cred(
            json.dumps(credential.cred_data),
            json.dumps(credential.cred_req_metadata),
        )
        return StoredCredential(
            cred_id,
            credential,
            {"attributes": credential.cred_data},
        )

    async def _resolve_schema(self, schema_name: str, schema_version: str,
                              origin_did: str) -> ResolvedSchema:
        """
        Resolve a schema defined by one of our issuers
        """
        for agent_id, agent in self._agents.items():
            if agent.synced:
                found = agent.find_credential_type(schema_name, schema_version, origin_did)
                if found:
                    defn = found["definition"]
                    did = defn.origin_did or agent.did
                    return ResolvedSchema(
                        agent_id,
                        schema_id(did, defn.name, defn.version),
                        defn.name,
                        defn.version,
                        did,
                        defn.attr_names,
                    )
        raise IndyConfigError("Issuer schema not found: {}/{}".format(schema_name, schema_version))

    async def _construct_proof(self, holder_id: str, proof_req: ProofRequest,
                               cred_ids: set = None) -> ConstructedProof:
        """
        Construct a proof from credentials in the holder's wallet, given a proof request
        """
        holder = self._agents.get(holder_id)
        if not holder:
            raise IndyConfigError("Unknown holder id: {}".format(holder_id))
        if not holder.synced:
            raise IndyConfigError("Holder is not yet synchronized: {}".format(holder_id))
        log_json("Fetching credentials for request", proof_req.request, LOGGER)

        # TODO - use separate request to find credentials and allow manual filtering?
        if cred_ids:
            LOGGER.info("cred ids %s", cred_ids)
            found_creds_json = await holder.instance.get_creds_by_id(
                json.dumps(proof_req.request), cred_ids,
            )
        else:
            _referents, found_creds_json = await holder.instance.get_creds(
                json.dumps(proof_req.request), # + filters ..
            )
        found_creds = json.loads(found_creds_json)
        log_json("Found credentials", found_creds, LOGGER)

        if not found_creds["attrs"]:
            raise IndyError("No credentials found for proof")

        missing = set()
        too_many = set()
        for (claim_name, claim) in found_creds["attrs"].items():
            if not claim:
                missing.add(claim_name)
            if len(claim) > 1:
                too_many.add(claim_name)
        if missing:
            raise IndyError("No credentials found for proof")
        if too_many:
            raise IndyError("Too many credentials found for proof")

        # Construct the required payload to create proof
        request_params = {
            "self_attested_attributes": {},
            "requested_attributes": {
                claim_name: {"revealed": True, "cred_id": claim[0]["cred_info"]["referent"]}
                for (claim_name, claim) in found_creds["attrs"].items()
            },
            "requested_predicates": {},
        }

        # FIXME catch exception?
        log_json("Creating proof", request_params, LOGGER)
        proof_json = await holder.instance.create_proof(
            proof_req.request,
            found_creds,
            request_params,
        )
        proof = json.loads(proof_json)
        return ConstructedProof(proof)

    def _add_proof_spec(self, **params) -> str:
        """
        Add a proof request specification

        Args:
            params: parameters to be passed to the :class:`ProofSpecCfg` constructor
        """
        cfg = ProofSpecCfg(**params)
        if not cfg.spec_id:
            cfg.spec_id = _make_id("proof-")
        if cfg.spec_id in self._proof_specs:
            raise IndyConfigError("Duplicate proof spec ID: {}".format(cfg.spec_id))
        self._proof_specs[cfg.spec_id] = cfg
        return cfg.spec_id

    async def _sync_proof_spec(self, spec: ProofSpecCfg) -> bool:
        missing = spec.get_incomplete_schemas()
        check = False
        for s_key in missing:
            try:
                found = await self._resolve_schema(*s_key)
                cfg = SchemaCfg(
                    found.schema_name, found.schema_version,
                    found.attr_names, found.origin_did)
                spec.populate_schema(cfg)
                check = True
            except IndyConfigError:
                pass
        if check:
            missing = spec.get_incomplete_schemas()
        spec.synced = not missing
        return spec.synced

    def _get_proof_spec_status(self, spec_id: str) -> ServiceResponse:
        """
        Return the status of a registered proof spec

        Args:
            spec_id: the unique identifier of the proof specification
        """
        if spec_id in self._proof_specs:
            msg = ProofSpecStatus(spec_id, self._proof_specs[spec_id].status)
        else:
            msg = IndyServiceFail("Unregistered proof spec: {}".format(spec_id))
        return msg

    async def _generate_proof_request(self, spec_id: str) -> ProofRequest:
        """
        Create a proof request from a previously registered proof specification
        """
        spec = self._proof_specs.get(spec_id)
        if not spec:
            raise IndyConfigError("Proof specification not defined: {}".format(spec_id))
        if not spec.synced:
            raise IndyConfigError("Proof specification not synced: {}".format(spec_id))
        return _prepare_proof_request(spec)

    async def _request_proof(self, connection_id: str, proof_req: ProofRequest,
                             cred_ids: set = None, params: dict = None) -> VerifiedProof:
        """
        Request a verified proof from a connection
        """
        conn = self._connections.get(connection_id)
        if not conn:
            raise IndyConfigError("Unknown connection id: {}".format(connection_id))
        if not conn.synced:
            raise IndyConfigError("Connection is not yet synchronized: {}".format(connection_id))
        verifier = self._agents[conn.agent_id]
        if verifier.agent_type != AgentType.verifier:
            raise IndyConfigError(
                "Cannot verify proof from non-verifier agent: {}".format(verifier.agent_id))
        if not verifier.synced:
            raise IndyConfigError("Verifier is not yet synchronized: {}".format(verifier.agent_id))
        proof = await conn.instance.construct_proof(proof_req, cred_ids, params)
        return await self._verify_proof(verifier.agent_id, proof_req, proof)

    async def _verify_proof(self, verifier_id: str, proof_req: ProofRequest,
                            proof: ConstructedProof) -> VerifiedProof:
        verifier = self._agents.get(verifier_id)
        if not verifier:
            raise IndyConfigError("Unknown verifier id: {}".format(verifier_id))
        if not verifier.synced:
            raise IndyConfigError("Verifier is not yet synchronized: {}".format(verifier.agent_id))
        result = await verifier.instance.verify_proof(proof_req.request, proof.proof)
        parsed_proof = revealed_attrs(proof.proof)
        return VerifiedProof(result, parsed_proof, proof)

    async def _handle_ledger_status(self):
        """
        Download the ledger status from von-network and return it to the client
        """
        url = self._ledger_url
        async with self.http as client:
            response = await client.get("{}/status".format(url))
        return await response.text()

    def _agent_http_client(self, agent_id: str = None, **kwargs):
        """
        Create a new :class:`ClientSession` which includes DID signing information in each request

        Args:
            agent_id: an optional identifier for a specific issuer service (to enable DID signing)
        Returns:
            the initialized :class:`ClientSession` object
        """
        if "request_class" not in kwargs:
            kwargs["request_class"] = SignedRequest
        if agent_id and "auth" not in kwargs:
            kwargs["auth"] = self._did_auth(agent_id)
        return super(IndyService, self).http_client(**kwargs)

    def _did_auth(self, agent_id: str, header_list=None):
        """
        Create a :class:`SignedRequestAuth` representing our authentication credentials,
        used to sign outgoing requests

        Args:
            agent_id: the unique identifier of the issuer
            header_list: optionally override the list of headers to sign
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise IndyConfigError("Unknown agent ID: {}".format(agent_id))
        wallet = self._wallets[agent.wallet_id]
        if agent.did and wallet.seed:
            key_id = "did:sov:{}".format(agent.did)
            secret = wallet.seed
            if isinstance(secret, str):
                secret = secret.encode("ascii")
            return SignedRequestAuth(key_id, "ed25519", secret, header_list)
        return None

    async def _service_request(self, request: ServiceRequest) -> ServiceResponse:
        """
        Process a message from the exchange and send the reply, if any

        Args:
            request: the message to be processed
        """
        if isinstance(request, LedgerStatusReq):
            text = await self._handle_ledger_status()
            reply = LedgerStatus(text)

        elif isinstance(request, RegisterAgentReq):
            try:
                agent_id = self._add_agent(request.agent_type, request.wallet_id, **request.config)
                reply = self._get_agent_status(agent_id)
                self._sync_required()
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterConnectionReq):
            try:
                connection_id = self._add_connection(
                    request.connection_type, request.agent_id, **request.config)
                reply = self._get_connection_status(connection_id)
                self._sync_required()
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterCredentialTypeReq):
            try:
                self._add_credential_type(
                    request.issuer_id,
                    request.schema_name,
                    request.schema_version,
                    request.origin_did,
                    request.attr_names,
                    request.config)
                reply = IndyServiceAck()
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterWalletReq):
            try:
                wallet_id = self._add_wallet(**request.config)
                reply = self._get_wallet_status(wallet_id)
                self._sync_required()
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, AgentStatusReq):
            reply = self._get_agent_status(request.agent_id)

        elif isinstance(request, ConnectionStatusReq):
            reply = self._get_connection_status(request.connection_id)

        elif isinstance(request, WalletStatusReq):
            reply = self._get_wallet_status(request.wallet_id)

        elif isinstance(request, IssueCredentialReq):
            try:
                reply = await self._issue_credential(
                    request.connection_id,
                    request.schema_name,
                    request.schema_version,
                    request.origin_did,
                    request.cred_data)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, GenerateCredentialRequestReq):
            try:
                reply = await self._generate_credential_request(
                    request.holder_id, request.cred_offer)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, StoreCredentialReq):
            try:
                reply = await self._store_credential(
                    request.holder_id, request.credential)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, ResolveSchemaReq):
            try:
                reply = await self._resolve_schema(
                    request.schema_name, request.schema_version, request.origin_did)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, ConstructProofReq):
            try:
                reply = await self._construct_proof(
                    request.holder_id, request.proof_req, request.cred_ids)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RegisterProofSpecReq):
            try:
                spec_id = self._add_proof_spec(**request.config)
                reply = self._get_proof_spec_status(spec_id)
                self._sync_required()
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, GenerateProofRequestReq):
            try:
                reply = await self._generate_proof_request(request.spec_id)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        elif isinstance(request, RequestProofReq):
            try:
                reply = await self._request_proof(
                    request.connection_id, request.proof_req,
                    request.cred_ids, request.params)
            except IndyError as e:
                reply = IndyServiceFail(str(e))

        #elif isinstance(request, VerifyProofReq):
        #    reply = await self._handle_verify_proof(request)

        else:
            reply = None
        return reply
