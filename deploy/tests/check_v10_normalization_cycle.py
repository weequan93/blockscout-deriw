#!/usr/bin/env python3
"""Exercise a second PG15 cleanup, finite cycle and lost 3,501-row reply."""
import check_v10_normalization_plan as F
import check_v10_normalization_reuse as R


def check(*args):
    return R.check(*args, cycle=True)


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
