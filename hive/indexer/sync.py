"""Hive sync manager."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
from pathlib import Path
from time import perf_counter as perf
from typing import Final, Tuple

from hive.conf import Conf, SCHEMA_NAME
from hive.db.adapter import Db
from hive.db.db_state import DbState
from hive.db.schema import execute_sql_script
from hive.indexer.accounts import Accounts
from hive.indexer.block import BlocksProviderBase
from hive.indexer.blocks import Blocks
from hive.indexer.community import Community
from hive.indexer.db_adapter_holder import DbLiveContextHolder
from hive.indexer.hive_db.block import BlockHiveDb
from hive.indexer.hive_db.massive_blocks_data_provider import MassiveBlocksDataProviderHiveDb
from hive.indexer.mock_block_provider import MockBlockProvider
from hive.indexer.mock_vops_provider import MockVopsProvider
from hive.server.common.mentions import Mentions
from hive.server.common.payout_stats import PayoutStats
from hive.steem.signal import (
    can_continue_thread,
    restore_default_signal_handlers,
    set_custom_signal_handlers,
    set_exception_thrown,
)
from hive.utils.communities_rank import update_communities_posts_and_rank
from hive.utils.misc import log_memory_usage
from hive.utils.stats import BroadcastObject
from hive.utils.stats import FlushStatusManager as FSM
from hive.utils.stats import OPStatusManager as OPSM
from hive.utils.stats import PrometheusClient as PC
from hive.utils.stats import WaitingStatusManager as WSM
from hive.utils.timer import Timer

log = logging.getLogger(__name__)


class SyncHiveDb:
    def __init__(self, conf: Conf):
        self._conf = conf
        self._db = conf.db()

        # Might be lower or higher than actual block number stored in HAF database
        self._last_block_to_process = self._conf.get('test_max_block')

        self._last_block_for_massive_sync = self._conf.get('test_last_block_for_massive')

        self._massive_blocks_data_provider = None
        self._lbound = None
        self._ubound = None
        self._databases = None
        self._were_mocks_after_db_blocks = False

    def __enter__(self):
        log.info("Entering HAF mode synchronization")
        set_custom_signal_handlers()

        DbLiveContextHolder.set_live_context(False)
        self._databases = MassiveBlocksDataProviderHiveDb.Databases(db_root=self._db, conf=self._conf)

        Blocks.setup(conf=self._conf)

        Community.start_block = self._conf.get("community_start_block")
        DbState.initialize()

        self._show_info(self._db)

        self._check_log_explain_queries()

        if not Blocks.is_consistency():
            raise RuntimeError("Fatal error related to `hive_blocks` consistency")
        self._load_mock_data()
        Accounts.load_ids()  # prefetch id->name and id->rank memory maps
        update_communities_posts_and_rank(self._db)

        self._prepare_app_context()
        self._prepare_app_schema()

        self._massive_blocks_data_provider = MassiveBlocksDataProviderHiveDb(
            databases=self._databases,
            number_of_blocks_in_batch=self._conf.get('max_batch'),
        )

        return self

    def __exit__(self, exc_type, value, traceback):
        log.info("Exiting HAF mode synchronization")

        if not self._were_mocks_after_db_blocks:
            self._context_detach()  # context attaching requires context to be detached or error will be raised
            self._context_attach()

        last_imported_block = Blocks.head_num()
        DbState.finish_massive_sync(current_imported_block=last_imported_block)
        PayoutStats.generate()

        Blocks.close_own_db_access()
        if self._databases:
            self._databases.close()
        if not DbLiveContextHolder.is_live_context():
            Blocks.close_own_db_access()

    def run(self) -> None:
        log.info(f"Using HAF database as block data provider, pointed by url: '{self._conf.get('database_url')}'")

        while True:
            if not can_continue_thread():
                restore_default_signal_handlers()
                return

            last_imported_block = Blocks.head_num()
            log.info(f"Last imported block is: {last_imported_block}")

            self._lbound, self._ubound = self._query_for_app_next_block()

            allow_massive = True
            if self._last_block_for_massive_sync and self._lbound:
                if self._lbound < self._last_block_for_massive_sync:
                    self._ubound = self._last_block_for_massive_sync

                if self._lbound > self._last_block_for_massive_sync:
                    allow_massive = False

            if self._last_block_to_process:
                if last_imported_block >= self._last_block_to_process:
                    log.info(f"REACHED test_max_block of {self._last_block_to_process}")
                    return

                if not (self._lbound and self._ubound):  # all blocks from HAF db processed
                    self._lbound = last_imported_block + 1
                    self._ubound = self._last_block_to_process
                    self._were_mocks_after_db_blocks = True
                else:
                    self._ubound = min(self._last_block_to_process, self._ubound)

            if not (self._lbound and self._ubound):
                continue

            log.info(f"target_head_block: {self._ubound}")
            log.info(f"test_max_block: {self._last_block_to_process}")

            if self._ubound - self._lbound > 100 and allow_massive:
                # mode with detached indexes and context
                log.info("[MASSIVE] *** MASSIVE blocks processing ***")
                self._massive_blocks_data_provider.update_sync_block_range(self._lbound, self._ubound)

                DbState.before_massive_sync(self._lbound, self._ubound)

                self._context_detach()
                self._catchup_irreversible_block(is_massive_sync=True)
                self._context_attach()

                last_block = Blocks.head_num()
                DbState.finish_massive_sync(current_imported_block=last_block)
            else:
                # mode with attached indexes and context
                log.info("[SINGLE] *** SINGLE block processing***")
                self._massive_blocks_data_provider.update_sync_block_range(self._lbound, self._lbound)

                log.info(f"Attempting to process first block in range: <{self._lbound}:{self._ubound}>")
                self._blocks_data_provider(self._massive_blocks_data_provider)
                blocks = self._massive_blocks_data_provider.get(number_of_blocks=1)
                Blocks.process_multi(blocks, is_massive_sync=False)
                self._periodic_actions(blocks[0])

    def _periodic_actions(self, block: BlockHiveDb) -> None:
        """Actions performed at a given time, calculated on the basis of the current block number"""

        if (block_num := block.get_num()) % 1200 == 0:  # 1hour
            log.warning(f"head block {block_num} @ {block.get_date()}")
            log.info("[SINGLE] hourly stats")
            log.info("[SINGLE] filling payout_stats_view executed")
            with ThreadPoolExecutor(max_workers=2) as executor:
                executor.submit(PayoutStats.generate)
                executor.submit(Mentions.refresh)
        elif block_num % 200 == 0:  # 10min
            log.info("[SINGLE] 10min")
            log.info("[SINGLE] updating communities posts and rank")
            update_communities_posts_and_rank(self._db)

    def _prepare_app_context(self) -> None:
        log.info(f"Looking for '{SCHEMA_NAME}' context.")
        ctx_present = self._db.query_one(
            f"SELECT hive.app_context_exists('{SCHEMA_NAME}') as ctx_present;"
        )
        if not ctx_present:
            log.info(f"No application context present. Attempting to create a '{SCHEMA_NAME}' context...")
            self._db.query_no_return(f"SELECT hive.app_create_context('{SCHEMA_NAME}');")
            log.info("Application context creation done.")

    def _prepare_app_schema(self) -> None:
        log.info("Attempting to create application schema...")
        script_path = Path(__file__).parent.parent / "db/sql_scripts/hafapp_api.sql"

        log.info(f"Attempting to execute SQL script: '{script_path}'")
        execute_sql_script(query_executor=self._db.query_no_return, path_to_script=script_path)
        log.info("Application schema created.")

    def _query_for_app_next_block(self) -> Tuple[int, int]:
        log.info("Querying for next block for app context...")
        self._db.query("START TRANSACTION")
        lbound, ubound = self._db.query_row(f"SELECT * FROM hive.app_next_block('{SCHEMA_NAME}')")
        self._db.query("COMMIT")
        log.info(f"Next block range from hive.app_next_block is: <{lbound}:{ubound}>")
        return lbound, ubound

    def _catchup_irreversible_block(self, is_massive_sync: bool = False) -> None:
        assert self._massive_blocks_data_provider is not None

        log.info(f"Attempting to process block range: <{self._lbound}:{self._ubound}>")
        self._process_blocks_from_provider(
            massive_block_provider=self._massive_blocks_data_provider,
            is_massive_sync=is_massive_sync,
            lbound=self._lbound,
            ubound=self._ubound,
        )
        log.info(f"Block range: <{self._lbound}:{self._ubound}> processing finished")

    def _context_detach(self) -> None:
        is_attached = self._db.query_one(f"SELECT hive.app_context_is_attached('{SCHEMA_NAME}')")
        if is_attached:
            log.info("Trying to detach app context...")
            self._db.query_no_return(f"SELECT hive.app_context_detach('{SCHEMA_NAME}')")
            log.info("App context detaching done.")
        else:
            log.info("No attached context - detach skipped.")

    def _context_attach(self) -> None:
        last_block = Blocks.head_num()
        log.info(f"Trying to attach app context with block number: {last_block}")
        self._db.query_no_return(f"SELECT hive.app_context_attach('{SCHEMA_NAME}', {last_block})")
        log.info("App context attaching done.")

    def _check_log_explain_queries(self) -> None:
        if self._conf.get("log_explain_queries"):
            is_superuser = self._db.query_one("SELECT is_superuser()")
            assert (
                is_superuser
            ), 'The parameter --log_explain_queries=true can be used only when connect to the database with SUPERUSER privileges'

    def _load_mock_data(self) -> None:
        paths = self._conf.get("mock_block_data_path") or []
        for path in paths:
            MockBlockProvider.load_block_data(path)

        if mock_vops_data_path := self._conf.get("mock_vops_data_path"):
            MockVopsProvider.load_block_data(mock_vops_data_path)

    @staticmethod
    def _show_info(database: Db) -> None:
        last_block = Blocks.head_num()

        sql = f"SELECT level, patch_date, patched_to_revision FROM {SCHEMA_NAME}.hive_db_patch_level ORDER BY level DESC LIMIT 1"
        patch_level_data = database.query_row(sql)

        from hive.utils.misc import show_app_version

        show_app_version(log, last_block, patch_level_data)

    @staticmethod
    def _blocks_data_provider(blocks_data_provider: BlocksProviderBase) -> None:
        try:
            futures = blocks_data_provider.start()

            for future in futures:
                exception = future.exception()
                if exception:
                    raise exception
        except:
            log.exception("Exception caught during fetching blocks data")
            raise

    @staticmethod
    def _block_consumer(
        blocks_data_provider: BlocksProviderBase, is_massive_sync: bool, lbound: int, ubound: int
    ) -> int:
        from hive.utils.stats import minmax

        is_debug = log.isEnabledFor(10)
        num = 0
        time_start = OPSM.start()
        rate = {}
        LIMIT_FOR_PROCESSED_BLOCKS = 1000

        rate = minmax(rate, 0, 1.0, 0)

        def print_summary():
            stop = OPSM.stop(time_start)
            log.info("=== TOTAL STATS ===")
            wtm = WSM.log_global("Total waiting times")
            ftm = FSM.log_global("Total flush times")
            otm = OPSM.log_global("All operations present in the processed blocks")
            ttm = ftm + otm + wtm
            log.info("Elapsed time: %.4fs. Calculated elapsed time: %.4fs. Difference: %.4fs", stop, ttm, stop - ttm)
            if rate:
                log.info(
                    "Highest block processing rate: %.4f bps. %d:%d", rate['max'], rate['max_from'], rate['max_to']
                )
                log.info("Lowest block processing rate: %.4f bps. %d:%d", rate['min'], rate['min_from'], rate['min_to'])
            log.info("=== TOTAL STATS ===")

        try:
            Blocks.set_end_of_sync_lib(ubound)
            count = ubound - lbound + 1
            timer = Timer(count, entity='block', laps=['rps', 'wps'])

            while lbound <= ubound:
                number_of_blocks_to_proceed = min([LIMIT_FOR_PROCESSED_BLOCKS, ubound - lbound + 1])
                time_before_waiting_for_data = perf()

                blocks = blocks_data_provider.get(number_of_blocks_to_proceed)

                if not can_continue_thread():
                    break

                assert len(blocks) == number_of_blocks_to_proceed

                to = min(lbound + number_of_blocks_to_proceed, ubound + 1)
                timer.batch_start()

                block_start = perf()
                Blocks.process_multi(blocks, is_massive_sync)
                block_end = perf()

                timer.batch_lap()
                timer.batch_finish(len(blocks))
                time_current = perf()

                prefix = (
                    "[MASSIVE]"
                    f" Got block {min(lbound + number_of_blocks_to_proceed - 1, ubound)} @ {blocks[-1].get_date()}"
                )

                log.info(timer.batch_status(prefix))
                log.info(f"[MASSIVE] Time elapsed: {time_current - time_start}s")
                log.info(f"[MASSIVE] Current system time: {datetime.now().strftime('%H:%M:%S')}")
                log.info(log_memory_usage())
                rate = minmax(rate, len(blocks), time_current - time_before_waiting_for_data, lbound)

                if block_end - block_start > 1.0 or is_debug:
                    otm = OPSM.log_current("Operations present in the processed blocks")
                    ftm = FSM.log_current("Flushing times")
                    wtm = WSM.log_current("Waiting times")
                    log.info(f"Calculated time: {otm + ftm + wtm:.4f} s.")

                OPSM.next_blocks()
                FSM.next_blocks()
                WSM.next_blocks()

                lbound = to
                PC.broadcast(BroadcastObject('sync_current_block', lbound, 'blocks'))

                num = num + 1

                if not can_continue_thread():
                    break
        except Exception:
            log.exception("Exception caught during processing blocks...")
            set_exception_thrown()
            print_summary()
            raise

        print_summary()
        return num

    @classmethod
    def _process_blocks_from_provider(
        cls, massive_block_provider: BlocksProviderBase, is_massive_sync: bool, lbound: int, ubound: int
    ) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            block_data_provider_future = pool.submit(cls._blocks_data_provider, massive_block_provider)
            block_consumer_future = pool.submit(
                cls._block_consumer, massive_block_provider, is_massive_sync, lbound, ubound
            )

            consumer_exception = block_consumer_future.exception()
            block_data_provider_exception = block_data_provider_future.exception()

            if consumer_exception:
                raise consumer_exception

            if block_data_provider_exception:
                raise block_data_provider_exception
