"""Start isolated worker adapters for executor and transport tests."""

import multiprocessing

from bistra.executor import _worker


def worker_starter(adapter, *, target=_worker):
    def start_worker(worker_id, initial):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=target, args=(child, worker_id, adapter, initial))
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        return process, parent

    return start_worker


def unexpected_parent(index, next_index):
    raise AssertionError("all dispatch groups should have been assigned to children")
