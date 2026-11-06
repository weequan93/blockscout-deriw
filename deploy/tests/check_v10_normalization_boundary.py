#!/usr/bin/env python3
"""Exercise inherited capacity budgets and a 3,501-row block on disposable PG15."""
import check_v10_normalization_plan as F
import check_v10_normalization_reuse as R


def check(*args):
    return R.check(*args, boundary=True)


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
