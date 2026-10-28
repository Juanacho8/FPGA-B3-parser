"""mbo_check's verdict on one run's "mbo" block and MBO checkpoint dump."""

import mbo_check as mc

WIN = 200000001
REASONS = ("add_duplicate", "modify_missing", "modify_side", "modify_price", "modify_prev_qty",
           "delete_missing", "delete_side", "delete_price", "delete_qty")


def block(**kw):
    b = dict(faults=dict.fromkeys(REASONS, 0), faults_stale=dict.fromkeys(REASONS, 0), level_mismatches=0,
             mismatches=[], pool_overflow=0, table_overflow=0)
    b.update(kw)
    return b


PLAN = [((68, 1, 5, 0), WIN, [(100, 5, 1)], [])]
GOOD = "68 1 5 0 200000001 CLEAN 0 1 100 5 1 0\n"


def test_clean_run_passes_and_stale_faults_are_allowed():
    stale = dict.fromkeys(REASONS, 0) | {"delete_missing": 4}
    assert mc.check_run(block(faults_stale=stale), PLAN, GOOD) == ([], 1, 0)


def test_each_failure_is_reported():
    faults = dict.fromkeys(REASONS, 0) | {"delete_qty": 1}
    assert mc.check_run(block(faults=faults), PLAN, GOOD)[0]
    assert mc.check_run(block(level_mismatches=1, mismatches=[{"at": [68, 1, 5, 0]}]), PLAN, GOOD)[0]
    assert mc.check_run(block(pool_overflow=1), PLAN, GOOD)[0]
    assert mc.check_run(block(), PLAN, "68 1 5 0 200000001 CLEAN 0 1 100 4 1 0\n")[0]   # key differs
