"""All-settled execution using each stage's existing continuation lifecycle."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, replace

from mewcode.orchestration.parallel import settlement


async def run_parallel_group(*, group, local, state, chain, execute_stage, notify):
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.waits import BranchWaitPaused
    pending = [key for key in group.node_ids if key not in local]
    if not pending:
        return
    limits = [w.token_limit - w.state['tokens'] for w, _ in chain if w.token_limit is not None]
    lease = max(0, min(limits) // len(pending)) if limits else None
    semaphore = asyncio.Semaphore(group.max_concurrency)

    async def branch(node_id):
        async with semaphore:
            outcome = None
            try:
                # A stage includes scheduling, persistence and multiple
                # productive slices. An outer wall-clock cap would cancel
                # their checkpoint/continuation lifecycle indiscriminately.
                outcome = await execute_stage(node_id, group, lease)
            except asyncio.CancelledError:
                raise
            except BranchWaitPaused as exc:
                outcome = (exc.reason, None, ExpertRoleResult(text=exc.reason, status='paused',
                    error_kind=exc.error_kind, failed_node_id=node_id), False)
            except Exception as exc:
                outcome = (str(exc), None, ExpertRoleResult(text=f'{type(exc).__name__}: {exc}',
                    status='failed', error_kind='node_failure', failed_node_id=node_id), False)
            finally:
                for watchdog, _ in chain:
                    active = [key for key in watchdog.state.get('active_nodes', []) if key != node_id]
                    watchdog.state.update(active_nodes=active, active_node=next(iter(active), ''),
                                          active_at=time.time() if active else None)
            if outcome is not None:
                failure = outcome[2] or ExpertRoleResult(text=outcome[0], status='needs_attention',
                                                         error_kind='parallel_branch_unfinished')
                local[node_id] = replace(failure, failed_node_id=node_id)
                state['round_results'][node_id] = asdict(local[node_id])
            if node_id in local:
                # Failure is a settled outcome for this fan-in only. It never
                # becomes an accepted artifact or a successful quality verdict.
                receipt = settlement(group, local)
                state.setdefault('parallel_settlements', {})[group.group_id] = {
                    **receipt, 'contract_hash': state['contract_hash']}
                await notify()

    tasks = [asyncio.create_task(branch(key)) for key in pending]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
