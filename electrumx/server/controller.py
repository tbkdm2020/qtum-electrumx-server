# Copyright (c) 2016-2017, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

from asyncio import Event

from aiorpcx import _version as aiorpcx_version, TaskGroup

import electrumx
from electrumx.lib.server_base import ServerBase
from electrumx.lib.util import version_string
from electrumx.server.chain_state import ChainState
from electrumx.server.mempool import MemPool
from electrumx.server.session import SessionManager


class Notifications(object):
    # hashX notifications come from two sources: new blocks and
    # mempool refreshes.
    #
    # A user with a pending transaction is notified after the block it
    # gets in is processed.  Block processing can take an extended
    # time, and the prefetcher might poll the daemon after the mempool
    # code in any case.  In such cases the transaction will not be in
    # the mempool after the mempool refresh.  We want to avoid
    # notifying clients twice - for the mempool refresh and when the
    # block is done.  This object handles that logic by deferring
    # notifications appropriately.

    def __init__(self):
        self._touched_mp = {}
        self._touched_bp = {}
        self._highest_block = 0
        self._notify_funcs = []
        self._eventlog_touched = {}

    async def _maybe_notify(self):
        tmp, tbp = self._touched_mp, self._touched_bp
        common = set(tmp).intersection(tbp)
        if common:
            height = max(common)
        elif tmp and max(tmp) == self._highest_block:
            height = self._highest_block
        else:
            # Either we are processing a block and waiting for it to
            # come in, or we have not yet had a mempool update for the
            # new block height
            return
        touched = tmp.pop(height)
        touched.update(tbp.pop(height, set()))
        for old in [h for h in tmp if h <= height]:
            del tmp[old]
        for old in [h for h in tbp if h <= height]:
            del tbp[old]
        eventlog_touched = self._eventlog_touched.pop(height)
        for notify_func in self._notify_funcs:
            await notify_func(height, touched, eventlog_touched)

    def add_callback(self, notify_func):
        self._notify_funcs.append(notify_func)

    async def on_mempool(self, touched, height):
        self._touched_mp[height] = touched
        await self._maybe_notify()

    async def on_block(self, touched, eventlog_touched, height):
        self._touched_bp[height] = touched
        self._highest_block = height
        self._eventlog_touched[height] = eventlog_touched
        await self._maybe_notify()


class Controller(ServerBase):
    '''Manages server initialisation and stutdown.

    Servers are started once the mempool is synced after the block
    processor first catches up with the daemon.
    '''
    async def serve(self, shutdown_event):
        '''Start the RPC server and wait for the mempool to synchronize.  Then
        start serving external clients.
        '''
        if not (0, 6, 2) <= aiorpcx_version < (0, 7):
            raise RuntimeError('aiorpcX version 0.6.x with x>=2 required')

        env = self.env
        min_str, max_str = env.coin.SESSIONCLS.protocol_min_max_strings()
        self.logger.info(f'software version: {electrumx.version}')
        self.logger.info(f'aiorpcX version: {version_string(aiorpcx_version)}')
        self.logger.info(f'supported protocol versions: {min_str}-{max_str}')
        self.logger.info(f'event loop policy: {env.loop_policy}')
        self.logger.info(f'reorg limit is {env.reorg_limit:,d} blocks')

        notifications = Notifications()
        daemon = env.coin.DAEMON(env)
        BlockProcessor = env.coin.BLOCK_PROCESSOR
        bp = BlockProcessor(env, daemon, notifications)
        mempool = MemPool(env.coin, daemon, notifications, bp.lookup_utxos)
        chain_state = ChainState(env, daemon, bp)
        session_mgr = SessionManager(env, chain_state, mempool,
                                     notifications, shutdown_event)

        caught_up_event = Event()
        serve_externally_event = Event()
        synchronized_event = Event()

        async with TaskGroup() as group:
            await group.spawn(session_mgr.serve(serve_externally_event))
            await group.spawn(bp.fetch_and_process_blocks(caught_up_event))
            await caught_up_event.wait()
            await group.spawn(mempool.keep_synchronized(synchronized_event))
            await synchronized_event.wait()
            serve_externally_event.set()
