"""Blocks processor."""

import logging
import json

from hive.db.adapter import Db

from hive.indexer.accounts import Accounts
from hive.indexer.posts import Posts
from hive.indexer.custom_op import CustomOp
from hive.indexer.payments import Payments
from hive.indexer.follow import Follow
from hive.indexer.votes import Votes
from hive.indexer.post_data_cache import PostDataCache
from hive.indexer.tags import Tags
from time import perf_counter

log = logging.getLogger(__name__)

DB = Db.instance()

class Blocks:
    """Processes blocks, dispatches work, manages `hive_blocks` table."""
    blocks_to_flush = []
    ops_stats = {}
    _head_block_date = None

    def __init__(cls):
        head_date = cls.head_date()
        if(head_date == ''):
            cls._head_block_date = None
        else:
            cls._head_block_date = head_date

    @staticmethod
    def merge_ops_stats(od1, od2):
        if od2 is not None:
            for k, v in od2.items():
                if k in od1:
                    od1[k] += v
                else:
                    od1[k] = v

        return od1

    @classmethod
    def head_num(cls):
        """Get hive's head block number."""
        sql = "SELECT num FROM hive_blocks ORDER BY num DESC LIMIT 1"
        return DB.query_one(sql) or 0

    @classmethod
    def head_date(cls):
        """Get hive's head block date."""
        sql = "SELECT created_at FROM hive_blocks ORDER BY num DESC LIMIT 1"
        return str(DB.query_one(sql) or '')

    @classmethod
    def process(cls, block, vops_in_block, hived):
        """Process a single block. Always wrap in a transaction!"""
        time_start = perf_counter()
        #assert is_trx_active(), "Block.process must be in a trx"
        ret = cls._process(block, vops_in_block, hived, is_initial_sync=False)
        PostDataCache.flush()
        Tags.flush()
        Votes.flush()
        time_end = perf_counter()
        log.info("[PROCESS BLOCK] %fs", time_end - time_start)
        return ret

    @classmethod
    def process_multi(cls, blocks, vops, hived, is_initial_sync=False):
        """Batch-process blocks; wrapped in a transaction."""
        time_start = perf_counter()
        DB.query("START TRANSACTION")

        last_num = 0
        try:
            for block in blocks:
                last_num = cls._process(block, vops, hived, is_initial_sync)
        except Exception as e:
            log.error("exception encountered block %d", last_num + 1)
            raise e

        # Follows flushing needs to be atomic because recounts are
        # expensive. So is tracking follows at all; hence we track
        # deltas in memory and update follow/er counts in bulk.
        PostDataCache.flush()
        Tags.flush()
        Votes.flush()
        cls._flush_blocks()
        Follow.flush(trx=False)

        DB.query("COMMIT")
        time_end = perf_counter()
        log.info("[PROCESS MULTI] %i blocks in %fs", len(blocks), time_end - time_start)

        return cls.ops_stats

    @staticmethod
    def prepare_vops(vopsList, date):
        vote_ops = {}
        comment_payout_ops = {}
        for vop in vopsList:
            key = None
            val = None

            op_type = vop['type']
            op_value = vop['value']
            if op_type == 'curation_reward_operation':
                key = "{}/{}".format(op_value['comment_author'], op_value['comment_permlink'])
                val = {'reward' : op_value['reward']}
            elif op_type == 'author_reward_operation':
                key = "{}/{}".format(op_value['author'], op_value['permlink'])
                val = {'hbd_payout':op_value['hbd_payout'], 'hive_payout':op_value['hive_payout'], 'vesting_payout':op_value['vesting_payout']}
            elif op_type == 'comment_reward_operation':
                key = "{}/{}".format(op_value['author'], op_value['permlink'])
                val = {'payout':op_value['payout'], 'author_rewards':op_value['author_rewards'], 'total_payout_value':op_value['total_payout_value'], 'curator_payout_value':op_value['curator_payout_value'], 'beneficiary_payout_value':op_value['beneficiary_payout_value'] }

            elif op_type == 'effective_comment_vote_operation':
                key = "{}/{}".format(op_value['author'], op_value['permlink'])
                val = {'pending_payout':op_value['pending_payout']}
                vote_ops.append(vop)
            elif op_type == 'comment_payout_update_operation':
                key = "{}/{}".format(op_value['author'], op_value['permlink'])
                val = {'is_paidout': True} # comment_payout_update_operation implicates is_paidout (is generated only when post is paidout)

            if key is not None and val is not None:
                if key in comment_payout_ops:
                    comment_payout_ops[key].append({op_type:val})
                else:
                    comment_payout_ops[key] = [{op_type:val}]

        return (vote_ops, comment_payout_ops)


    @classmethod
    def _process(cls, block, virtual_operations, hived, is_initial_sync=False):
        """Process a single block. Assumes a trx is open."""
        #pylint: disable=too-many-branches
        num = cls._push(block)
        block_date = block['timestamp']

        # head block date shall point to last imported block (not yet current one) to conform hived behavior.
        # that's why operations processed by node are included in the block being currently produced, so its processing time is equal to last produced block.
        if(cls._head_block_date is None):
            cls._head_block_date = block_date

        # [DK] we will make two scans, first scan will register all accounts
        account_names = set()
        for tx_idx, tx in enumerate(block['transactions']):
            for operation in tx['operations']:
                op_type = operation['type']
                op = operation['value']

                # account ops
                if op_type == 'pow_operation':
                    account_names.add(op['worker_account'])
                elif op_type == 'pow2_operation':
                    account_names.add(op['work']['value']['input']['worker_account'])
                elif op_type == 'account_create_operation':
                    account_names.add(op['new_account_name'])
                elif op_type == 'account_create_with_delegation_operation':
                    account_names.add(op['new_account_name'])
                elif op_type == 'create_claimed_account_operation':
                    account_names.add(op['new_account_name'])

        Accounts.register(account_names, cls._head_block_date)     # register any new names

        # second scan will process all other ops
        json_ops = []
        for tx_idx, tx in enumerate(block['transactions']):
            for operation in tx['operations']:
                op_type = operation['type']
                op = operation['value']

                if(op_type != 'custom_json_operation'):
                    if op_type in cls.ops_stats:
                        cls.ops_stats[op_type] += 1
                    else:
                        cls.ops_stats[op_type] = 1

                # account metadata updates
                if op_type == 'account_update_operation':
                    if not is_initial_sync:
                        Accounts.dirty(op['account']) # full
                elif op_type == 'account_update2_operation':
                    if not is_initial_sync:
                        Accounts.dirty(op['account']) # full

                # post ops
                elif op_type == 'comment_operation':
                    Posts.comment_op(op, cls._head_block_date)
                    if not is_initial_sync:
                        Accounts.dirty(op['author']) # lite - stats
                elif op_type == 'delete_comment_operation':
                    Posts.delete_op(op)
                elif op_type == 'comment_options_operation':
                    Posts.comment_options_op(op)
                elif op_type == 'vote_operation':
                    if not is_initial_sync:
                        Accounts.dirty(op['author']) # lite - rep
                        Accounts.dirty(op['voter']) # lite - stats

                # misc ops
                elif op_type == 'transfer_operation':
                    Payments.op_transfer(op, tx_idx, num, cls._head_block_date)
                elif op_type == 'custom_json_operation':
                    json_ops.append(op)

        # follow/reblog/community ops
        if json_ops:
            custom_ops_stats = CustomOp.process_ops(json_ops, num, cls._head_block_date)
            cls.ops_stats = Blocks.merge_ops_stats(cls.ops_stats, custom_ops_stats)

        # virtual ops
        comment_payout_ops = {}
        vote_ops = {}

        empty_vops = (vote_ops, comment_payout_ops)

        if is_initial_sync:
            (vote_ops, comment_payout_ops) = virtual_operations[num] if num in virtual_operations else empty_vops
        else:
            vops = hived.get_virtual_operations(num)
            (vote_ops, comment_payout_ops) = Blocks.prepare_vops(vops, cls._head_block_date)

        for k, v in vote_ops.items():
            Votes.effective_comment_vote_op(k, v, cls._head_block_date)

        if comment_payout_ops:
            comment_payout_stats = Posts.comment_payout_op(comment_payout_ops, cls._head_block_date)
            cls.ops_stats = Blocks.merge_ops_stats(cls.ops_stats, comment_payout_stats)

        cls._head_block_date = block_date

        return num

    @classmethod
    def verify_head(cls, steem):
        """Perform a fork recovery check on startup."""
        hive_head = cls.head_num()
        if not hive_head:
            return

        # move backwards from head until hive/steem agree
        to_pop = []
        cursor = hive_head
        while True:
            assert hive_head - cursor < 25, "fork too deep"
            hive_block = cls._get(cursor)
            steem_hash = steem.get_block(cursor)['block_id']
            match = hive_block['hash'] == steem_hash
            log.info("[INIT] fork check. block %d: %s vs %s --- %s",
                     hive_block['num'], hive_block['hash'],
                     steem_hash, 'ok' if match else 'invalid')
            if match:
                break
            to_pop.append(hive_block)
            cursor -= 1

        if hive_head == cursor:
            return # no fork!

        log.error("[FORK] depth is %d; popping blocks %d - %d",
                  hive_head - cursor, cursor + 1, hive_head)

        # we should not attempt to recover from fork until it's safe
        fork_limit = steem.last_irreversible()
        assert cursor < fork_limit, "not proceeding until head is irreversible"

        cls._pop(to_pop)

    @classmethod
    def _get(cls, num):
        """Fetch a specific block."""
        sql = """SELECT num, created_at date, hash
                 FROM hive_blocks WHERE num = :num LIMIT 1"""
        return dict(DB.query_row(sql, num=num))

    @classmethod
    def _push(cls, block):
        """Insert a row in `hive_blocks`."""
        num = int(block['block_id'][:8], base=16)
        txs = block['transactions']
        cls.blocks_to_flush.append({
            'num': num,
            'hash': block['block_id'],
            'prev': block['previous'],
            'txs': len(txs),
            'ops': sum([len(tx['operations']) for tx in txs]),
            'date': block['timestamp']})
        return num

    @classmethod
    def _flush_blocks(cls):
        query = """
            INSERT INTO 
                hive_blocks (num, hash, prev, txs, ops, created_at) 
            VALUES 
        """
        values = []
        for block in cls.blocks_to_flush:
            values.append("({}, '{}', '{}', {}, {}, '{}')".format(block['num'], block['hash'], block['prev'], block['txs'], block['ops'], block['date']))
        DB.query(query + ",".join(values))
        cls.blocks_to_flush = []

    @classmethod
    def _pop(cls, blocks):
        """Pop head blocks to navigate head to a point prior to fork.

        Without an undo database, there is a limit to how fully we can recover.

        If consistency is critical, run hive with TRAIL_BLOCKS=-1 to only index
        up to last irreversible. Otherwise use TRAIL_BLOCKS=2 to stay closer
        while avoiding the vast majority of microforks.

        As-is, there are a few caveats with the following strategy:

         - follow counts can get out of sync (hive needs to force-recount)
         - follow state could get out of sync (user-recoverable)

        For 1.5, also need to handle:

         - hive_communities
         - hive_members
         - hive_flags
         - hive_modlog
        """
        DB.query("START TRANSACTION")

        for block in blocks:
            num = block['num']
            date = block['date']
            log.warning("[FORK] popping block %d @ %s", num, date)
            assert num == cls.head_num(), "can only pop head block"

            # get all affected post_ids in this block
            sql = "SELECT id FROM hive_posts WHERE created_at >= :date"
            post_ids = tuple(DB.query_col(sql, date=date))

            # remove all recent records -- communities
            DB.query("DELETE FROM hive_notifs        WHERE created_at >= :date", date=date)
            DB.query("DELETE FROM hive_subscriptions WHERE created_at >= :date", date=date)
            DB.query("DELETE FROM hive_roles         WHERE created_at >= :date", date=date)
            DB.query("DELETE FROM hive_communities   WHERE created_at >= :date", date=date)

            # remove all recent records -- core
            DB.query("DELETE FROM hive_feed_cache  WHERE created_at >= :date", date=date)
            DB.query("DELETE FROM hive_reblogs     WHERE created_at >= :date", date=date)
            DB.query("DELETE FROM hive_follows     WHERE created_at >= :date", date=date)

            # remove posts: core, tags, cache entries
            if post_ids:
                DB.query("DELETE FROM hive_post_tags   WHERE post_id IN :ids", ids=post_ids)
                DB.query("DELETE FROM hive_posts       WHERE id      IN :ids", ids=post_ids)
                DB.query("DELETE FROM hive_posts_data  WHERE id      IN :ids", ids=post_ids)

            DB.query("DELETE FROM hive_payments    WHERE block_num = :num", num=num)
            DB.query("DELETE FROM hive_blocks      WHERE num = :num", num=num)

        DB.query("COMMIT")
        log.warning("[FORK] recovery complete")
        # TODO: manually re-process here the blocks which were just popped.
