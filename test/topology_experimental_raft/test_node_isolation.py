#
# Copyright (C) 2023-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import logging
import pytest
import time

from cassandra.cluster import ConsistencyLevel, ExecutionProfile, EXEC_PROFILE_DEFAULT # type: ignore
from cassandra.cluster import NoHostAvailable, OperationTimedOut # type: ignore
from cassandra.query import SimpleStatement # type: ignore
from cassandra.policies import WhiteListRoundRobinPolicy  # type: ignore

from test.pylib.manager_client import ManagerClient
from test.pylib.util import wait_for_cql_and_get_hosts, read_barrier

from cassandra.cluster import Cluster


logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_banned_node_cannot_communicate(manager: ManagerClient) -> None:
    """Test that a node banned from the cluster is not able to perform inserts
       that require communicating with other nodes."""
    # Decrease the failure detector threshold so we don't have to wait for too long.
    config = {
        'failure_detector_timeout_in_ms': 2000
    }
    srvs = await manager.servers_add(3, config=config)
    cql = manager.get_cql()

    # Use RF=2 keyspace and below CL=ALL so that performing an INSERT requires
    # communicating with another node.
    await cql.run_async("create keyspace ks with replication = "
                        "{'class': 'SimpleStrategy', 'replication_factor': 2}")
    await cql.run_async("create table ks.t (pk int primary key)")

    # Pause one of the servers so other nodes mark it as dead and we can remove it.
    # We deliberately don't shut it down, but only pause it - we want to test
    # that we solved the harder problem of safely removing nodes which didn't shut down.
    logger.info(f"Pausing server {srvs[2]}")
    await manager.server_pause(srvs[2].server_id)
    logger.info(f"Waiting until server {srvs[0]} marks {srvs[2]} as dead")
    await manager.server_not_sees_other_server(srvs[0].ip_addr, srvs[2].ip_addr)
    logger.info(f"Removing {srvs[2]} using {srvs[0]}")
    await manager.remove_node(srvs[0].server_id, srvs[2].server_id)
    # Perform a read barrier on srvs[1] so it learns about the ban.
    logger.info(f"Performing read barrier on server {srvs[1]}")
    host = (await wait_for_cql_and_get_hosts(cql, [srvs[1]], time.time() + 60))[0]
    await read_barrier(cql, host)
    logger.info(f"Unpausing {srvs[2]}")
    await manager.server_unpause(srvs[2].server_id)

    # We need a separate driver session to communicate with the removed server,
    # the original driver session bugs out.
    logger.info(f"Connecting to {srvs[2]}")
    with manager.con_gen([srvs[2].ip_addr], manager.port, manager.use_ssl) as c:
        with c.connect() as s:
            logger.info(f"Connected, sending request")
            q = SimpleStatement('insert into ks.t (pk) values (0)', consistency_level=ConsistencyLevel.ALL)
            # Before introducing host banning, a removed node was able to participate
            # as if it was a normal node and, for example, could insert data into the cluster.
            # Now other nodes refuse to communicate so we'll get an exception.
            with pytest.raises((NoHostAvailable, OperationTimedOut)):
                await s.run_async(q, execution_profile='whitelist', timeout=5)
