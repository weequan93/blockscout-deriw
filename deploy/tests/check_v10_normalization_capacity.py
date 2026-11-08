#!/usr/bin/env python3
"""Exercise the finite capacity grant and 6,000-request protocol on PG15."""
import check_v10_normalization_plan as F
import check_v10_normalization_reuse as R


def check(*args):
    return R.check(*args, capacity=True)


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
