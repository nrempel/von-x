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
Classes for managing the active :class:`IndyService` configuration - agents, connections,
schemas, proof requests, and wallets.
"""

from distutils.version import LooseVersion
from enum import Enum
import logging
from typing import Mapping, Sequence
import uuid

from von_agent.agents import (
    _BaseAgent,
    Issuer,
    HolderProver,
    Verifier,
)
from von_agent.nodepool import NodePool
from von_agent.wallet import Wallet
from von_agent.util import schema_id

from .connection import ConnectionBase, ConnectionType, HolderConnection
from .errors import IndyConfigError
from .tob import TobConnection

LOGGER = logging.getLogger(__name__)


class AgentType(Enum):
    """
    Enumeration of supported agent types
    """
    issuer = "issuer"
    holder = "holder"
    verifier = "verifier"


class AgentCfg:
    """
    Manage configuration settings for an Agent, including schemas bound for the ledger
    """
    def __init__(self, agent_type: str, wallet_id: str, **params):
        self.agent_id = params.get("id")
        try:
            self.agent_type = AgentType(agent_type)
        except KeyError:
            raise IndyConfigError("Unsupported agent type: {}".format(agent_type))
        self.cred_types = []
        self._instance = None
        self.opened = False
        self.registered = False
        self.synced = False
        self.wallet_id = wallet_id
        self.abbreviation = params.get("abbreviation")
        self.email = params.get("email")
        self.endpoint = params.get("endpoint")
        self.name = params.get("name")
        self.url = params.get("url")

    @property
    def created(self) -> bool:
        """
        Accessor for the current created status of the agent instance
        """
        return self.instance is not None

    @property
    def did(self) -> str:
        """
        Accessor for DID of the agent's wallet
        """
        return self._instance and self._instance.did

    @property
    def extended_config(self) -> dict:
        """
        Accessor for the extended Agent configuration
        """
        ret = {}
        if self.endpoint:
            ret["endpoint"] = self.endpoint
        return ret

    @property
    def instance(self) -> _BaseAgent:
        """
        Accessor for the current agent instance
        """
        return self._instance

    @property
    def role(self) -> str:
        """
        Accessor for the role of the agent to be registered on the ledger
        """
        return "TRUST_ANCHOR" if self.agent_type == AgentType.issuer else ""

    @property
    def status(self) -> dict:
        """
        Get the current status of the agent
        """
        return {
            "did": self.did,
            "created": self.created,
            "opened": self.opened,
            "registered": self.registered,
            "synced": self.synced,
        }

    @property
    def verkey(self) -> str:
        """
        Accessor for the verkey of the agent's wallet
        """
        return self._instance and self._instance.verkey

    async def create(self, wallet: 'WalletCfg') -> None:
        """
        Create the agent instance

        Args:
            wallet: the registered wallet configuration, previously created and opened
        """
        if not self._instance:
            if self.agent_type == AgentType.issuer:
                cls = Issuer
            elif self.agent_type == AgentType.holder:
                cls = HolderProver
            elif self.agent_type == AgentType.verifier:
                cls = Verifier
            else:
                raise IndyConfigError("Unknown agent type")
            self._instance = cls(wallet.instance, self.extended_config)
        await self.open()

    async def open(self) -> None:
        """
        Open the agent instance for storing or issuing credentials
        """
        if not self.opened:
            self.opened = await self._instance.open()
            if isinstance(self._instance, HolderProver):
                # NOTE: should only create this once,
                # and only in the root wallet (virtual_wallet == None)
                await self._instance.create_link_secret(str(uuid.uuid4()))

    async def close(self) -> None:
        """
        Close the agent instance
        """
        if self.opened:
            await self._instance.close()
            self.opened = False

    def add_credential_type(self, schema: 'SchemaCfg', **params) -> None:
        """
        Add a credential type to the Agent configuration

        Args:
            schema: the :class:`SchemaCfg` to be added
        """
        if self.agent_type != AgentType.issuer:
            raise IndyConfigError("Only agent of type 'issuer' may publish schemas")
        self.cred_types.append({
            "definition": schema,
            "ledger_schema": None,
            "cred_def": None,
            "params": params,
        })

    def find_credential_type(self, name: str, version: str, origin_did: str = None) -> dict:
        """
        Find the extended information for a specific schema, including the ledger schema
        definition and credential definition (if any)

        Args:
            name: the schema name to be located
            version: the schema version to be located
        """
        match = SchemaCfg(name, version, None, origin_did)
        for cred_type in self.cred_types:
            if cred_type["definition"].compare(match):
                return cred_type
        return None

    def get_connection_params(self, _connection: 'ConnectionCfg') -> dict:
        """
        Get parameters required for initializing the connection
        """
        if self.agent_type == AgentType.issuer:
            cred_specs = []
            for cred_type in self.cred_types:
                params = cred_type["params"]
                type_spec = {
                    "schema": cred_type["definition"],
                    "source_claim": params.get("source_claim"),
                }
                if "description" in params:
                    type_spec["description"] = params["description"]
                if "issuer_url" in params:
                    type_spec["issuer_url"] = params["issuer_url"]
                if "mapping" in params:
                    type_spec["mapping"] = params["mapping"]
                cred_specs.append(type_spec)
            return {
                "abbreviation": self.abbreviation,
                "credential_types": cred_specs,
                "did": self.did,
                "email": self.email,
                "name": self.name,
                "url": self.url,
            }
        return None


class ConnectionCfg:
    """
    Manage configuration settings for a connection between an issuer and a target
    """
    def __init__(self, connection_type: str, agent_id: str, agent_type: str, **params):
        self.connection_id = params.get("id")
        self.agent_id = agent_id
        self.agent_type = agent_type
        try:
            self.connection_type = ConnectionType(connection_type)
        except KeyError:
            raise IndyConfigError("Unsupported connection type: {}".format(connection_type))
        self._instance = None
        self.connection_params = params
        self.opened = False
        self.synced = False

        if self.connection_type != ConnectionType.TheOrgBook and \
                self.connection_type != ConnectionType.holder:
            raise IndyConfigError("Only TOB and Holder connections are currently supported")

    @property
    def created(self) -> bool:
        """
        Accessor for the current created status of the connection instance
        """
        return self._instance is not None

    @property
    def instance(self) -> ConnectionBase:
        """
        Accessor for the connection instance
        """
        return self._instance

    @property
    def status(self) -> dict:
        """
        Accessor for the status of the connection
        """
        return {
            "created": self.created,
            "opened": self.opened,
            "synced": self.synced,
        }

    async def create(self, agent_params: dict) -> None:
        """
        Create the connection instance

        Args:
            agent_params: extra parameters assembled by the agent service for this connection
        """
        if self.connection_type == ConnectionType.TheOrgBook:
            cls = TobConnection
        elif self.connection_type == ConnectionType.holder:
            cls = HolderConnection
        self._instance = cls(self.agent_id, self.agent_type, agent_params, self.connection_params)

    async def open(self, service: 'IndyService') -> None:
        """
        Open the connection

        Args:
            service: the Indy service handling this connection
        """
        if not self.opened:
            await self._instance.open(service)
            self.opened = True

    async def sync(self) -> None:
        """
        Perform synchronization of the connection instance
        """
        if not self.synced:
            await self._instance.sync()
            self.synced = True

    async def close(self) -> None:
        """
        Close the connection instance
        """
        if self.opened:
            await self._instance.close()
            self.opened = False


class ProofSpecCfg:
    """
    A proof request specification
    """
    def __init__(self, **params):
        self.spec_id = params.get("id")
        self.version = params.get("version")
        if not self.version:
            raise IndyConfigError("Missing version for proof spec: {}".format(self.spec_id))
        self.schemas = params.get("schemas")
        if not self.schemas:
            raise IndyConfigError("Missing schemas for proof spec: {}".format(self.spec_id))
        self.synced = not self.get_incomplete_schemas()

    @property
    def status(self) -> dict:
        """
        Accessor for the status of the proof specification
        """
        return {
            "synced": self.synced,
        }

    def get_incomplete_schemas(self) -> set:
        """
        Get a set of schemas which have yet to be populated with details from the ledger
        """
        missing = set()
        for schema in self.schemas:
            if not schema.get("definition"):
                s_key = schema["key"]
                missing.add((s_key["name"], s_key["version"], s_key.get("did")))
        return missing

    def populate_schema(self, found_schema: 'SchemaCfg') -> None:
        """
        Populate required schema details from the ledger
        """
        for schema in self.schemas:
            if not schema.get("definition"):
                s_key = schema["key"]
                cfg = SchemaCfg(s_key["name"], s_key["version"], None, s_key.get("did"))
                if cfg.compare(found_schema):
                    schema["definition"] = found_schema.copy()
                    if not schema.get("attributes"):
                        schema["attributes"] = found_schema.attr_names


class SchemaCfg:
    """
    A credential schema definition
    """
    def __init__(self, name: str, version: str = None, attributes=None, origin_did: str = None):
        self.name = name
        self.version = version
        self._attributes = []
        if attributes:
            self.attributes = attributes
        self.origin_did = origin_did

    @property
    def schema_id(self) -> str:
        """
        Accessor for the schema_id of this schema
        """
        return schema_id(self.origin_did, self.name, self.version)

    @property
    def attributes(self) -> list:
        """
        Accessor for the extended schema attributes list

        Returns:
            a copy of the schema attributes
        """
        return self._attributes.copy()

    @attributes.setter
    def attributes(self, value) -> None:
        """
        Setter for the schema attributes list
        """
        self._attributes = []
        if isinstance(value, Mapping):
            for name, attr in value.items():
                self.add_attribute(attr, name)
        elif isinstance(value, Sequence):
            for attr in value:
                self.add_attribute(attr)
        else:
            raise IndyConfigError('Unsupported type for attributes: {}'.format(value))

    @property
    def attr_names(self) -> list:
        """
        Accessor for the schema attribute names

        Returns:
            the attribute names only
        """
        return tuple(attr['name'] for attr in self._attributes)

    def add_attribute(self, attr, name=None) -> None:
        """
        Add an attribute to the schema including optional type information

        Args:
            attr: a dict or str representing the attribute
            name: the name of the attribute
        """
        if isinstance(attr, Mapping):
            if name is not None:
                attr['name'] = name
            self._attributes.append(attr)
        elif isinstance(attr, str):
            attr = {'name': attr}
            self._attributes.append(attr)
        elif attr is None and name:
            self._attributes.append({'name': name})
        else:
            raise IndyConfigError('Unsupported type for attribute: {}'.format(attr))

    def copy(self) -> 'SchemaCfg':
        """
        Create a copy of this :class:`SchemaCfg` instance
        """
        return SchemaCfg(self.name, self.version, self._attributes, self.origin_did)

    def validate(self, value) -> None:
        """
        Perform validation of a set of attribute values against the schema
        """
        pass

    def compare(self, schema: 'SchemaCfg') -> bool:
        """
        Check whether this schema instance and another are compatible.
        Note: schemas with an empty issuer DID will match schemas with a blank issuer DID,
        or the same DID
        """
        if self.name != schema.name:
            return False
        if self.version and schema.version and self.version != schema.version:
            return False
        if self.origin_did and schema.origin_did and self.origin_did != schema.origin_did:
            return False
        if self.attributes and schema.attributes and self.attributes != schema.attributes:
            return False
        return True

    def __repr__(self) -> str:
        return 'SchemaCfg(name={}, version={}, origin_did={})'.format(
            self.name, self.version, self.origin_did)


class SchemaManager:
    """
    A manager class for handling a set of loaded credential schema definitions
    """

    def __init__(self):
        self._schemas = []

    @property
    def schemas(self) -> list:
        """
        An accessor for the list of all loaded schemas
        """
        return self._schemas.copy()

    def add_schema(self, schema, override=False) -> None:
        """
        Add a schema to the manager

        Args:
            schema: a :class:`SchemaCfg` or dict instance
            override: replace an existing schema if any
        """
        if not isinstance(schema, SchemaCfg):
            if not isinstance(schema, Mapping):
                raise IndyConfigError('Unsupported type for schema: {}'.format(schema))
            name = schema.get('name')
            if not name:
                raise IndyConfigError('Missing schema name')
            schema = SchemaCfg(name, schema.get('version'), schema.get('attributes'))
        found = self.find(schema.name, schema.version)
        if found:
            if override:
                self.remove_schema(found)
            else:
                raise IndyConfigError('Duplicate schema definition: {}'.format(schema))
        self._schemas.append(schema)

    def remove_schema(self, schema, version=None) -> None:
        """
        Remove an existing schema from the manager

        Args:
            schema: the schema name
            version: the schema version
        """
        if isinstance(schema, str):
            schema = self.find(schema, version)
        self._schemas.remove(schema)

    def load(self, values: Sequence, override=False) -> None:
        """
        Load a list of schemas and add each to the manager

        Args:
            values: the list of schema definitions
            override: replace existing defined schemas of the same name and version
        """
        for spec in values:
            self.add_schema(spec, override)

    def find(self, name: str, version: str = None) -> SchemaCfg:
        """
        Locate a defined schema

        Args:
            name: the schema name
            version: the schema version

        Returns:
            the located :class:`SchemaCfg` instance, if any
        """
        found = None
        for schema in self._schemas:
            if schema.name == name:
                if version is not None:
                    if schema.version == version:
                        found = schema
                        break
                else:
                    if found is None or LooseVersion(found.version) < LooseVersion(schema.version):
                        found = schema
        return found


class WalletCfg:
    """
    Manage configuration settings for an Indy wallet
    """
    def __init__(self, **params):
        self.wallet_id = params.get("id")
        self.name = params.get("name")
        if not self.name:
            raise IndyConfigError("Missing wallet name")
        self.seed = params.get("seed")
        if not self.seed:
            raise IndyConfigError("Missing seed for wallet '{}'".format(self.name))
        if len(self.seed) != 32:
            raise IndyConfigError(
                "Wallet seed length is not 32 characters: {}".format(self.seed)
            )
        self.type = params.get("type")  # default to virtual?
        self.params = params.get("params") or {}
        if "freshness_time" not in self.params:
            self.params["freshness_time"] = 0
        self.access_creds = params.get("access_creds") or {"key": ""}
        self._instance = None

    @property
    def created(self) -> bool:
        """
        Accessor for the current created status of the wallet instance
        """
        return self._instance and self._instance.created

    @property
    def instance(self) -> Wallet:
        """
        Accessor for the wallet instance
        """
        return self._instance

    @property
    def opened(self) -> bool:
        """
        Accessor for the current opened status of the wallet instance
        """
        return self._instance and self._instance.handle is not None

    @property
    def status(self) -> dict:
        """
        Accessor for the current status of the wallet instance
        """
        return {
            "created": self.created,
            "opened": self.opened,
        }

    async def create(self, pool: NodePool) -> None:
        """
        Create the wallet instance

        Args:
            pool: the initialized :class:`NodePool` instance for the wallet
        """
        self._instance = Wallet(
            pool,
            self.seed,
            self.name,
            self.type,
            self.params,
            self.access_creds)
        await self.instance.create()

    async def open(self):
        """
        Open the wallet instance
        """
        await self._instance.open()

    async def close(self) -> None:
        """
        Close the wallet instance
        """
        if self.opened:
            await self._instance.close()
