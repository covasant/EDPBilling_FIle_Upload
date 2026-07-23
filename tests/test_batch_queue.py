"""The queue and its in-flight guard, each test against its own instance.

This is what the module globals made impossible: every test used to share one
process-wide queue that conftest.py had no way to reset.
"""

import threading

from app.core.queue import BatchQueue, SegmentBatchTask


def _task(date="17-07-2026", segment="MCX", files=None):
    return SegmentBatchTask(
        folder_date=date, segment=segment, files=[(p, "NA") for p in (files or ["/x/f.csv"])]
    )


def test_a_fresh_queue_is_empty():
    q = BatchQueue()
    assert q.empty() and q.size == 0 and q.unfinished == 0


def test_enqueue_then_get_round_trips_the_batch():
    q = BatchQueue()
    assert q.enqueue(_task()) is True
    assert q.size == 1
    assert q.get().key == "17-07-2026|MCX|upload|scan"


def test_the_same_batch_cannot_be_queued_twice():
    """Two scans finding the same segment/date must not queue it twice."""
    q = BatchQueue()
    assert q.enqueue(_task()) is True
    assert q.enqueue(_task()) is False, "duplicate batch key must be refused"
    assert q.size == 1


def test_batches_differing_only_by_date_or_segment_both_queue():
    q = BatchQueue()
    assert q.enqueue(_task(date="17-07-2026", segment="MCX")) is True
    assert q.enqueue(_task(date="18-07-2026", segment="MCX")) is True
    assert q.enqueue(_task(date="17-07-2026", segment="EQ")) is True
    assert q.size == 3


def test_the_guard_holds_while_a_batch_is_in_flight():
    """Dequeuing does NOT free the key - otherwise a scan mid-upload would
    queue the same segment again and CBOS would get a second PROCESSID."""
    q = BatchQueue()
    q.enqueue(_task())
    task = q.get()

    assert q.is_queued(task.key), "still in flight after get()"
    assert q.enqueue(_task()) is False, "must not requeue a batch being processed"

    q.release(task.key)
    assert not q.is_queued(task.key)
    assert q.enqueue(_task()) is True, "requeueable once released"


def test_unfinished_only_drops_on_task_done():
    """size drops at get(); unfinished is the real 'everything is done' signal
    that /queue-status reports."""
    q = BatchQueue()
    q.enqueue(_task())
    q.get()

    assert q.size == 0
    assert q.unfinished == 1, "dequeued but not yet marked done"

    q.task_done()
    assert q.unfinished == 0


def test_releasing_an_unknown_key_is_harmless():
    """The worker releases in a finally block, which can run for a batch that
    never made it into the guard."""
    BatchQueue().release("never-queued")  # must not raise


def test_concurrent_enqueues_of_one_batch_admit_exactly_one():
    """The guard is the only thing standing between two scheduler threads and
    a double PROCESSID reservation."""
    q = BatchQueue()
    results = []
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        results.append(q.enqueue(_task()))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"exactly one enqueue should win, got {results}"
    assert q.size == 1


def test_two_queues_do_not_share_state():
    """The whole point of dropping the module globals."""
    a, b = BatchQueue(), BatchQueue()
    a.enqueue(_task())
    assert a.size == 1
    assert b.size == 0 and b.empty()
    assert b.enqueue(_task()) is True, "b's guard is its own"
